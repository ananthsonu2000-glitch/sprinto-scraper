"""Gemini adapter: model ranking and automatic fallback (mocked HTTP, offline)."""
import json

import httpx
import pytest
from pydantic import BaseModel

from sprinto_agent.errors import IntegrationError
from sprinto_agent.llm.gemini_llm import GeminiLLM, rank_models

REAL_LIST = """gemini-2.5-flash gemini-2.5-pro gemini-2.5-flash-preview-tts gemini-2.5-flash-lite gemini-2.5-flash-image
gemini-3-flash-preview gemini-3.1-pro-preview gemini-3.1-pro-preview-customtools gemini-3.1-flash-lite-preview
gemini-3.1-flash-lite gemini-3-pro-image gemini-3.5-flash gemini-3.5-flash-lite gemini-3.5-transcribe gemini-3.6-flash
gemini-3.7-flash gemini-3.8-flash gemini-4-argon gemini-4-carbon gemini-2.5-computer-use-preview-10-2025""".split()


class Probe(BaseModel):
    company: str
    is_software: bool


def ok_json(obj, grounding=None):
    cand = {"content": {"parts": [{"text": json.dumps(obj) if not isinstance(obj, str) else obj}]}, "finishReason": "STOP"}
    if grounding:
        cand["groundingMetadata"] = grounding
    return httpx.Response(200, json={"candidates": [cand]})


def make(handler, chain):
    calls = []

    def h(req):
        model = req.url.path.split("/models/")[1].split(":")[0]
        calls.append(model)
        return handler(model, req)
    llm = GeminiLLM("k", models=chain, transport=httpx.MockTransport(h), cooldown_seconds=300)
    return llm, calls


def test_ranking_prefers_newest_pro_and_skips_special_models():
    r = rank_models(REAL_LIST)
    assert r[0] == "gemini-3.1-pro-preview"
    assert r[1:4] == ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash"]
    assert not any(x in " ".join(r) for x in ("tts", "image", "transcribe", "customtools", "computer-use", "argon"))
    assert r.index("gemini-2.5-pro") > r.index("gemini-3.5-flash")


def test_default_chain_is_best_first_and_includes_fast_lite_models(monkeypatch):
    monkeypatch.setattr(GeminiLLM, "_discover", lambda self: rank_models(REAL_LIST))
    llm = GeminiLLM("k")
    assert llm.chain[0] == "gemini-3.1-pro-preview" and len(llm.chain) == 10
    assert any("lite" in m for m in llm.chain)  # fast models are in the chain as late fallbacks


def test_gemini3_gets_low_thinking_but_search_and_older_models_do_not():
    bodies = {}

    def handler(model, req):
        bodies[model] = json.loads(req.content)
        if model == "gemini-3.6-flash":
            return httpx.Response(503, json={"error": {"message": "busy"}})
        return ok_json({"company": "A", "is_software": True})
    llm, _ = make(handler, ["gemini-3.6-flash", "gemini-2.5-flash"])
    llm.structured("t", "s", {}, Probe)
    assert bodies["gemini-3.6-flash"]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}
    assert "thinkingConfig" not in bodies["gemini-2.5-flash"]["generationConfig"]


@pytest.mark.parametrize("failure", ["503", "429", "404", "timeout", "blocked", "bad_schema", "truncated", "bad_request"])
def test_falls_back_to_next_model_on_any_failure(failure):
    def handler(model, req):
        if model == "best":
            if failure == "timeout":
                raise httpx.ReadTimeout("slow")
            if failure == "blocked":
                return httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})
            if failure == "bad_schema":
                return ok_json({"company": "X"})  # missing is_software
            if failure == "truncated":
                return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "{\"comp"}]}, "finishReason": "MAX_TOKENS"}]})
            code = {"503": 503, "429": 429, "404": 404, "bad_request": 400}[failure]
            return httpx.Response(code, json={"error": {"message": f"simulated {failure}"}})
        return ok_json({"company": "Chargebee", "is_software": True})
    llm, calls = make(handler, ["best", "second"])
    out = llm.structured("t", "sys", {"x": 1}, Probe)
    assert out.company == "Chargebee" and llm.last_model == "second"
    assert calls == ["best", "second"]


def test_failed_model_cools_down_and_is_tried_last():
    def handler(model, req):
        if model == "best":
            return httpx.Response(503, json={"error": {"message": "overloaded"}})
        return ok_json({"company": "A", "is_software": True})
    llm, calls = make(handler, ["best", "second"])
    llm.structured("t", "s", {}, Probe)
    llm.structured("t", "s", {}, Probe)
    assert calls == ["best", "second", "second"]  # second call skips the overloaded model


def test_bad_api_key_stops_without_trying_every_model():
    llm, calls = make(lambda m, r: httpx.Response(400, json={"error": {"message": "API key not valid. Please pass a valid API key."}}),
                      ["a", "b", "c"])
    with pytest.raises(IntegrationError) as e:
        llm.structured("t", "s", {}, Probe)
    assert e.value.kind == "auth" and calls == ["a"]


def test_all_models_failing_raises_with_every_reason():
    llm, calls = make(lambda m, r: httpx.Response(503, json={"error": {"message": "busy"}}), ["a", "b"])
    with pytest.raises(IntegrationError) as e:
        llm.structured("t", "s", {}, Probe)
    assert e.value.kind == "transient" and "a: HTTP 503" in e.value.message and "b: HTTP 503" in e.value.message


def test_web_research_parses_grounding_and_falls_back_when_ungrounded():
    grounding = {"webSearchQueries": ["saas series b"],
                 "groundingChunks": [{"web": {"uri": "https://vertexaisearch.cloud.google.com/r/1", "title": "techcrunch.com"}}],
                 "groundingSupports": [{"segment": {"text": "Acme raised a Series B on October 2, 2026."}, "groundingChunkIndices": [0]}]}

    def handler(model, req):
        body = json.loads(req.content)
        assert body["tools"] == [{"google_search": {}}]
        if model == "best":
            return ok_json("No sources used.")  # answered without grounding -> unusable for evidence
        return ok_json("Acme raised a Series B on October 2, 2026.", grounding)
    llm, calls = make(handler, ["best", "second"])
    out = llm.web_research("sys", "find companies")
    assert calls == ["best", "second"]
    assert out["citations"][0]["cited_text"].startswith("Acme raised") and out["citations"][0]["title"] == "techcrunch.com"
    assert "grounded statement" in out["citations"][0]["kind"]


def test_all_models_busy_retries_on_later_ticks_then_hands_off(ctx):
    from conftest import DEMO  # noqa: F401
    from sprinto_agent.outreach import create_campaign
    from sprinto_agent.worker import Worker
    from sprinto_agent import store

    class BusyLLM:
        simulated = False
        label = "busy"

        def structured(self, *a, **k):
            raise IntegrationError("transient", "all Gemini models failed for extract: 503", "llm")
    ctx.llm = BusyLLM()
    pid = create_campaign(ctx, "t", ["acmecloud.example"])["prospect_ids"][0]
    w = Worker(ctx)
    w.tick()
    p = store.get_prospect(ctx.conn, pid)
    assert p["stage"] == "RESEARCHING" and "busy" in p["last_error"]  # waits, no human needed yet
    w.tick()
    w.tick()
    p = store.get_prospect(ctx.conn, pid)
    assert p["stage"] == "ESCALATED" and "LLM_UNAVAILABLE" in p["escalation_reason"]


def test_schema_is_sent_inline_without_refs():
    seen = {}

    def handler(model, req):
        seen["body"] = json.loads(req.content)
        return ok_json({"fields": []})
    from sprinto_agent.schemas import ExtractionOutput
    llm, _ = make(handler, ["m"])
    llm.structured("extract", "s", {}, ExtractionOutput)
    schema_txt = json.dumps(seen["body"]["generationConfig"]["responseJsonSchema"])
    assert "$ref" not in schema_txt and "$defs" not in schema_txt
    assert seen["body"]["generationConfig"]["responseMimeType"] == "application/json"
