"""Chat completions over OpenAI-compatible APIs: Groq first, OpenRouter's free models when Groq is limited or down."""

import secrets
from collections.abc import Iterator

import openai

from app.config import settings

RETRYABLE = (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError)


class ProviderDown(Exception):
    pass


def _providers(size: str) -> list[tuple[str, openai.OpenAI, str]]:
    out = []
    if settings.groq_api_key:
        client = openai.OpenAI(base_url=settings.groq_base_url, api_key=settings.groq_api_key, max_retries=1, timeout=60)
        out.append(("groq", client, settings.llm_big if size == "big" else settings.llm_small))
    if settings.openrouter_api_key:
        client = openai.OpenAI(base_url=settings.openrouter_base_url, api_key=settings.openrouter_api_key, max_retries=1, timeout=90)
        out.append(("openrouter", client, settings.llm_fallback))
    return out


class LLM:
    def stream(self, messages: list[dict], tools: list[dict] | None, size: str) -> Iterator[tuple]:
        """Yields ("meta", provider, model), then ("text", piece)..., ("tool", id, name, json_args)..., ("usage", p, c)."""
        failures = []
        for name, client, model in _providers(size):
            args = {"model": model, "messages": messages, "stream": True}
            if tools:
                args["tools"] = tools
            if name == "groq":
                args["stream_options"] = {"include_usage": True}
            try:
                stream = client.chat.completions.create(**args)
            except RETRYABLE as e:  # the SDK already retried once with backoff; now try the next provider
                failures.append(f"{name}: {type(e).__name__}")
                continue
            yield ("meta", name, model)
            calls: dict[int, dict] = {}
            for chunk in stream:
                if getattr(chunk, "usage", None):
                    yield ("usage", chunk.usage.prompt_tokens, chunk.usage.completion_tokens)
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta.content:
                    yield ("text", delta.content)
                for tc in delta.tool_calls or []:  # tool calls arrive in fragments, keyed by index
                    call = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                    call["id"] = tc.id or call["id"]
                    if tc.function:
                        call["name"] = call["name"] or (tc.function.name or "")
                        call["args"] += tc.function.arguments or ""
            for call in calls.values():
                yield ("tool", call["id"] or f"call_{secrets.token_hex(4)}", call["name"], call["args"] or "{}")
            return
        raise ProviderDown("; ".join(failures) or "no LLM provider is configured")
