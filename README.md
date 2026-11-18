# Sprinto Prospecting & Meeting-Scheduling MVP (demo delivery only)

Given company domains, the system reads each website, extracts evidence-backed facts,
qualifies the company with a bounded research loop, picks a named contact from Hunter,
writes two evidence-backed pain hypotheses, drafts and QA-checks an email, sends it
**only to your demo address**, then handles replies and scheduling with a LangGraph agent
that checks Google Calendar and books only explicitly agreed slots.

> Three visibly distinct modes: **SIMULATION** (fixtures, nothing real), **DRY RUN**
> (real reads, nothing sent/booked), **REAL DEMO** (real sends/bookings, demo address only).

## Quick start (simulation — no credentials)

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt      # Windows; use .venv/bin/python elsewhere
.venv/Scripts/python -m pytest -q                              # 124 tests, all offline
.venv/Scripts/python scripts/demo_simulation.py                # end-to-end walkthrough in the terminal
.venv/Scripts/python -m uvicorn sprinto_agent.api:app --port 8765   # dashboard at http://localhost:8765
```

In the dashboard: **Use fixture domains → Start run → Run worker tick**. Open a company,
go to *Thread & scheduling*, type a labelled **simulated prospect reply** (e.g. "Interested,
happy to chat", then "Option B works"), and run another tick.

## Find companies by filters (no domain list needed)

Open **1 · Find companies** in the dashboard. Choose industry (Hunter's industry list),
company size, funding stage and recency, HQ countries, roles being hired, minimum NRR and
keywords, then click **Search**.

* **Hunter Discover**: company search by filters, which uses the same `HUNTER_API_KEY`. Hunter's
  docs say this call is free. Hunter silently ignores filters your plan doesn't include
  (funding, for example, is paid-only). The tool reports which filters were applied and never
  counts an ignored filter as a match.
* **News & web search**: Gemini with Google Search grounding (`GEMINI_API_KEY`), or Claude's web
  search tool when `LLM_PROVIDER=anthropic`. Every company must be named in a retrieved source;
  funding, dates and NRR count only when that source states them. Sources that contain
  instructions aimed at AI agents are dropped. With Gemini, the citations are Gemini's own
  statements linked to Google sources, not exact quotes from the articles.
  Google Search grounding has its own quota. A free key can run out of it while normal Gemini
  calls still work; the results panel then shows a quota error for this source.
* **Non-commercial organisations** (universities, colleges, government bodies, non-profits) are
  excluded by default. Hunter's industry tags include some of them, so the tool also filters by
  name and by `.edu`/`.ac`/`.gov` domains. A checkbox turns this off.
* Each filter shows **✓ matched**, **? unknown** or **✗ not matched**, with the source behind it.
  * **strong**: every filter is confirmed.
  * **partial**: nothing contradicts your filters, but some are unconfirmed.
  * **needs domain**: no source states the website. Type it in before adding.
  * **excluded**: a source contradicts one of your filters.
* **NRR** is rarely published by private companies. It matches only when a source states it;
  otherwise it stays unknown and doesn't exclude the company.
* Tick companies and click **Add selected to a new run**. They then go through the normal
  pipeline: scrape the website, qualify, find contacts, write and send the email.

## Setup for real integrations

Copy `.env.example` to `.env` (never commit `.env`). Secrets stay server-side; the API
only reports whether each is configured.

| Integration | What to set | Notes |
|---|---|---|
| Mode | `APP_MODE=dry_run` first, then `real_demo` | `real_demo` also needs `REAL_SENDS_ENABLED=true` and `DEMO_RECIPIENT_EMAIL` |
| LLM (default: Gemini) | `LLM_PROVIDER=gemini`, `GEMINI_API_KEY`. Optional: `GEMINI_MODELS`, `GEMINI_SEARCH_MODELS`, `GEMINI_MAX_MODELS` (10), `GEMINI_TIMEOUT_SECONDS` (90) | Auto-ranked model chain with automatic fallback (see below). Gemini 3.1 Pro and Google Search grounding need billing enabled on the key |
| LLM (alternative: Claude) | `LLM_PROVIDER=anthropic`, `ANTHROPIC_API_KEY`, `LLM_MODEL` (default `claude-opus-5-5`) | Structured outputs via `messages.parse`; server-side refusal fallback on by default (`LLM_SERVER_FALLBACK=off` to disable) |
| Hunter | `HUNTER_API_KEY` | Domain Search (people) and Discover (company search). Quota/plan errors pause lookups and show on the readiness panel. No free-tier allowance is assumed; the free plan ignores the funding filter |
| Gmail + Calendar | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, then `python scripts/google_oauth.py` → `GOOGLE_REFRESH_TOKEN`; `GMAIL_SENDER_ADDRESS`; `GOOGLE_CALENDAR_ID` | Scopes: `gmail.send`, `gmail.readonly`, `calendar.events`, `calendar.freebusy` (least privilege for implemented features). Use a Desktop OAuth client |
| Database | `DB_PATH`, `CHECKPOINT_DB_PATH` | SQLite (WAL). Migrations in `migrations/` run automatically on start |
| Worker | `python -m sprinto_agent.worker` (loop) or `--once`; or `EMBEDDED_WORKER=true` in the API | Several workers can run; per-prospect DB leases prevent double processing |
| Website reader | `BROWSER_FALLBACK=true` + `pip install playwright && python -m playwright install chromium` | Optional JS-render fallback; every browser request is re-checked by the SSRF guard |

Missing credentials never produce fake success: the affected prospect pauses with
`SETUP_REQUIRED` / `WAITING_INTEGRATION` and the readiness panel says why. After adding a
key, click **Resume** next to that integration on the readiness panel. Paused companies
continue from where they stopped, and pages already scraped are not fetched again.

### Gemini models and automatic fallback

At start-up the tool lists the models your key can use and ranks them, best first:
newest generation first; then Pro, Flash, Flash-Lite; then newer minor versions; stable
before preview. Speech, image, transcription and unnamed experimental models are skipped.
Set `GEMINI_MODELS` to force your own order.

Each call goes down the chain until one model succeeds. A call moves to the next model on:
* a busy or overloaded model (503), rate limit (429), quota limit, or server error;
* a timeout or connection error;
* a retired or unknown model (404, which is then skipped for the rest of the run);
* a rejected request (400) that isn't about the API key;
* blocked or truncated output;
* output that doesn't match the required JSON schema.

A wrong API key stops straight away instead of trying every model. A model that just failed is
tried last for 5 minutes, or for 1 hour after a quota error. Gemini 3+ models use low "thinking"
effort for schema-bound tasks, so they finish within the timeout.

**Free tier only (`GEMINI_FREE_TIER=true`).** Free quotas are per model and per day, and reset
at midnight Pacific (13:30 IST in winter). In this mode:
* Pro models are left out of the chain; free keys get "limit: 0" for them.
* Any model that still answers "limit: 0" is skipped permanently.
* A quota error puts a model on hold until the time Google gives (usually the daily reset).
  While on hold it is skipped without a call.
* Because each model has its own allowance, falling back across models spreads your daily capacity.
* The free Google Search allowance (used by the news search) is tracked separately. After two
  models refuse it, the search is paused until the reset instead of trying the whole chain.
* When every model is used up, companies wait in their current stage and continue automatically
  after the reset. Nothing is escalated and no calls are wasted.
* Holds are shared by the worker, dashboard and scripts (`data/gemini_quota_state.json`).
* The readiness panel shows which models are available now, which are waiting for the reset,
  and whether news search is available.

If every model fails, the company isn't escalated straight away. It keeps its stage, waits, and
is retried on the next worker tick; it goes to the escalation queue (`LLM_UNAVAILABLE`) only
after 3 failed ticks. The readiness panel shows the model order and the last model used, and
every fallback is written to the Demo activity log with the reason for each skipped model.

### Live checks

```bash
.venv/Scripts/python scripts/check_setup.py                 # free / read-only call to each service; prints OK / FAIL / SKIP
.venv/Scripts/python scripts/live_scrape_check.py stripe.com  # real website reader on any domains (no keys needed)
```

The website reader has been run live on real company sites. With the browser fallback on,
pages that build their content with JavaScript (for example Chargebee's security page)
are rendered, so certification wording such as "SOC 2 Type II" and "ISO 27001:2022" is
captured as evidence. Trust centres on subdomains (such as `trust.<company>.com`) are
selected as security pages.

## Demo delivery (enforced in code)

* `send_email` has **no recipient parameter**. The address comes only from `DEMO_RECIPIENT_EMAIL`.
* Every mailbox/calendar adapter calls `delivery_guard.enforce_*` before any I/O: `to` must
  equal the demo address, `cc`/`bcc` must be empty, attendees must equal the demo address.
* Subjects are prefixed `[DEMO — Company]`; events `[DEMO]`.
* Hunter emails are stored as `email_research_only` and never reach delivery code.
* Self-addressed threads: outbound mail carries `X-Sprinto-Agent` + our own `Message-ID`;
  the poller ignores those and our outbound IDs, and dedupes inbound by provider message ID.

## Component map

```
sprinto_agent/
  config.py            settings (env), modes, versioned config loader (rubric/policy/capabilities/prompts)
  db.py, store.py      SQLite, migrations, optimistic-locking prospect state, leases, evidence,
                       Demo activity log (CRM adapter boundary), escalations, messages
  research/
    url_guard.py       domain normalisation + SSRF guard (private IPs, metadata, ports, redirects)
    fetcher.py         HTTP reader (robots, size/time limits, redirect validation, browser fallback)
                       + FixtureFetcher (SIMULATION)
    extraction.py      fixed-field extraction; excerpt validation; never-infer rules; conflicts kept
    qualification.py   rubric scoring in code (LLM only proposes levels + evidence IDs)
    agent.py           bounded research loop (homepage + 3, then ≤3 rounds × ≤2 pages)
  contacts.py          Hunter candidate scoring (5 × 0–2) and role preference
  pain.py              hypothesis verification/scoring, distinctness, INSUFFICIENT_PAIN_EVIDENCE
  claims.py            deterministic claim-safety rules (metrics, urgency, deficiency, false Re:)
  email_gen.py         draft → separate evaluation → mandatory checks → ≤2 revisions
  integrations/        hunter.py, mailbox.py (Gmail/sim/dry-run), calendar.py (Google/sim/dry-run),
                       google_auth.py, delivery_guard.py
  tools.py             9 typed tools with what/when/when-not/returns, structured results, logging
  ops.py               operation ledger: idempotent irreversible actions + reconciliation
  outreach.py          initial outreach stage machine; batch isolation
  scheduling.py        slots, explicit tz formatting, safe parsing of proposed times
  reply_agent.py       LangGraph reply/scheduling graph with SQLite checkpoints
  discovery.py         find companies by filters (Hunter Discover + news web search), honest matching
  human.py             controlled escalation resolution, integration resume
  worker.py            background ticks: outreach, polling, replies, no-reply deadline
  api.py + web/        FastAPI + dashboard
  llm/                 gemini_llm.py (ranked model chain + fallback, Google Search grounding),
                       anthropic_llm.py, simulated.py (rule-based, SIMULATION only), prompt rendering
config/                rubric_v1.json, policy_v1.json, capabilities_v1.json, prompts_v1.json (versioned),
                       hunter_industries.json (Hunter's industry list)
fixtures/              simulated websites, Hunter responses and news (fictional .example companies)
scripts/               check_setup.py, live_scrape_check.py, google_oauth.py, demo_simulation.py
tests/                 124 offline tests
```

## Tools

| Tool | What | When | When not | Returns |
|---|---|---|---|---|
| `fetch_website_page` | Guarded fetch + parse + store one page | Initial research; research rounds for a named gap | Off-site, private, login-protected, already-fetched, over budget | page id, status, final URL, sizes, attempts, error |
| `search_hunter_contacts` | Hunter Domain Search (cached) | Once after qualification | Before qualification; Hunter paused; never for delivery | candidates or quota/auth/setup error |
| `send_email` | Send in tracked thread to demo address (idempotent op ID) | Outreach after QA; verified replies | Prospect addresses; before QA; confirmation before booking; chasers | message id, thread id, label, or classified error |
| `read_thread_replies` | Ingest new user-authored messages from tracked thread | Polling | Unrelated inbox; agent-authored mail | counts of new / ignored messages |
| `check_calendar` | Organizer free/busy | Before proposing; right before booking | Inferring prospect availability; reusing a stale check | busy intervals, checked_at |
| `book_meeting` | Create `[DEMO]` event, demo attendee only | Explicit exact-slot agreement + fresh check | Interest alone; ambiguity; already booked | event id or error |
| `update_meeting` | Move existing event | Agreed change to a booked meeting | Creating meetings | event id or error |
| `update_crm` | Write to the internal *Demo activity log* | Outcomes and key events | Implying an external CRM | target |
| `escalate` | Pause only this prospect with reason + allowed actions | Ambiguity, limits, uncertainty, setup | Inventing a decision instead | escalation id |

Full schemas: `GET /api/tools`.

## Error handling

| Tool / area | Transient | Permanent / invalid | Auth / setup | Quota | Uncertain outcome |
|---|---|---|---|---|---|
| fetch_website_page | 3 attempts, 0.5s/1.5s backoff; then recorded as retrieval failure | 4xx/robots/unsafe recorded, not retried, not negative evidence | — | — | n/a (read) |
| search_hunter_contacts | 3 attempts | 400/422 not retried | integration marked `auth_failed`; prospect → WAITING_INTEGRATION | `paused_quota`; lookups paused, reason shown | n/a |
| LLM (Gemini) | next model in the chain; if all fail, retry on the next tick, escalate `LLM_UNAVAILABLE` after 3 ticks | invalid output / blocked → next model | bad key → prospect WAITING_INTEGRATION | model put in a 1-hour cooldown, next model used | n/a |
| LLM (Claude) | Anthropic SDK retries (2), then retry on the next tick as above | refusal / bad request → escalate | prospect → WAITING_INTEGRATION | — | n/a |
| check_calendar | 3 attempts, then `CALENDAR_UNAVAILABLE` handoff; no unverified slots | — | integration status set | — | n/a |
| send_email | — | `SEND_FAILED` escalation | `SETUP_REQUIRED` escalation | — | reconcile by Message-ID; found → success; confirmed absent → one retry; lookup fails → `SEND_UNCERTAIN` pause |
| book/update_meeting | — | `BOOKING_FAILED`, no confirmation sent | escalation | — | reconcile by `sprintoOpId`; else `BOOKING_UNCERTAIN` pause |
| update_crm / activity log | retry ×3, then fallback file replayed next tick; action state kept | — | — | — | n/a |

Stable operation IDs: outreach `outreach:{campaign}:{domain}`, reply `reply:{thread}:{incoming_msg}:{action}`,
booking `book:{prospect}:{slot}`.

## Human role

Rubrics, thresholds, prompts and approved capabilities live in versioned files under `config/`;
every campaign and prospect records the versions used. The **escalation queue** shows reason and
only the resume actions valid for that reason (e.g. `confirm_sent` / `confirm_not_sent` for
`SEND_UNCERTAIN`). Escalation pauses only that prospect.
