"""FastAPI backend + static dashboard.

  uvicorn sprinto_agent.api:app --port 8000

The API never returns secrets. Start-run only enqueues work; the worker (separate
process, or EMBEDDED_WORKER=true, or the "Run worker tick" button) does the work, so
nothing depends on a browser staying open.
"""
from __future__ import annotations

import csv
import io
import os
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import persist, store
from .config import Mode, Settings
from .context import build_context, readiness
from .human import HumanActionError, resolve_escalation, resume_integration
from .outreach import create_campaign
from .tools import tool_catalog
from .worker import Worker

WEB = Path(__file__).resolve().parent.parent / "web"
LOCK = threading.RLock()  # one sqlite connection per process; serialise access

app = FastAPI(title="Sprinto Prospecting MVP (demo)")
_state: dict = {}
SERVERLESS = bool(os.environ.get("SERVERLESS") or os.environ.get("VERCEL"))
TICK_BUDGET = float(os.environ.get("TICK_BUDGET_SECONDS") or 150)


def ctx():
    if "ctx" not in _state:
        persist.restore()  # serverless cold start: pull the last snapshot (no-op locally)
        _state["ctx"] = build_context(Settings.from_env())
        _state["worker"] = Worker(_state["ctx"], owner="api-embedded")
        if SERVERLESS:
            _state["worker"].max_steps = int(os.environ.get("SERVERLESS_MAX_STEPS") or 1)
        if _state["ctx"].settings.embedded_worker:
            def loop():
                while True:
                    with LOCK:
                        try:
                            _state["worker"].tick()
                        except Exception:  # noqa: BLE001
                            pass
                    time.sleep(_state["ctx"].settings.worker_poll_seconds)
            threading.Thread(target=loop, daemon=True).start()
    return _state["ctx"]


@app.middleware("http")
async def snapshot_after_writes(request: Request, call_next):
    response = await call_next(request)
    if request.method != "GET" and persist.enabled() and "ctx" in _state:
        with LOCK:
            persist.save()
    return response


class RunIn(BaseModel):
    name: str = "Demo campaign"
    domains_text: str = ""
    csv_text: str = ""


class ReplyIn(BaseModel):
    body: str


class ResolveIn(BaseModel):
    action: str
    note: str = ""


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/status")
def status():
    with LOCK:
        c = ctx()
        return {"settings": c.settings.public_view(), "versions": c.versions.summary(), "readiness": readiness(c),
                "mode_labels": Mode.LABELS, "serverless": SERVERLESS}


@app.get("/api/tools")
def tools():
    return tool_catalog()


@app.post("/api/runs")
def start_run(body: RunIn):
    domains = [d for d in body.domains_text.replace(",", "\n").splitlines() if d.strip()]
    if body.csv_text.strip():
        rows = list(csv.reader(io.StringIO(body.csv_text)))
        if rows and any("domain" in h.lower() for h in rows[0]):
            idx = next(i for i, h in enumerate(rows[0]) if "domain" in h.lower())
            rows = rows[1:]
        else:
            idx = 0
        domains += [r[idx] for r in rows if len(r) > idx and r[idx].strip()]
    if not domains:
        raise HTTPException(400, "no domains supplied")
    with LOCK:
        return create_campaign(ctx(), body.name, domains)


@app.post("/api/worker/tick")
def tick():
    with LOCK:
        ctx()
        if SERVERLESS:
            return _state["worker"].tick(budget_seconds=TICK_BUDGET, on_progress=persist.save)
        return _state["worker"].tick()


@app.get("/api/campaigns")
def campaigns():
    with LOCK:
        return [dict(r) for r in ctx().conn.execute("SELECT * FROM campaigns ORDER BY created_at DESC")]


SUMMARY_COLS = ("id, campaign_id, normalized_domain, mode, stage, terminal_status, qualification_status, "
                "qualification_score, contact_score, reply_status, meeting_status, escalation_reason, suppressed, updated_at, contact")


@app.get("/api/prospects")
def prospects(campaign_id: str | None = None):
    with LOCK:
        c = ctx()
        q = f"SELECT {SUMMARY_COLS} FROM prospects" + (" WHERE campaign_id=?" if campaign_id else "") + " ORDER BY created_at"
        rows = c.conn.execute(q, (campaign_id,) if campaign_id else ()).fetchall()
        out = []
        for r in rows:
            d = store.row_to_dict(r)
            d["contact_name"] = (d.pop("contact") or {}).get("name")
            out.append(d)
        return out


@app.get("/api/prospects/{pid}")
def prospect(pid: str):
    with LOCK:
        c = ctx()
        p = store.get_prospect(c.conn, pid)
        if not p:
            raise HTTPException(404)
        p.pop("lease_owner", None)
        pages = [{k: v for k, v in pg.items() if k != "text"} | {"text_chars": len(pg["text"] or "")}
                 for pg in store.list_pages(c.conn, pid)]
        return {
            "prospect": p,
            "evidence": list(store.evidence_map(c.conn, pid).values()),
            "pages": pages,
            "messages": store.list_messages(c.conn, pid),
            "operations": [dict(r) for r in c.conn.execute("SELECT * FROM operations WHERE prospect_id=? ORDER BY created_at", (pid,))],
            "activity": [dict(r) for r in c.conn.execute("SELECT * FROM activity_log WHERE prospect_id=? ORDER BY id", (pid,))],
            "escalations": [dict(r) for r in c.conn.execute("SELECT * FROM escalations WHERE prospect_id=? ORDER BY created_at", (pid,))],
            "calendar_events": [dict(r) for r in c.conn.execute(
                "SELECT e.* FROM sim_calendar_events e WHERE e.event_id=? OR e.op_id LIKE ?",
                (p["calendar_event_id"] or "", f"%{pid}%"))],
        }


@app.post("/api/prospects/{pid}/simulate-reply")
def simulate_reply(pid: str, body: ReplyIn):
    with LOCK:
        c = ctx()
        if c.settings.mode == Mode.REAL_DEMO:
            raise HTTPException(400, "Simulated replies are disabled in real_demo mode; reply in Gmail instead.")
        p = store.get_prospect(c.conn, pid)
        if not p or not p["gmail_thread_id"]:
            raise HTTPException(400, "prospect has no tracked thread yet")
        mid = c.mailbox.inject_prospect_reply(p["gmail_thread_id"], body.body)
        store.log_activity(c.conn, pid, "simulated_prospect_reply", "injected", body.body[:200], actor="human",
                           campaign_id=p["campaign_id"])
        return {"provider_message_id": mid, "label": "SIMULATED PROSPECT REPLY"}


@app.get("/api/escalations")
def escalations():
    with LOCK:
        return store.open_escalations(ctx().conn)


@app.post("/api/escalations/{esc_id}/resolve")
def resolve(esc_id: str, body: ResolveIn):
    with LOCK:
        try:
            return resolve_escalation(ctx(), esc_id, body.action, body.note)
        except HumanActionError as e:
            raise HTTPException(400, str(e))


@app.post("/api/integrations/{name}/resume")
def integration_resume(name: str):
    with LOCK:
        return {"resumed": resume_integration(ctx(), name)}


# ------------------------------------------------------------------ discovery (find companies by filters)

class DomainIn(BaseModel):
    domain: str


class AddIn(BaseModel):
    candidate_ids: list[str]
    name: str | None = None


@app.get("/api/discovery/options")
def discovery_options():
    from . import discovery as dz
    with LOCK:
        c = ctx()
        return {"industries": dz.industries(), "headcount": dz.HEADCOUNT_RANGES,
                "funding_series": [{"id": k, "label": v} for k, v in dz.SERIES_LABEL.items()],
                "sources": [{"id": "hunter_discover", "label": c.hunter.label + " — Discover"},
                            {"id": "news_search", "label": "News & web search via " + c.llm.label}]}


@app.post("/api/discovery/search")
def discovery_search(body: dict):
    from pydantic import ValidationError

    from . import discovery as dz
    try:
        f = dz.DiscoveryFilters(**body)
    except ValidationError as e:
        raise HTTPException(422, "; ".join(err["msg"] for err in e.errors()))
    if not f.requested():
        raise HTTPException(422, "Choose at least one filter")
    with LOCK:
        return dz.run_search(ctx(), f)


@app.get("/api/discovery/searches")
def discovery_searches():
    with LOCK:
        rows = ctx().conn.execute(
            "SELECT s.id, s.created_at, s.status, s.filters, (SELECT COUNT(*) FROM discovery_candidates c WHERE c.search_id=s.id) n"
            " FROM discovery_searches s ORDER BY s.created_at DESC LIMIT 20").fetchall()
        return [dict(r) for r in rows]


@app.get("/api/discovery/searches/{sid}")
def discovery_get(sid: str):
    from . import discovery as dz
    with LOCK:
        try:
            return dz.get_search(ctx(), sid)
        except KeyError:
            raise HTTPException(404)


@app.post("/api/discovery/candidates/{cid}/domain")
def discovery_domain(cid: str, body: DomainIn):
    from . import discovery as dz
    with LOCK:
        try:
            return dz.set_candidate_domain(ctx(), cid, body.domain)
        except (ValueError, KeyError) as e:
            raise HTTPException(400, str(e))


@app.post("/api/discovery/searches/{sid}/add")
def discovery_add(sid: str, body: AddIn):
    from . import discovery as dz
    with LOCK:
        return dz.add_to_run(ctx(), sid, body.candidate_ids, body.name)


@app.get("/api/activity")
def activity(limit: int = 200):
    with LOCK:
        rows = ctx().conn.execute(
            "SELECT a.*, p.normalized_domain FROM activity_log a LEFT JOIN prospects p ON p.id=a.prospect_id "
            "ORDER BY a.id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
