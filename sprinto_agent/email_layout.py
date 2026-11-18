"""Consistent plain-text email layout.

  Hi Alex,
  <blank line>
  Opening fact.
  <blank line>
  Paragraph(s).
  <blank line>
  Question?
  <blank line>
  Best,
  Sender

`compose()` builds an email from parts. `ensure_layout()` is a send-time safety net: an
email body that arrives as one unbroken block is split into greeting, paragraphs and
sign-off so nothing goes out as a wall of text.
"""
from __future__ import annotations

import re

SIGNOFF_RE = re.compile(r"\s*(Best(?: regards)?|Kind regards|Regards|Thanks|Thank you|Cheers|Warm regards),?\s*\n?\s*([^\n.?!]{0,60})\s*$", re.I)
GREETING_RE = re.compile(r"^\s*((?:Hi|Hello|Hey|Dear)\s+[^,\n]{1,40},)\s*", re.I)
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(])")


def _clean(s: str) -> str:
    return re.sub(r"[ \t]+", " ", (s or "").strip())


def compose(greeting: str, opening: str, paragraphs: list[str], question: str, sender: str) -> str:
    g = _clean(greeting) or "Hi,"
    if not g.endswith(","):
        g = g.rstrip(".!:;") + ","
    # strip any greeting/sign-off the model put inside the content parts
    blocks = [_clean(opening)] + [_clean(p) for p in paragraphs]
    blocks = [SIGNOFF_RE.sub("", GREETING_RE.sub("", b)).strip() for b in blocks]
    blocks = [b for b in blocks if b]
    q = _clean(question)
    return "\n\n".join([g, *blocks, q, f"Best,\n{sender}"] if q else [g, *blocks, f"Best,\n{sender}"])


def ensure_layout(body: str) -> str:
    """Leave well-formed emails alone; reflow an unbroken block into a proper layout."""
    text = (body or "").replace("\r\n", "\n").strip()
    if "\n\n" in text or len(text) < 120:
        return text
    greeting = ""
    m = GREETING_RE.match(text)
    if m:
        greeting, text = m.group(1), text[m.end():]
    signoff = ""
    s = SIGNOFF_RE.search(text)
    if s:
        word, name = s.group(1), s.group(2).strip()
        signoff = f"{word.capitalize() if word.islower() else word},\n{name}".rstrip()
        text = text[: s.start()].rstrip()
    sentences = [x.strip() for x in SENT_SPLIT.split(text) if x.strip()]
    question = ""
    if sentences and sentences[-1].endswith("?"):
        question = sentences.pop()
    paras: list[str] = []
    if sentences:
        paras.append(sentences[0])  # opening fact on its own
        rest = sentences[1:]
        for i in range(0, len(rest), 2):  # then short paragraphs of up to two sentences
            paras.append(" ".join(rest[i:i + 2]))
    blocks = [b for b in [greeting, *paras, question, signoff] if b]
    return "\n\n".join(blocks)
