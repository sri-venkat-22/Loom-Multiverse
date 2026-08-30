"""The one place Loom talks to a model. FR-AGENT-07/08/09, FR-COST-02, NFR-MAINT-03.

Everything provider-shaped lives behind this file: message normalisation, tool-call quirks,
retries, prompt-cache breakpoints, and pricing. The loop, the phases and the rubric all see
`contracts.Response` and nothing else.

Three of the four bugs in the blueprint addendum's sample loop are fixed here rather than there:

* `litellm.completion_cost()` raises on unpriced models — extremely common for `openrouter/*`
  and `dashscope/*` — so it is wrapped and falls back to a price table. An unpriced model must
  never raise *after* the tokens are already paid for (FR-COST-02).
* `message.model_dump()` round-trips badly: litellm dumps carry `None` fields and provider
  extras that the next request rejects. Every outgoing message is rebuilt from scratch.
* Tool arguments arrive as a JSON *string* from Qwen, intermittently. `ToolCall` coerces them,
  and this file makes sure they leave the same way.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

from loom.config import DEFAULT_PRICE_TABLE, Price
from loom.contracts import Response, ToolCall
from loom.ledger import Ledger

#: Retried with backoff. Everything else is the caller's problem and fails immediately: a 400 is
#: a bug in the request and retrying it just costs time.
RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

#: litellm raises typed exceptions that do not always carry `status_code`.
RETRY_EXCEPTIONS = frozenset(
    {
        "RateLimitError",
        "ServiceUnavailableError",
        "InternalServerError",
        "APIConnectionError",
        "APIError",
        "Timeout",
        "APITimeoutError",
    }
)

#: Only these keys go back to a provider. `None` values are dropped entirely.
MESSAGE_KEYS = ("role", "content", "tool_calls", "tool_call_id", "name")

#: Models that support explicit prompt-cache breakpoints. DashScope caches implicitly and needs
#: no marker; OpenAI-compatible endpoints ignore one.
CACHE_CONTROL_PREFIXES = ("anthropic/", "claude-", "bedrock/anthropic")

#: Fires once per model per process, so a fallback price is visible but not noisy.
_PRICED_BY_FALLBACK: set[str] = set()

#: `_completion_cost()` memo. litellm costs a second to import; this pays it once.
_COST_FN: Callable[..., Any] | None = None
_COST_FN_RESOLVED = False


class ProviderError(RuntimeError):
    """A provider call that could not be completed after retries."""


# --------------------------------------------------------------------------- FR-AGENT-07


def normalise_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild the transcript with only the keys a provider accepts, and no nulls.

    Not a copy-with-edits: a fresh dict per message, because the failure this prevents is a
    provider-specific extra field surviving a round trip and 400-ing the *next* request.
    """
    out: list[dict[str, Any]] = []
    for message in messages:
        clean: dict[str, Any] = {}
        for key in MESSAGE_KEYS:
            value = message.get(key)
            if value is None:
                continue
            if key == "tool_calls":
                calls = _normalise_tool_calls(value)
                if not calls:
                    continue
                clean[key] = calls
            else:
                clean[key] = value
        if "content" not in clean and "tool_calls" not in clean:
            clean["content"] = ""  # a message with neither is rejected by most providers
        out.append(clean)
    return out


def _normalise_tool_calls(calls: Any) -> list[dict[str, Any]]:
    normalised: list[dict[str, Any]] = []
    for index, call in enumerate(calls or []):
        data = call if isinstance(call, Mapping) else getattr(call, "__dict__", {})
        function = data.get("function") or {}
        if not isinstance(function, Mapping):
            function = getattr(function, "__dict__", {})
        arguments = function.get("arguments", "{}")
        if not isinstance(arguments, str):
            arguments = _dumps(arguments)
        normalised.append(
            {
                "id": str(data.get("id") or f"call_{index}"),
                "type": "function",
                "function": {"name": str(function.get("name", "")), "arguments": arguments},
            }
        )
    return normalised


def _dumps(value: Any) -> str:
    import json

    return json.dumps(value)


# --------------------------------------------------------------------------- raw -> Response


def to_response(
    raw: Any,
    *,
    model: str,
    price_table: Mapping[str, Price] | None = None,
    on_event: Callable[..., Any] | None = None,
) -> Response:
    """Normalise one provider payload. Pure — no network, which is what lets a cassette replay
    a recorded payload through exactly this code and catch shape drift for free (FR-EVAL-03)."""
    data = _as_dict(raw)
    choices = data.get("choices") or [{}]
    message = _as_dict(choices[0]).get("message") or {}
    message = _as_dict(message)

    text = message.get("content")
    if isinstance(text, list):  # Anthropic-style content blocks, when litellm passes them through
        text = "".join(b.get("text", "") for b in (_as_dict(x) for x in text))

    tool_calls = [
        ToolCall(
            id=str(_as_dict(c).get("id") or f"call_{i}"),
            name=str(_as_dict(_as_dict(c).get("function") or {}).get("name", "")),
            arguments=_as_dict(_as_dict(c).get("function") or {}).get("arguments") or {},
        )
        for i, c in enumerate(message.get("tool_calls") or [])
    ]

    usage = _as_dict(data.get("usage") or {})
    in_tokens = int(usage.get("prompt_tokens") or 0)
    out_tokens = int(usage.get("completion_tokens") or 0)

    return Response(
        text=text or None,
        tool_calls=tool_calls,
        usd_cost=cost_of(
            raw,
            model=model,
            in_tokens=in_tokens,
            out_tokens=out_tokens,
            price_table=price_table,
            on_event=on_event,
        ),
        in_tokens=in_tokens,
        out_tokens=out_tokens,
        raw=data,
    )


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        result = dump()
        return dict(result) if isinstance(result, Mapping) else {}
    return dict(getattr(value, "__dict__", {}) or {})


# --------------------------------------------------------------------------- FR-COST-02


def cost_of(
    raw: Any,
    *,
    model: str,
    in_tokens: int,
    out_tokens: int,
    price_table: Mapping[str, Price] | None = None,
    on_event: Callable[..., Any] | None = None,
) -> float:
    """USD for one call. Never raises — an unpriced model is a reporting problem, and killing a
    build over it after the tokens are already paid for is the worst of both outcomes."""
    measure = _completion_cost()
    if measure is not None:
        try:
            measured = measure(completion_response=raw)
            if measured:
                return float(measured)
        except Exception:  # noqa: BLE001 - litellm raises bare Exception for an unmapped model
            pass
    return _fallback_cost(
        model=model,
        in_tokens=in_tokens,
        out_tokens=out_tokens,
        price_table=price_table,
        on_event=on_event,
    )


def _fallback_cost(
    *,
    model: str,
    in_tokens: int,
    out_tokens: int,
    price_table: Mapping[str, Price] | None,
    on_event: Callable[..., Any] | None,
) -> float:
    table = dict(price_table or DEFAULT_PRICE_TABLE)
    price = table.get(model) or table.get(model.split("/")[-1])
    first_time = model not in _PRICED_BY_FALLBACK
    _PRICED_BY_FALLBACK.add(model)

    if first_time and on_event is not None:
        # `budget_warning` rather than a new event kind: what this warns about is that the
        # budget's *numbers* are estimated rather than measured.
        on_event(
            "budget_warning",
            reason="price_table_fallback",
            model=model,
            priced=price is not None,
        )
    if price is None:
        return 0.0
    return in_tokens / 1e6 * price.input_per_mtok + out_tokens / 1e6 * price.output_per_mtok


# --------------------------------------------------------------------------- the adapter


class LiteLLMProvider:
    """`contracts.Provider`, backed by litellm.

    `acompletion` is injected so the retry and pricing paths are unit-testable with no network
    and no key; in production it is `litellm.acompletion`, imported lazily so that importing
    this module does not cost the CLI its 400 ms cold-start budget (NFR-PERF-01).
    """

    def __init__(
        self,
        model: str,
        *,
        price_table: Mapping[str, Price] | None = None,
        ledger: Ledger | None = None,
        phase: str = "",
        run_id: str = "",
        on_event: Callable[..., Any] | None = None,
        max_retries: int = 4,
        base_delay: float = 0.5,
        temperature: float | None = None,
        acompletion: Callable[..., Any] | None = None,
        sleep: Callable[[float], Any] | None = None,
        jitter: Callable[[float, float], float] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        self.model = model
        self.price_table = dict(price_table or DEFAULT_PRICE_TABLE)
        self.ledger = ledger
        self.phase = phase
        self.run_id = run_id
        self.on_event = on_event
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.temperature = temperature
        self._acompletion = acompletion
        self._sleep = sleep or asyncio.sleep
        # Injected so a test can assert the *growth* exactly instead of asserting across two
        # overlapping random bands, which is a flake waiting for a bad CI day.
        self._jitter = jitter or random.uniform
        self.extra = dict(extra or {})

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> Response:
        payload = self._apply_cache_control(normalise_messages(messages))
        kwargs: dict[str, Any] = {"model": self.model, "messages": payload}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        kwargs.update(self.extra)

        started = time.perf_counter()
        raw = await self._call_with_retries(kwargs)
        seconds = time.perf_counter() - started

        response = to_response(
            raw, model=self.model, price_table=self.price_table, on_event=self.on_event
        )
        if self.ledger is not None:
            self.ledger.record(
                phase=self.phase,
                model=self.model,
                usd=response.usd_cost,
                in_tok=response.in_tokens,
                out_tok=response.out_tokens,
                seconds=seconds,
                run_id=self.run_id,
            )
        return response

    async def _call_with_retries(self, kwargs: dict[str, Any]) -> Any:
        """FR-AGENT-08 — jittered exponential backoff, bounded, one event per retry."""
        call = self._acompletion or _litellm_acompletion()
        last: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                return await call(**kwargs)
            except Exception as exc:  # noqa: BLE001 - re-raised below unless it is retryable
                if not _is_retryable(exc) or attempt == self.max_retries - 1:
                    raise
                last = exc
                delay = self.base_delay * (2**attempt) * self._jitter(0.5, 1.5)
                if self.on_event is not None:
                    self.on_event(
                        "retry",
                        model=self.model,
                        attempt=attempt + 1,
                        of=self.max_retries,
                        delay=round(delay, 3),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                await self._sleep(delay)
        raise ProviderError(f"{self.model} failed after {self.max_retries} attempts") from last

    def _apply_cache_control(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """FR-AGENT-09 — the prompt-cache breakpoint, owned here and nowhere else.

        The system block is the long, stable part of every phase's request, so it is the one
        worth caching. Note this is *provider* prompt caching, not litellm's response cache.
        """
        if not any(self.model.startswith(p) or p in self.model for p in CACHE_CONTROL_PREFIXES):
            return messages
        for message in messages:
            if message.get("role") == "system" and isinstance(message.get("content"), str):
                message["content"] = [
                    {
                        "type": "text",
                        "text": message["content"],
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
                break
        return messages


def _is_retryable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status in RETRY_STATUS
    return type(exc).__name__ in RETRY_EXCEPTIONS


def _use_local_price_map() -> None:
    """litellm fetches its price map from GitHub at import time unless told not to.

    Loom makes no unannounced outbound call, and a stale local map is precisely what the
    fallback table in `config.DEFAULT_PRICE_TABLE` exists to cover.
    """
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")


def _completion_cost() -> Callable[..., Any] | None:
    """litellm's cost function, imported at most once. `None` if litellm is not importable.

    Cached both ways: a *failed* import is not cached by Python, so without this every call
    would re-pay a second of import time to fail again.
    """
    global _COST_FN, _COST_FN_RESOLVED
    if not _COST_FN_RESOLVED:
        _COST_FN_RESOLVED = True
        _use_local_price_map()
        try:
            from litellm import completion_cost

            _COST_FN = completion_cost
        except Exception:  # noqa: BLE001 - a missing optional import is not a build failure
            _COST_FN = None
    return _COST_FN


def _litellm_acompletion() -> Callable[..., Any]:
    _use_local_price_map()
    from litellm import acompletion

    return cast("Callable[..., Any]", acompletion)
