"""DEMO DELIVERY POLICY — enforced in code at every integration boundary.

* Email "to" must be exactly [DEMO_RECIPIENT_EMAIL]; cc and bcc must be empty.
* Calendar attendees must be exactly [DEMO_RECIPIENT_EMAIL].
* Subjects carry "[DEMO — Company]"; events carry "[DEMO]".
No caller (and no LLM output) can choose a recipient: delivery functions take the
address only from settings, and the integrations re-check it before any I/O.
"""
from __future__ import annotations

from ..errors import DeliveryPolicyViolation


def _norm(addrs) -> list[str]:
    if addrs is None:
        return []
    if isinstance(addrs, str):
        addrs = [a for a in addrs.split(",") if a.strip()]
    return [a.strip().lower() for a in addrs]


def enforce_email_recipients(settings, to, cc=None, bcc=None) -> str:
    demo = settings.delivery_address
    to_l, cc_l, bcc_l = _norm(to), _norm(cc), _norm(bcc)
    if to_l != [demo]:
        raise DeliveryPolicyViolation(f"recipient {to_l} is not the configured demo address")
    if cc_l or bcc_l:
        raise DeliveryPolicyViolation("cc/bcc must be empty in demo delivery")
    return demo


def enforce_attendees(settings, attendees) -> list[str]:
    demo = settings.delivery_address
    if _norm(attendees) != [demo]:
        raise DeliveryPolicyViolation(f"attendees {_norm(attendees)} must be exactly the demo address")
    return [demo]


def demo_subject(company: str, subject: str) -> str:
    prefix = f"[DEMO — {company}]"
    subject = subject.strip()
    if prefix in subject:
        return subject
    if subject.lower().startswith("re:"):
        return "Re: " + prefix + " " + subject[3:].strip()
    return f"{prefix} {subject}"


def enforce_subject(subject: str) -> None:
    if "[DEMO — " not in subject:
        raise DeliveryPolicyViolation("subject is missing the [DEMO — Company] prefix")


def demo_event_summary(summary: str) -> str:
    return summary if summary.startswith("[DEMO]") else f"[DEMO] {summary}"


def enforce_event_summary(summary: str) -> None:
    if not summary.startswith("[DEMO]"):
        raise DeliveryPolicyViolation("calendar event summary must start with [DEMO]")
