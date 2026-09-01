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

import os
import random
import re
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
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

#: A single completion is bounded so no turn can run away. Without this a poorly-aligned model on
#: an OpenAI-compatible endpoint keeps generating past its own turn — inventing the next `### User:`
#: turn and drifting into unrelated text — because nothing on the wire told it to stop. Generous
#: enough for a phase's whole JSON artifact or one file written through a tool-call argument.
MAX_OUTPUT_TOKENS = 16384

#: Conversation-boundary markers a served chat template leaks when a weak model continues past its
#: own turn. Stopping here cuts the model the instant it starts writing the other side of the
#: conversation. Safe against real output: a JSON artifact or a source file never opens a line with
#: one of these role headers.
STOP_SEQUENCES = ("\n### User:", "\n### Assistant:", "\n### Human:")

#: Fires once per model per process, so a fallback price is visible but not noisy.
_PRICED_BY_FALLBACK: set[str] = set()

#: `_completion_cost()` memo. litellm costs a second to import; this pays it once.
_COST_FN: Callable[..., Any] | None = None
_COST_FN_RESOLVED = False


class ProviderError(RuntimeError):
    """A provider call that could not be completed after retries."""


class _StreamCommitted(Exception):
    """Internal signal, never seen outside this module: a stream failed *after* a delta was
    already forwarded to `on_token`. Its existence tells `_run` that falling over to another
    provider would print a second answer on top of the first, so the turn must fail with the
    original error instead."""

    def __init__(self, original: Exception) -> None:
        self.original = original


class ProviderQuotaError(ProviderError):
    """A provider allowance that resets on a clock, not on a retry.

    Separate from `ProviderError` because the remedy is different in kind: waiting eight
    seconds cannot fix a limit that resets at midnight, and the useful thing to tell someone
    is *when* — and that credit raises the ceiling — not that four attempts failed.
    """


class ProviderAuthError(ProviderError):
    """No usable credentials for this model.

    Split out because it is the one provider failure that is never the provider's fault and
    never worth retrying — a wrong key stays wrong — and because it is what every first run
    hits. A traceback here teaches someone that the tool is broken; a sentence teaches them
    where to put their key.
    """


#: HTTP statuses that mean "your credentials, not our servers".
AUTH_STATUS = frozenset({401, 403})

#: A 429 that says this is a *daily* allowance is not something backoff fixes. Retrying it
#: burns seconds and prints a wall of provider JSON to explain a one-sentence problem.
DAILY_QUOTA_MARKERS = ("per-day", "per_day", "daily", "free-models-per-day")

#: Exception class names litellm raises for the same thing when it has no status code.
AUTH_EXCEPTIONS = frozenset({"AuthenticationError", "PermissionDeniedError"})

#: Which environment variable litellm expects, per model-string prefix. Deliberately partial:
#: an unknown prefix names no variable rather than confidently naming the wrong one.
KEY_FOR_PREFIX: dict[str, str] = {
    "openrouter": "OPENROUTER_API_KEY",
    "nvidia_nim": "NVIDIA_NIM_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "ollama": "",  # local, no key
}


def key_variable_for(model: str) -> str:
    """The environment variable a model string needs, or "" if we do not know."""
    return KEY_FOR_PREFIX.get(model.split("/")[0], "")


def _is_auth(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status in AUTH_STATUS
    return type(exc).__name__ in AUTH_EXCEPTIONS


def _is_daily_quota(exc: Exception) -> bool:
    if getattr(exc, "status_code", None) != 429 and type(exc).__name__ != "RateLimitError":
        return False
    return any(marker in str(exc).lower() for marker in DAILY_QUOTA_MARKERS)


def _quota_error(model: str, exc: Exception) -> ProviderQuotaError:
    """Say when it resets. That is the only fact that changes what someone does next."""
    when = ""
    match = re.search(r'"X-RateLimit-Reset"\s*:\s*"?(\d{10,13})"?', str(exc))
    if match:
        stamp = int(match.group(1))
        moment = datetime.fromtimestamp(stamp / 1000 if stamp > 10**11 else stamp, UTC)
        when = f" It resets at {moment.astimezone().strftime('%H:%M on %d %b')} local time."
    return ProviderQuotaError(
        f"{model}: the provider's allowance for this account is spent, and this is a limit "
        f"that resets on a clock rather than something retrying fixes.{when}\n"
        "Wait for the reset, add credit to raise the ceiling, or run with --model on a "
        "provider you have quota with."
    )


def _auth_error(model: str, exc: Exception) -> ProviderAuthError:
    variable = key_variable_for(model)
    where = (
        f"Set {variable} in your environment, or put it in ~/.loom/credentials.json "
        f'(mode 0600):\n  {{"{variable}": "..."}}'
        if variable
        else "Set the API key environment variable your provider expects."
    )
    return ProviderAuthError(f"no usable credentials for {model}.\n{where}\n\nProvider said: {exc}")


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
        max_tokens: int = MAX_OUTPUT_TOKENS,
        stop: Sequence[str] | None = None,
        acompletion: Callable[..., Any] | None = None,
        sleep: Callable[[float], Any] | None = None,
        jitter: Callable[[float, float], float] | None = None,
        extra: Mapping[str, Any] | None = None,
        on_token: Callable[[str], None] | None = None,
        chunk_builder: Callable[..., Any] | None = None,
        fallback_models: Sequence[str] | None = None,
    ) -> None:
        self.model = model
        # FR-AGENT-08 — models to try, in order, when the one before is down. Empty by default,
        # so the chain is one link long and a run with no fallbacks behaves exactly as before.
        self.fallback_models = list(fallback_models or [])
        self.price_table = dict(price_table or DEFAULT_PRICE_TABLE)
        self.ledger = ledger
        self.phase = phase
        self.run_id = run_id
        self.on_event = on_event
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.stop = list(stop) if stop is not None else list(STOP_SEQUENCES)
        self._acompletion = acompletion
        # FR-REPL-05 — when set, `complete()` streams and forwards each text delta here as it
        # arrives. Left None everywhere except an interactive TTY run, so nothing else changes.
        self.on_token = on_token
        self._chunk_builder = chunk_builder
        if sleep is not None:
            self._sleep = sleep
        else:
            import asyncio

            self._sleep = asyncio.sleep
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
        base_kwargs: dict[str, Any] = {"messages": payload}
        if tools:
            base_kwargs["tools"] = tools
            base_kwargs["tool_choice"] = "auto"
        if self.temperature is not None:
            base_kwargs["temperature"] = self.temperature
        # The turn bound, set before `extra` so a caller can still override either explicitly.
        base_kwargs["max_tokens"] = self.max_tokens
        if self.stop:
            base_kwargs["stop"] = self.stop
        base_kwargs.update(self.extra)

        started = time.perf_counter()
        single = self._stream if self.on_token is not None else self._call_with_retries
        raw, used_model = await self._run(base_kwargs, single)
        seconds = time.perf_counter() - started

        response = to_response(
            raw, model=used_model, price_table=self.price_table, on_event=self.on_event
        )
        if self.ledger is not None:
            self.ledger.record(
                phase=self.phase,
                model=used_model,
                usd=response.usd_cost,
                in_tok=response.in_tokens,
                out_tok=response.out_tokens,
                seconds=seconds,
                run_id=self.run_id,
            )
        return response

    async def _run(
        self,
        base_kwargs: dict[str, Any],
        single: Callable[[dict[str, Any]], Any],
    ) -> tuple[Any, str]:
        """FR-AGENT-08 — walk the model chain (primary, then `fallback_models`), moving to the
        next only when a provider is down, and return the raw payload with the model that answered.

        Streaming carries one extra rule, enforced by `_stream` raising `_StreamCommitted`: a
        stream that fails *after* a delta reached the screen cannot fall over — a second provider
        would print a second answer on top of the first — so that case fails the turn with the
        original error. A pre-first-delta drop, the overload the fallback exists for, moves on.
        """
        chain = [self.model, *self.fallback_models]
        for i, model in enumerate(chain):
            try:
                raw = await single({**base_kwargs, "model": model})
                return raw, model
            except _StreamCommitted as committed:
                raise committed.original from None
            except Exception as exc:  # noqa: BLE001 - re-raised on the last link
                if i == len(chain) - 1:
                    raise
                if self.on_event is not None:
                    self.on_event(
                        "fallback_switch",
                        from_model=model,
                        to_model=chain[i + 1],
                        reason=f"{type(exc).__name__}: {exc}",
                    )
        raise ProviderError("empty model chain")  # unreachable: chain always has the primary

    def _preflight(self, model: str) -> None:
        """Refuse before the wire when the key `model` needs is simply not set.

        Not redundant with the 401 path. Providers disagree about what a *missing* credential
        is — NVIDIA NIM reports it as a 500, which is in `RETRY_STATUS`, so without this it
        would back off four times before failing for a reason no amount of waiting fixes. A
        key that is present but wrong still goes the 401 route.

        Keyed on the model being attempted, not `self.model`: a fallback link may need a
        different key than the primary, and must be checked against its own.

        Only on the real path: an injected `acompletion` is a test or a cassette, and neither
        needs a key.
        """
        if self._acompletion is not None:
            return
        variable = key_variable_for(model)
        if variable and not os.environ.get(variable):
            raise ProviderAuthError(
                f"no usable credentials for {model}.\n"
                f"Set {variable} in your environment, or put it in ~/.loom/credentials.json "
                f'(mode 0600):\n  {{"{variable}": "..."}}'
            )

    async def _stream(self, kwargs: dict[str, Any]) -> Any:
        """FR-REPL-05 — stream the completion, forwarding each text delta to `on_token`, then
        reassemble the whole response so cost, tokens and tool calls flow through `to_response`
        exactly as the non-streaming path does.

        No retry of a half-consumed stream: re-driving one is a second answer, not a retry. But a
        drop *before the first delta* has shown nothing, so `_run` is free to fall over to the
        next provider — the overload this exists for is exactly that case. Once a delta is on
        screen we are committed: a later failure raises `_StreamCommitted` so `_run` fails the
        turn with the original error rather than printing a second answer over the first.
        """
        model = kwargs["model"]
        self._preflight(model)
        call = self._acompletion or _litellm_acompletion()
        build = self._chunk_builder or _litellm_stream_chunk_builder()
        emitted = False
        try:
            stream = await call(**{**kwargs, "stream": True})
            chunks: list[Any] = []
            async for chunk in stream:
                chunks.append(chunk)
                delta = _delta_text(chunk)
                if delta and self.on_token is not None:
                    emitted = True  # past the point of no return — nothing can un-print this
                    self.on_token(delta)
            return build(chunks, messages=kwargs["messages"])
        except Exception as exc:  # noqa: BLE001 - re-raised, possibly wrapped, below
            if emitted:
                raise _StreamCommitted(exc) from exc
            raise

    async def _call_with_retries(self, kwargs: dict[str, Any]) -> Any:
        """FR-AGENT-08 — jittered exponential backoff, bounded, one event per retry."""
        model = kwargs["model"]
        self._preflight(model)
        call = self._acompletion or _litellm_acompletion()
        last: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                return await call(**kwargs)
            except Exception as exc:  # noqa: BLE001 - re-raised below unless it is retryable
                if _is_daily_quota(exc):
                    raise _quota_error(model, exc) from exc
                if _is_auth(exc):
                    # Never retried: four attempts at a key that is missing is four times the
                    # wait for the same answer.
                    raise _auth_error(model, exc) from exc
                if not _is_retryable(exc) or attempt == self.max_retries - 1:
                    raise
                last = exc
                delay = self.base_delay * (2**attempt) * self._jitter(0.5, 1.5)
                if self.on_event is not None:
                    self.on_event(
                        "retry",
                        model=model,
                        attempt=attempt + 1,
                        of=self.max_retries,
                        delay=round(delay, 3),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                await self._sleep(delay)
        raise ProviderError(f"{model} failed after {self.max_retries} attempts") from last

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
    import litellm

    # litellm prints its own provider list and a "give feedback" link to stderr on every error.
    # Loom already turns the useful part into a sentence; the banner is noise on top of it.
    litellm.suppress_debug_info = True
    from litellm import acompletion

    return cast("Callable[..., Any]", acompletion)


def _delta_text(chunk: Any) -> str:
    """The text a streamed chunk carries, or "" for a chunk that carries only a tool-call delta,
    a role marker, or the final usage record."""
    data = _as_dict(chunk)
    choices = data.get("choices") or [{}]
    delta = _as_dict(_as_dict(choices[0]).get("delta") or {})
    text = delta.get("content")
    return text if isinstance(text, str) else ""


def _litellm_stream_chunk_builder() -> Callable[..., Any]:
    """litellm's reassembler — turns the list of streamed chunks back into one response object
    carrying the full text, the tool calls and the usage, which `to_response` then normalises."""
    import litellm

    return cast("Callable[..., Any]", litellm.stream_chunk_builder)
