"""Free-tier mode: never spend calls on models with no free quota; wait for daily resets."""
import json

import httpx
import pytest
from pydantic import BaseModel

from sprinto_agent.errors import IntegrationError
from sprinto_agent.llm.gemini_llm import GeminiLLM, rank_models
from test_gemini import REAL_LIST


class Probe(BaseModel):
    company: str
    is_software: bool


OK = httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps({"company": "A", "is_software": True})}]},
                                               "finishReason": "STOP"}]})


def quota_429(zero=False, per_day=True, retry="68798s", details=True):
    msg = "You exceeded your current quota, please check your plan and billing details.\n* Quota exceeded for metric: x, " \
          + ("limit: 0, " if zero else "limit: 20, ") + "model: m"
    d = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
          "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier" if per_day else
                          "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
         {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry}] if details else []
    return httpx.Response(429, json={"error": {"code": 429, "message": msg, "details": d}})


def make(handler, chain, tmp_path, **kw):
    calls = []

    def h(req):
        model = req.url.path.split("/models/")[1].split(":")[0]
        calls.append(model)
        return handler(model, req)
    return GeminiLLM("k", models=chain, transport=httpx.MockTransport(h),
                     state_path=str(tmp_path / "quota.json"), **kw), calls


def test_free_tier_drops_pro_models(monkeypatch):
    monkeypatch.setattr(GeminiLLM, "_discover", lambda self: rank_models(REAL_LIST))
    llm = GeminiLLM("k", free_tier=True)
    assert not any("pro" in m for m in llm.chain) and llm.chain[0] == "gemini-3.8-flash"
    assert "free tier" in llm.label


def test_zero_free_quota_model_is_never_called_again(tmp_path):
    llm, calls = make(lambda m, r: quota_429(zero=True) if m == "pro" else OK, ["pro", "flash"], tmp_path)
    llm.structured("t", "s", {}, Probe)
    llm.structured("t", "s", {}, Probe)
    assert calls == ["pro", "flash", "flash"]
    # a new process (e.g. the worker) reads the shared state and also skips it
    llm2, calls2 = make(lambda m, r: OK, ["pro", "flash"], tmp_path)
    llm2.structured("t", "s", {}, Probe)
    assert calls2 == ["flash"]


def test_daily_quota_hold_uses_googles_retry_time(tmp_path):
    llm, calls = make(lambda m, r: quota_429() if m == "a" else OK, ["a", "b"], tmp_path)
    llm.structured("t", "s", {}, Probe)
    st = llm.quota_status()
    assert "a" in st["on_quota_hold"] and st["available_now"] == ["b"]
    llm.structured("t", "s", {}, Probe)
    assert calls == ["a", "b", "b"]  # 'a' skipped without a call while on hold


def test_all_models_out_of_quota_raises_quota_without_calls(tmp_path):
    llm, calls = make(lambda m, r: quota_429(), ["a", "b"], tmp_path)
    with pytest.raises(IntegrationError) as e:
        llm.structured("t", "s", {}, Probe)
    assert e.value.kind == "quota" and len(calls) == 2
    with pytest.raises(IntegrationError) as e2:
        llm.structured("t", "s", {}, Probe)
    assert e2.value.kind == "quota" and "used up" in e2.value.message and len(calls) == 2  # no new calls


def test_search_allowance_exhausted_stops_after_two_models(tmp_path):
    llm, calls = make(lambda m, r: quota_429(details=False), ["a", "b", "c", "d"], tmp_path)
    with pytest.raises(IntegrationError) as e:
        llm.web_research("s", "find companies")
    assert e.value.kind == "quota" and "Google Search" in e.value.message
    assert calls == ["a", "b"]  # didn't burn the whole chain
    with pytest.raises(IntegrationError):
        llm.web_research("s", "again")
    assert calls == ["a", "b"]  # held until the daily reset


def test_pipeline_waits_for_quota_without_escalating(ctx):
    from sprinto_agent import store
    from sprinto_agent.outreach import create_campaign
    from sprinto_agent.worker import Worker

    class OutOfQuota:
        simulated = False
        label = "free tier"

        def structured(self, *a, **k):
            raise IntegrationError("quota", "free Gemini quota used up on every model until 19 Nov 13:30 IST", "llm")
    ctx.llm = OutOfQuota()
    pid = create_campaign(ctx, "t", ["acmecloud.example"])["prospect_ids"][0]
    for _ in range(4):
        Worker(ctx).tick()
    p = store.get_prospect(ctx.conn, pid)
    assert p["stage"] == "RESEARCHING" and "waiting for free AI quota" in p["last_error"]
    assert not store.open_escalations(ctx.conn)
