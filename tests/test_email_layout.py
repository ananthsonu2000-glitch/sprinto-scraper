"""Every email must look like an email: greeting, spaced paragraphs, question, sign-off."""
from helpers import agent_mails, reply, run_campaign

from sprinto_agent.email_layout import compose, ensure_layout

WALL = ("Hi Alex, AcmeCloud is built for regulated and enterprise organizations that require significant control over "
        "data handling and storage. Given that you serve enterprise clients like Globex Bank and Initech Health, managing the "
        "volume of security reviews and documentation requests might be a complex process to navigate efficiently. "
        "Sprinto helps teams share security and compliance information with customers, which can support responses to "
        "customer security reviews. Would you be open to exploring how this could streamline your GRC workflows? "
        "Best, Demo Sender")


def blocks(body):
    return body.split("\n\n")


def test_wall_of_text_is_reflowed():
    b = blocks(ensure_layout(WALL))
    assert b[0] == "Hi Alex,"
    assert b[1].startswith("AcmeCloud is built")
    assert b[-2].endswith("GRC workflows?")
    assert b[-1] == "Best,\nDemo Sender"
    assert len(b) >= 5


def test_well_formed_email_untouched():
    good = "Hi Alex,\n\nLine one.\n\nWould that help?\n\nBest,\nDemo Sender"
    assert ensure_layout(good) == good


def test_compose_strips_stray_greetings_and_signoffs():
    body = compose("Hi Alex", "Hi Alex, AcmeCloud serves banks.", ["It may be busy. Best, Bob"], "Open to a chat?", "Demo Sender")
    assert blocks(body) == ["Hi Alex,", "AcmeCloud serves banks.", "It may be busy.", "Open to a chat?", "Best,\nDemo Sender"]


def test_every_outgoing_email_is_structured(ctx):
    p = run_campaign(ctx, ["acmecloud.example"])["acmecloud.example"]
    p = reply(ctx, p, "Thanks, I'm interested — happy to chat.")
    reply(ctx, p, "Option A works")
    mails = agent_mails(ctx)
    assert len(mails) == 3
    for m in mails:
        b = blocks(m["body"])
        assert b[0].startswith("Hi ") and b[0].endswith(","), m["body"]
        assert b[-1].startswith("Best,\n"), m["body"]
        assert len(b) >= 3
