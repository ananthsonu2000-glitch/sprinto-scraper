"""Claude via the official Anthropic SDK.

* structured(): schema-validated structured outputs (messages.parse).
* web_research(): Claude's server-side web search tool, returning the cited
  sources so code can verify every discovered company against what was retrieved.

Server-side refusal fallback is enabled by default (LLM_SERVER_FALLBACK=default);
set LLM_SERVER_FALLBACK=off to disable it.
"""
from __future__ import annotations

from ..errors import ErrorKind, IntegrationError
from .base import T, render_user_prompt

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicLLM:
    simulated = False

    def __init__(self, api_key: str | None, model: str, effort: str = "medium", server_fallback: str = "default"):
        try:
            import anthropic  # imported lazily so simulation runs without it
        except ImportError as e:  # pragma: no cover
            raise IntegrationError(ErrorKind.SETUP_REQUIRED, f"anthropic SDK unavailable: {e}", "llm")
        self._anthropic = anthropic
        # api_key=None -> SDK default credential chain (ANTHROPIC_AUTH_TOKEN, `ant auth login` profile, WIF)
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=2) if api_key else anthropic.Anthropic(max_retries=2)
        self.model = model
        self.effort = effort
        self.server_fallback = server_fallback
        self.label = f"Anthropic {model}"

    @property
    def _fallback_on(self) -> bool:
        return bool(self.server_fallback) and self.server_fallback != "off"

    def _call(self, fn):
        a = self._anthropic
        try:
            return fn()
        except a.AuthenticationError as e:
            raise IntegrationError(ErrorKind.AUTH, f"Anthropic authentication failed: {e}", "llm")
        except a.PermissionDeniedError as e:
            raise IntegrationError(ErrorKind.AUTH, f"Anthropic permission denied: {e}", "llm")
        except a.BadRequestError as e:
            raise IntegrationError(ErrorKind.INVALID_INPUT, f"Anthropic rejected request: {e}", "llm")
        except a.RateLimitError as e:
            raise IntegrationError(ErrorKind.TRANSIENT, f"Anthropic rate limited: {e}", "llm")
        except a.APIStatusError as e:
            kind = ErrorKind.TRANSIENT if e.status_code >= 500 else ErrorKind.PERMANENT
            raise IntegrationError(kind, f"Anthropic API error {e.status_code}", "llm")
        except a.APIConnectionError as e:
            raise IntegrationError(ErrorKind.TRANSIENT, f"Anthropic connection error: {e}", "llm")

    def structured(self, task: str, system: str, context: dict, schema: type[T]) -> T:
        kwargs = dict(
            model=self.model, max_tokens=16000, system=system,
            messages=[{"role": "user", "content": render_user_prompt(context)}],
            output_format=schema, output_config={"effort": self.effort},
        )
        if self._fallback_on:
            resp = self._call(lambda: self.client.beta.messages.parse(
                betas=[FALLBACK_BETA], fallbacks=self.server_fallback, **kwargs))
        else:
            resp = self._call(lambda: self.client.messages.parse(**kwargs))
        if resp.stop_reason == "refusal":
            raise IntegrationError(ErrorKind.PERMANENT, f"model declined task {task}", "llm")
        if resp.stop_reason == "max_tokens":
            raise IntegrationError(ErrorKind.TRANSIENT, f"output truncated for task {task}", "llm")
        parsed = resp.parsed_output
        if parsed is None:
            raise IntegrationError(ErrorKind.PERMANENT, f"no structured output for task {task}", "llm")
        return parsed

    def web_research(self, system: str, prompt: str, context: dict | None = None, max_uses: int = 6) -> dict:
        """Run Claude with the web search server tool. Returns the final text plus every
        search result and citation, so callers can verify claims against sources."""
        tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses}]
        messages: list = [{"role": "user", "content": prompt}]
        results, citations, text_parts, errors = [], [], [], []
        for _ in range(4):  # bounded pause_turn continuations
            kwargs = dict(model=self.model, max_tokens=16000, system=system, messages=messages, tools=tools,
                          output_config={"effort": self.effort})
            if self._fallback_on:
                resp = self._call(lambda: self.client.beta.messages.create(
                    betas=[FALLBACK_BETA], fallbacks=self.server_fallback, **kwargs))
            else:
                resp = self._call(lambda: self.client.messages.create(**kwargs))
            if resp.stop_reason == "refusal":
                raise IntegrationError(ErrorKind.PERMANENT, "model declined the web research task", "llm")
            for b in resp.content:
                btype = getattr(b, "type", "")
                if btype == "web_search_tool_result":
                    content = getattr(b, "content", None)
                    if isinstance(content, list):
                        for r in content:
                            results.append({"url": getattr(r, "url", ""), "title": getattr(r, "title", ""),
                                            "page_age": getattr(r, "page_age", None)})
                    elif content is not None:
                        errors.append(str(getattr(content, "error_code", "search_error")))
                elif btype == "text":
                    text_parts.append(getattr(b, "text", ""))
                    for c in getattr(b, "citations", None) or []:
                        if getattr(c, "type", "") == "web_search_result_location":
                            citations.append({"url": c.url, "title": getattr(c, "title", ""),
                                              "cited_text": getattr(c, "cited_text", "")})
            if resp.stop_reason != "pause_turn":
                break
            messages = [{"role": "user", "content": prompt}, {"role": "assistant", "content": resp.content}]
        ages = {r["url"]: r["page_age"] for r in results}
        for c in citations:
            c["page_age"] = ages.get(c["url"])
        return {"text": "\n".join(text_parts), "results": results, "citations": citations, "errors": errors}
