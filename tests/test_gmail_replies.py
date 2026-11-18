"""Real Gmail replies carry the quoted earlier message; only the new text may be judged."""
import base64

import httpx

from sprinto_agent.integrations.mailbox import GmailMailbox, strip_quoted_reply

QUOTED = """Option C please.

On Wed, 18 Nov 2026 at 15:02, Demo Sender <me@example.com> wrote:
> Hi Alex,
>
>   Option A: Thu, 19 Nov 2026, 11:00
>   Option B: Fri, 20 Nov 2026, 11:00
"""


def test_strips_gmail_quote_block():
    assert strip_quoted_reply(QUOTED) == "Option C please."


def test_strips_wrapped_wrote_line_and_outlook_style():
    wrapped = "Sounds good, interested.\n\nOn Wed, 18 Nov 2026 at 15:02, Demo Sender\n<me@example.com> wrote:\n> Option B\n"
    assert strip_quoted_reply(wrapped) == "Sounds good, interested."
    outlook = "Please send info.\n\n-----Original Message-----\nFrom: Demo\nOption A"
    assert strip_quoted_reply(outlook) == "Please send info."


def test_plain_reply_unchanged():
    assert strip_quoted_reply("Not interested, thanks.") == "Not interested, thanks."


def test_gmail_thread_ignores_agent_mail_and_strips_quotes(settings):
    def b64(s):
        return base64.urlsafe_b64encode(s.encode()).decode()

    thread = {"messages": [
        {"id": "m1", "internalDate": "1795000000000", "payload": {"mimeType": "text/plain", "body": {"data": b64("Hi Alex ... Option A ... Option B")},
                                                                   "headers": [{"name": "X-Sprinto-Agent", "value": "1"}, {"name": "Subject", "value": "[DEMO — AcmeCloud] Hi"}]}},
        {"id": "m2", "internalDate": "1795000100000", "payload": {"mimeType": "text/plain", "body": {"data": b64(QUOTED)},
                                                                   "headers": [{"name": "From", "value": "me@example.com"}]}},
    ]}

    class Auth:
        def headers(self):
            return {"Authorization": "Bearer t"}
    gm = GmailMailbox(settings, Auth(), http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=thread))))
    msgs = gm.list_thread("t1")
    assert msgs[0].is_agent and "Option B" in msgs[0].body
    assert not msgs[1].is_agent and msgs[1].body == "Option C please."
