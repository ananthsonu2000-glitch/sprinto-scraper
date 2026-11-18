# Build report — Sprinto prospecting MVP

## Starting point
No existing repository for this project was found on the machine, so this is a new Python
codebase (FastAPI, stdlib `sqlite3`, LangGraph, httpx, BeautifulSoup, Playwright). The AI runs on
Google Gemini by default, with Claude (Anthropic SDK) as an alternative. SQLAlchemy was dropped
because Windows Application Control blocked its native extension; three other native wheels are
pinned to versions the policy allows (see `requirements.txt`).

## Implemented
* **Inputs:** domain paste/CSV, normalisation, dedupe, rejection of internal hosts/IPs.
* **Find companies by filters:** Hunter Discover plus a news search (Gemini with Google Search).
  Each filter is shown as matched / unknown / not matched with its source. Non-commercial
  organisations (universities, government bodies, non-profits) are excluded by default.
* **Website reader (our own scraper):**
  * SSRF guard on every URL and redirect hop (private/metadata IPs, ports, credentials, internal
    suffixes, DNS check).
  * robots.txt, timeouts, size cap and a per-domain page budget.
  * Menus, headers and footers stripped from the text.
  * Playwright browser rendering for JavaScript-only pages, with the guard checking every request.
  * Page selection takes one page each of security/trust (including `trust.` subdomains),
    customers and company/careers.
* **Evidence store:** URL, exact excerpt (validated against the retrieved text), retrieved_at,
  published_at and source type. 13 fixed fields, each with value, evidence IDs and status.
  Conflicts are kept, funding is never inferred, compliance wording is kept exactly as written,
  and an absence never becomes a negative.
* **Qualification:** scored in code from levels the AI proposes. Fabricated evidence IDs earn 0;
  reason-now windows are checked against dates; outcomes are QUALIFIED / DISQUALIFIED /
  LOW_PRIORITY / INSUFFICIENT_EVIDENCE. Blocked pages count as retrieval failures. The research
  loop is bounded: homepage + 3 pages, then up to 3 rounds of up to 2 pages.
* **Contacts:** Hunter Domain Search with caching and quota/auth pausing; 5-criterion scoring and
  role preference; NO_SUITABLE_CONTACT when nobody fits.
* **Pain hypotheses:** two distinct, evidence-backed hypotheses with code-verified scores;
  INSUFFICIENT_PAIN_EVIDENCE otherwise.
* **Email:**
  * The AI writes the parts (greeting, opening fact, paragraphs, question) and code assembles the
    layout, with blank lines between parts and a sign-off.
  * A send-time safety net reflows any single-block email before it goes out.
  * QA: a separate evaluator plus 8 code checks, at most 2 revisions, otherwise EMAIL_QA_FAILED.
* **Demo-only delivery,** enforced at the adapter boundary (to/cc/bcc/attendees):
  `[DEMO — Company]` subjects, `[DEMO]` events, and no tool accepts a recipient.
* **Reply agent** (LangGraph, SQLite checkpoints):
  * Handles interest, information requests (not treated as consent), exact-slot confirmation with
    a fresh calendar check, counter-proposals, rejection, opt-out, referrals, out-of-office,
    bounces, and unsupported or ambiguous replies (handed off).
  * Counter-proposals increment the round, invalidate the earlier calendar check, and need an
    explicit date, time and timezone, or the agent asks.
  * **Timing preferences** ("next week", "Thursday afternoon", "tomorrow", "mornings", "week after
    next") limit the offered slots to that window.
  * A 2-round negotiation cap, a 48-hour no-reply handoff, and rescheduling by updating the
    existing event.
  * Real Gmail replies have the quoted earlier message stripped before classification.
* **AI model chain:** Gemini models ranked best first, with automatic fallback on any failure.
  Failing models cool down (5 minutes; 1 hour after a quota error), and a company is retried on
  later ticks when every model is busy.
* **Reliability:** operation ledger with stable op IDs and provider reconciliation, so uncertain
  sends or bookings pause instead of repeating. Leases and optimistic version checks; restart
  resumes from stored stages and graph checkpoints.
* **Demo activity log** as the CRM adapter, with a fallback file if logging fails.
* **Dashboard:** Find companies tab; Pipeline tab with mode banner, readiness (including the model
  chain), company list, evidence and rubric, contacts, facts vs hypotheses, email and checks,
  thread and scheduling, labelled simulated replies, tool/ops log, and the escalation queue.

## Test results
`pytest`: **124 passed**, fully offline (fixtures, simulated AI, fault-injecting mailbox and
calendar, mocked Gemini/Hunter/Gmail HTTP). Every test case listed in the brief is covered.

## Live results (18 Nov 2026)
| Item | Result |
|---|---|
| Website reader | Chargebee, Postman, Freshworks. The browser fallback revealed JavaScript-only certifications (Chargebee SOC 2 Type II, ISO 27001:2022, PCI DSS Level 1) |
| Gemini analysis | Postman QUALIFIED 80/100; Chargebee QUALIFIED 80/100. The fallback chain handled 429/503/timeouts/retired models |
| Hunter | Postman: 6 people, Head of GRC chosen (10/10). Chargebee: NO_SUITABLE_CONTACT (no security/GRC/engineering lead; correct under the rubric). Discover: 15 Indian software companies, with colleges filtered out |
| Reply agent (dry run, Gemini) | interest → slots → booking → counter-proposal without a timezone → clarification → confirmation → event moved (no duplicate) |
| **Real demo** (Gmail + Calendar) | Real email sent to the demo inbox only; the user's real reply was read; slots came from the real calendar; "option c" → **real `[DEMO]` event, Mon 23 Nov 11:00 IST**, demo address only; confirmation sent after the event was created |
| Fixed from live feedback | Single-block email layout; the "next week" preference being ignored |
| Not run live | Gemini news search: Google Search grounding quota exhausted on the free key |

## Credentials and setup in place
Gemini API key, Hunter API key (free plan), Google OAuth (project `sprinto-prospecting-demo`,
Internal consent screen, Desktop client, refresh token), sender and demo recipient = the user's
own account. Mode: `real_demo` with `REAL_SENDS_ENABLED=true`.

## Open items
* Replace the Gemini and Hunter keys: they were pasted into chat.
* Enable Gemini billing (for the news search and Gemini 3.1 Pro).
* Confirm the capability wording in `config/capabilities_v1.json` (marked PENDING_HUMAN_CONFIRMATION).
* Optional: a Hunter paid plan (funding filter, more searches).

## Limitations
* The simulated AI is rule-based; it proves the pipeline and the guards, not the quality of the real prompts.
* Missing pain evidence ends the account rather than spending leftover research budget on more pages.
* DNS is checked before each request, but httpx resolves the name again, so a small DNS-rebinding
  window remains. Cross-site redirects are refused.
* SQLite with leases suits a single host; several hosts would need Postgres.
* Gmail replies are read by polling (no push notifications).
* Gemini news citations are grounded statements linked to sources, not exact quotes.
* Trust centres hosted on third-party domains are not fetched.
* Out-of-office notices don't extend the no-reply deadline.
