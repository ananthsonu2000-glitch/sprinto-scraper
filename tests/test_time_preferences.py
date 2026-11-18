"""'Let's chat next week' must only get next-week slots. Clock: Tue 17 Nov 2026 11:30 IST."""
from datetime import date, time

from helpers import acme_awaiting, agent_mails, reply

from sprinto_agent import clock
from sprinto_agent.scheduling import resolve_window

NEXT_WEEK = [date(2026, 11, d) for d in (23, 24, 25, 26, 27)]


def test_resolves_common_phrases(settings):
    assert resolve_window("lets chat next week", settings).dates == NEXT_WEEK
    assert resolve_window("Can we do the following week?", settings).dates == NEXT_WEEK
    assert resolve_window("tomorrow works", settings).dates == [date(2026, 11, 18)]
    assert resolve_window("later this week", settings).dates == [date(2026, 11, 18), date(2026, 11, 19), date(2026, 11, 20)]
    assert resolve_window("week after next", settings).dates[0] == date(2026, 11, 30)
    w = resolve_window("Thursday afternoon is best", settings)
    assert w.dates == [date(2026, 11, 19)] and w.hours == (time(12, 0), time(17, 0))
    assert resolve_window("next Tuesday", settings).dates == [date(2026, 11, 24)]
    assert resolve_window("mornings suit me", settings).hours == (time(9, 0), time(12, 0))
    assert resolve_window("Sounds good", settings) is None


def test_interest_with_next_week_offers_only_next_week(ctx):
    p = acme_awaiting(ctx)
    p = reply(ctx, p, "Thank you for reaching out. This seems interesting. Lets chat next week")
    days = [clock.parse(t["start"]).astimezone(clock.parse("2026-11-17T00:00:00+05:30").tzinfo).date()
            for t in p["times_proposed"]]
    assert len(days) == 3 and all(d in NEXT_WEEK for d in days)
    assert "Since you mentioned next week" in agent_mails(ctx)[-1]["body"]


def test_afternoon_preference_respected(ctx):
    p = acme_awaiting(ctx)
    p = reply(ctx, p, "Interested! Thursday afternoon would be ideal.")
    for t in p["times_proposed"]:
        local = clock.parse(t["start"]).astimezone(__import__("zoneinfo").ZoneInfo("Asia/Kolkata"))
        assert local.date() == date(2026, 11, 19) and 12 <= local.hour < 17


def test_counterproposal_with_only_a_window_reoffers_slots(ctx):
    p = acme_awaiting(ctx)
    p = reply(ctx, p, "Interested, happy to chat.")
    first = {t["start"] for t in p["times_proposed"]}
    p = reply(ctx, p, "None of those work, can we do next week instead?")
    assert p["reply_status"] == "SLOTS_PROPOSED"  # not a clarification request
    assert not first & {t["start"] for t in p["times_proposed"]}


def test_no_preference_keeps_earliest_slots(ctx):
    p = acme_awaiting(ctx)
    p = reply(ctx, p, "Interested, happy to chat.")
    assert "Since you mentioned" not in agent_mails(ctx)[-1]["body"]
    assert p["times_proposed"][0]["start"].startswith("2026-11-18")
