"""FR-AGENT-07/08/09, FR-COST-02, FR-EVAL-03 — the adapter, and the cassettes that outlive it.

Tiers: everything here is unit or cassette (no network, runs in CI). The one `@pytest.mark.live`
test at the bottom is the recorder behind `make cassettes`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from loom.agent import providers
from loom.agent.loop import run_agent_loop
from loom.agent.providers import (
    MAX_OUTPUT_TOKENS,
    LiteLLMProvider,
    ProviderAuthError,
    ProviderError,
    ProviderQuotaError,
    cost_of,
    key_variable_for,
    normalise_messages,
    to_response,
)
from loom.agent.tools.registry import ToolRegistry, tool
from loom.config import MODEL_TIERS, Price
from loom.ledger import Ledger
from loom.testing.cassette import CassetteExhausted, CassetteProvider, load_cassette

CASSETTES = Path(__file__).resolve().parent / "cassettes"
ANTHROPIC = CASSETTES / "anthropic_claude_sonnet.json"
QWEN = CASSETTES / "openrouter_qwen3_coder.json"


@pytest.fixture(autouse=True)
def forget_price_warnings() -> None:
    """The fallback warns once *per process*; each test wants a fresh process's worth."""
    providers._PRICED_BY_FALLBACK.clear()


# --------------------------------------------------------------------------- FR-AGENT-07


def test_messages_are_rebuilt_with_only_the_keys_a_provider_accepts() -> None:
    """A litellm dump carries `None` fields and provider extras; sending them back 400s."""
    out = normalise_messages(
        [
            {
                "role": "assistant",
                "content": None,
                "function_call": None,
                "annotations": [],
                "provider_specific_fields": {"x": 1},
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "echo", "arguments": '{"text": "hi"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "name": "echo", "content": "hi", "extra": 9},
        ]
    )
    assert out[0] == {
        "role": "assistant",
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "echo", "arguments": '{"text": "hi"}'},
            }
        ],
    }
    assert out[1] == {"role": "tool", "content": "hi", "tool_call_id": "c1", "name": "echo"}


def test_tool_call_arguments_leave_as_a_json_string() -> None:
    """They arrive both ways; they must go back out one way."""
    out = normalise_messages(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "c1", "function": {"name": "echo", "arguments": {"text": "hi"}}}
                ],
            }
        ]
    )
    assert out[0]["tool_calls"][0]["function"]["arguments"] == '{"text": "hi"}'


def test_a_tool_call_with_no_id_gets_one() -> None:
    out = normalise_messages(
        [{"role": "assistant", "tool_calls": [{"function": {"name": "echo", "arguments": "{}"}}]}]
    )
    assert out[0]["tool_calls"][0]["id"] == "call_0"


def test_a_message_with_neither_content_nor_tools_still_has_content() -> None:
    assert normalise_messages([{"role": "assistant", "content": None}]) == [
        {"role": "assistant", "content": ""}
    ]


def test_an_empty_tool_calls_list_is_dropped_rather_than_sent() -> None:
    assert normalise_messages([{"role": "assistant", "content": "hi", "tool_calls": []}]) == [
        {"role": "assistant", "content": "hi"}
    ]


# --------------------------------------------------------------------------- FR-EVAL-03


def test_the_anthropic_cassette_normalises_to_a_response() -> None:
    entries = load_cassette(ANTHROPIC)
    first = to_response(entries[0]["raw"], model="anthropic/claude-sonnet-5")
    assert first.text is None
    assert [c.name for c in first.tool_calls] == ["read_file"]
    assert first.tool_calls[0].arguments == {"path": "README.md"}
    assert first.tool_calls[0].id == "toolu_01A9FjkPqR"
    assert (first.in_tokens, first.out_tokens) == (1523, 84)
    assert first.usd_cost > 0


def test_the_qwen_cassette_survives_all_three_of_its_quirks() -> None:
    """Arguments as a JSON string, a tool call with no id, and a response with no usage block.

    None of these are visible to FakeLLM, which is the entire reason this tier exists.
    """
    entries = load_cassette(QWEN)
    first = to_response(entries[0]["raw"], model="openrouter/qwen/qwen3-coder")
    assert first.tool_calls[0].arguments == {"command": "pytest -q"}  # was a string
    assert first.tool_calls[0].id == "call_0"  # was absent
    assert first.text is None  # was ""

    second = to_response(entries[1]["raw"], model="openrouter/qwen/qwen3-coder")
    assert second.text == "Done — 6 tests pass."
    assert (second.in_tokens, second.out_tokens) == (0, 0)  # no usage block at all
    assert second.usd_cost == 0.0  # and so, honestly, no cost


def test_both_providers_produce_the_same_response_shape() -> None:
    """WP-2.7a's done-when, stated as a test: one Anthropic model and one Qwen model reduce to
    identical `Response` shapes, or the loop has two code paths and only knows about one."""
    shapes = []
    for path, model in (
        (ANTHROPIC, "anthropic/claude-sonnet-5"),
        (QWEN, "openrouter/qwen/qwen3-coder"),
    ):
        response = to_response(load_cassette(path)[0]["raw"], model=model)
        shapes.append(
            {k: type(v).__name__ for k, v in response.model_dump(exclude={"raw", "text"}).items()}
        )
    assert shapes[0] == shapes[1]


async def test_a_cassette_replays_in_order_and_runs_out_loudly() -> None:
    provider = CassetteProvider.from_file(QWEN)
    first = await provider.complete([{"role": "user", "content": "go"}])
    assert first.tool_calls[0].name == "run_bash"
    await provider.complete([{"role": "user", "content": "go"}])
    assert provider.remaining == 0
    with pytest.raises(CassetteExhausted, match="make cassettes"):
        await provider.complete([{"role": "user", "content": "go"}])


async def test_a_cassette_drives_the_real_loop(tmp_path: Path) -> None:
    """The end the cassettes exist for: recorded bytes, real adapter, real loop, no network."""
    from loom.agent.tools.bash import bash_tool

    result = await run_agent_loop(
        provider=CassetteProvider.from_file(QWEN),
        system="you build software",
        task="make the tests pass",
        tools=ToolRegistry([bash_tool(tmp_path)]),
        max_turns=5,
        max_usd=1.0,
    )
    assert result.status == "passed"
    assert [m["role"] for m in result.messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert "exit" in result.messages[3]["content"]  # the recorded `pytest -q` really ran


# --------------------------------------------------------------------------- FR-COST-02


def test_an_unpriced_model_falls_back_to_the_price_table_instead_of_raising() -> None:
    """`litellm.completion_cost()` raises on `dashscope/*`. After the tokens are already paid
    for is the worst possible moment to find that out."""
    events: list[tuple[str, dict[str, Any]]] = []
    raw = {
        "model": "qwen3-coder-next",
        "choices": [{"message": {"content": "hi"}}],
        "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
    }
    cost = to_response(
        raw,
        model="dashscope/qwen3-coder-next",
        price_table={"dashscope/qwen3-coder-next": Price(input_per_mtok=0.3, output_per_mtok=0.6)},
        on_event=lambda kind, **f: events.append((kind, f)),
    ).usd_cost

    assert cost == pytest.approx(0.9)
    assert events == [
        (
            "budget_warning",
            {
                "reason": "price_table_fallback",
                "model": "dashscope/qwen3-coder-next",
                "priced": True,
            },
        )
    ]


def test_a_model_in_no_table_at_all_costs_zero_and_still_does_not_raise() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    response = to_response(
        {
            "model": "who/knows",
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10},
        },
        model="who/knows",
        price_table={},
        on_event=lambda kind, **f: events.append((kind, f)),
    )
    assert response.usd_cost == 0.0
    assert events[0][1]["priced"] is False


def test_the_fallback_warns_once_per_model_not_once_per_call() -> None:
    events: list[str] = []
    raw = {
        "model": "x",
        "choices": [{"message": {"content": "hi"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10},
    }
    for _ in range(3):
        to_response(
            raw,
            model="dashscope/unknown",
            price_table={},
            on_event=lambda kind, **f: events.append(kind),
        )
    assert events == ["budget_warning"]


def test_a_priced_model_uses_the_measured_cost_not_the_table() -> None:
    """When litellm can price it, its number wins — the table is a fallback, not an override."""
    response = to_response(
        {
            "model": "gpt-4o-mini",
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 1000},
        },
        model="gpt-4o-mini",
        price_table={"gpt-4o-mini": Price(input_per_mtok=999.0, output_per_mtok=999.0)},
    )
    assert 0 < response.usd_cost < 1.0


# --------------------------------------------------------------------------- FR-AGENT-08


class Throttled(Exception):
    status_code = 429


class BadRequest(Exception):
    status_code = 400


def flaky(*failures: Exception, then: dict[str, Any] | None = None) -> Any:
    queue = list(failures)
    calls: list[dict[str, Any]] = []

    async def acompletion(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        if queue:
            raise queue.pop(0)
        return then or {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    acompletion.calls = calls  # type: ignore[attr-defined]
    return acompletion


def recording_sleep() -> Any:
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    sleep.delays = delays  # type: ignore[attr-defined]
    return sleep


async def test_a_429_is_retried_with_backoff_and_one_event_each() -> None:
    events: list[dict[str, Any]] = []
    sleep = recording_sleep()
    call = flaky(Throttled("slow down"), Throttled("slow down"))
    provider = LiteLLMProvider(
        "openrouter/qwen/qwen3-coder",
        acompletion=call,
        sleep=sleep,
        on_event=lambda kind, **f: events.append({"kind": kind, **f}),
        base_delay=1.0,
        jitter=lambda low, high: 1.0,
    )

    response = await provider.complete([{"role": "user", "content": "go"}])

    assert response.text == "ok"
    assert len(call.calls) == 3
    retries = [e for e in events if e["kind"] == "retry"]
    assert len(retries) == 2
    assert [e["attempt"] for e in retries] == [1, 2]
    assert all(e["of"] == 4 and "Throttled" in e["error"] for e in retries)
    assert sleep.delays == [1.0, 2.0]  # exponential, with the jitter pinned


async def test_the_backoff_is_jittered_within_its_band() -> None:
    """Every retry in a fleet waking at the same instant is how one 429 becomes an outage."""
    sleep = recording_sleep()
    provider = LiteLLMProvider(
        "m", acompletion=flaky(*[Throttled("no") for _ in range(3)]), sleep=sleep, base_delay=1.0
    )
    await provider.complete([{"role": "user", "content": "go"}])
    assert len(sleep.delays) == 3
    assert not any(d in (1.0, 2.0, 4.0) for d in sleep.delays), "the jitter is not applied"
    for attempt, delay in enumerate(sleep.delays):
        assert 0.5 * 2**attempt <= delay <= 1.5 * 2**attempt


async def test_a_400_is_not_retried() -> None:
    sleep = recording_sleep()
    call = flaky(BadRequest("your tools are malformed"))
    provider = LiteLLMProvider("m", acompletion=call, sleep=sleep)
    with pytest.raises(BadRequest):
        await provider.complete([{"role": "user", "content": "go"}])
    assert len(call.calls) == 1 and sleep.delays == []


async def test_retries_are_bounded() -> None:
    sleep = recording_sleep()
    call = flaky(*[Throttled("no") for _ in range(10)])
    provider = LiteLLMProvider("m", acompletion=call, sleep=sleep, max_retries=3)
    with pytest.raises(Throttled):
        await provider.complete([{"role": "user", "content": "go"}])
    assert len(call.calls) == 3, "a bounded retry that is not bounded is an outage amplifier"


async def test_an_untyped_rate_limit_is_recognised_by_class_name() -> None:
    """litellm's own exceptions do not all carry `status_code`."""

    class RateLimitError(Exception):
        pass

    call = flaky(RateLimitError("429"))
    provider = LiteLLMProvider("m", acompletion=call, sleep=recording_sleep())
    assert (await provider.complete([{"role": "user", "content": "go"}])).text == "ok"


def test_provider_error_exists_for_the_unreachable_branch() -> None:
    assert issubclass(ProviderError, RuntimeError)


# --------------------------------------------------------------------------- FR-AGENT-09


async def test_anthropic_gets_a_prompt_cache_breakpoint_on_the_system_block() -> None:
    call = flaky()
    provider = LiteLLMProvider("anthropic/claude-sonnet-5", acompletion=call)
    await provider.complete(
        [{"role": "system", "content": "long stable prompt"}, {"role": "user", "content": "go"}]
    )
    system = call.calls[0]["messages"][0]
    assert system["content"] == [
        {"type": "text", "text": "long stable prompt", "cache_control": {"type": "ephemeral"}}
    ]


async def test_a_qwen_model_is_left_alone() -> None:
    """DashScope caches implicitly; an OpenAI-compatible endpoint would just be confused."""
    call = flaky()
    provider = LiteLLMProvider("openrouter/qwen/qwen3-coder", acompletion=call)
    await provider.complete([{"role": "system", "content": "long stable prompt"}])
    assert call.calls[0]["messages"][0]["content"] == "long stable prompt"


# --------------------------------------------------------------------------- wiring


async def test_tools_are_passed_through_and_no_litellm_specific_kwarg_is() -> None:
    """The addendum's `caching=True` is litellm's *response* cache — in an agent loop it can
    replay one tool call forever."""

    @tool
    def echo(text: str) -> str:
        """Echo."""
        return text

    call = flaky()
    provider = LiteLLMProvider("m", acompletion=call)
    await provider.complete([{"role": "user", "content": "go"}], ToolRegistry([echo]).specs())

    sent = call.calls[0]
    assert sent["tools"][0]["function"]["name"] == "echo"
    assert sent["tool_choice"] == "auto"
    assert "caching" not in sent


async def test_every_completion_is_bounded_and_stops_at_a_fabricated_turn() -> None:
    """The runaway guard: a weak model that keeps generating past its own turn — inventing the
    next `### User:` turn and drifting into unrelated text — is cut at the wire. `extra` still wins
    so a caller can raise or lower the bound."""
    call = flaky()
    provider = LiteLLMProvider("m", acompletion=call)
    await provider.complete([{"role": "user", "content": "go"}])
    sent = call.calls[0]
    assert sent["max_tokens"] == MAX_OUTPUT_TOKENS
    assert "\n### User:" in sent["stop"]

    override = flaky()
    bounded = LiteLLMProvider("m", acompletion=override, extra={"max_tokens": 512})
    await bounded.complete([{"role": "user", "content": "go"}])
    assert override.calls[0]["max_tokens"] == 512


async def test_every_call_is_ledgered_under_its_phase(tmp_path: Path) -> None:
    """Ground rule 8 — every WP that touches a model call records its cost."""
    ledger = Ledger(tmp_path / "ledger.db")
    provider = LiteLLMProvider(
        "openrouter/qwen/qwen3-coder",
        acompletion=flaky(
            then={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 500, "completion_tokens": 100},
            }
        ),
        ledger=ledger,
        phase="build",
        run_id="run-1",
    )
    await provider.complete([{"role": "user", "content": "go"}])

    row = ledger.rows()[0]
    assert (row["phase"], row["model"], row["run_id"]) == (
        "build",
        "openrouter/qwen/qwen3-coder",
        "run-1",
    )
    assert (row["in_tok"], row["out_tok"]) == (500, 100)
    assert row["seconds"] > 0
    assert ledger.by_phase() == {"build": pytest.approx(row["usd"])}


def test_it_satisfies_the_provider_protocol() -> None:
    from loom.contracts import Provider

    assert isinstance(LiteLLMProvider("m"), Provider)
    assert isinstance(CassetteProvider([]), Provider)


# --------------------------------------------------------------------------- make cassettes


@pytest.mark.live
async def test_record_cassettes() -> None:
    """`make cassettes` — real calls, real money, one file per model.

    Overwrites the fixtures above with genuine recordings. Everything else in this file then
    runs against those, unchanged.
    """
    from loom.testing.cassette import Recorder

    @tool
    def read_file(path: str) -> str:
        """Read a file."""
        return "# A URL shortener\n"

    tools = ToolRegistry([read_file])
    for model, path in (
        ("anthropic/claude-sonnet-5", ANTHROPIC),
        ("openrouter/qwen/qwen3-coder", QWEN),
    ):
        recorder = Recorder(LiteLLMProvider(model), path, model=model)
        await run_agent_loop(
            provider=recorder,
            system="you build software",
            task="Read README.md, then say in one sentence what the project is.",
            tools=tools,
            max_turns=4,
            max_usd=0.25,
        )
        assert load_cassette(path), f"nothing recorded for {model}"


# --------------------------------------------------------------------------- credentials


class _Unauthorised(Exception):
    """Shaped like litellm's, which carries the status on the exception."""

    status_code = 401


async def test_a_missing_key_names_the_variable_instead_of_raising_litellm_s_error() -> None:
    """The first wall every new user hits. A traceback here reads as "this tool is broken"."""

    async def refuse(**kwargs: Any) -> Any:
        raise _Unauthorised("No cookie auth credentials found")

    provider = LiteLLMProvider("openrouter/qwen/qwen3-coder", acompletion=refuse)
    with pytest.raises(ProviderAuthError) as caught:
        await provider.complete([{"role": "user", "content": "hi"}])

    message = str(caught.value)
    assert "OPENROUTER_API_KEY" in message
    assert "credentials.json" in message
    assert "No cookie auth credentials found" in message  # the provider's own words survive


async def test_a_bad_key_is_not_retried() -> None:
    """Four attempts at a key that is wrong is four times the wait for the same answer."""
    attempts = 0

    async def refuse(**kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        raise _Unauthorised("nope")

    provider = LiteLLMProvider("anthropic/claude-sonnet-5", acompletion=refuse, base_delay=0.0)
    with pytest.raises(ProviderAuthError):
        await provider.complete([{"role": "user", "content": "hi"}])
    assert attempts == 1


async def test_an_unknown_provider_prefix_names_no_variable_rather_than_the_wrong_one() -> None:
    async def refuse(**kwargs: Any) -> Any:
        raise _Unauthorised("nope")

    provider = LiteLLMProvider("somebody/else", acompletion=refuse)
    with pytest.raises(ProviderAuthError, match="the API key environment variable"):
        await provider.complete([{"role": "user", "content": "hi"}])


def test_every_model_loom_names_itself_maps_to_a_key_variable() -> None:
    """A default model whose key variable we cannot name would give the unhelpful message."""
    for model in MODEL_TIERS.values():
        assert key_variable_for(model), f"{model} has no known key variable"


def test_a_free_model_is_priced_at_zero_rather_than_raising() -> None:
    """FR-COST-02 — and the consequence worth knowing: a model with no price makes every USD
    ceiling non-binding. `max_turns` becomes the only real one."""
    warned: list[dict[str, Any]] = []
    usd = cost_of(
        raw=None,
        model="nvidia_nim/some/free-model",
        in_tokens=1_000_000,
        out_tokens=1_000_000,
        price_table={},
        on_event=lambda kind, **f: warned.append(f),
    )
    assert usd == 0.0
    assert warned and warned[0]["reason"] == "price_table_fallback"
    assert warned[0]["priced"] is False


async def test_an_unset_key_is_refused_before_the_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    """Providers disagree about what a *missing* credential is — NVIDIA NIM calls it a 500,
    which is retryable, so without a pre-flight it backs off four times for a reason no amount
    of waiting fixes."""
    monkeypatch.delenv("NVIDIA_NIM_API_KEY", raising=False)
    provider = LiteLLMProvider("nvidia_nim/nvidia/some-model")
    with pytest.raises(ProviderAuthError, match="NVIDIA_NIM_API_KEY"):
        await provider.complete([{"role": "user", "content": "hi"}])


async def test_the_preflight_stays_out_of_the_way_of_tests_and_cassettes() -> None:
    """An injected `acompletion` is a test or a cassette; neither needs a key."""

    async def canned(**kwargs: Any) -> Any:
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    provider = LiteLLMProvider("nvidia_nim/nvidia/some-model", acompletion=canned)
    assert (await provider.complete([{"role": "user", "content": "hi"}])).text == "ok"


class _DailyCap(Exception):
    """Shaped like the one OpenRouter's free tier actually sends."""

    status_code = 429

    def __str__(self) -> str:
        return (
            'RateLimitError: {"error":{"message":"Rate limit exceeded: free-models-per-day. '
            'Add 10 credits to unlock 1000 free model requests per day","code":429,'
            '"metadata":{"headers":{"X-RateLimit-Limit":"50","X-RateLimit-Remaining":"0",'
            '"X-RateLimit-Reset":"1788220800000"}}}}'
        )


async def test_a_daily_cap_is_not_retried_and_says_when_it_resets() -> None:
    """Waiting eight seconds cannot fix a limit that resets at midnight. A real run backed off
    twice and then printed forty lines of provider JSON to explain a one-sentence problem."""
    attempts = 0

    async def capped(**kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        raise _DailyCap()

    provider = LiteLLMProvider("openrouter/free/model", acompletion=capped, base_delay=0.0)
    with pytest.raises(ProviderQuotaError) as caught:
        await provider.complete([{"role": "user", "content": "hi"}])

    assert attempts == 1, "a daily cap must not be retried"
    message = str(caught.value)
    assert "resets at" in message
    assert "add credit" in message.lower()


class _Overloaded(Exception):
    status_code = 429

    def __str__(self) -> str:
        return "RateLimitError: too many requests, slow down"


async def test_an_ordinary_rate_limit_is_still_retried() -> None:
    """The per-minute kind is exactly what backoff is for. Only the daily kind bypasses it."""
    attempts = 0

    async def flaky(**kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise _Overloaded()
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    provider = LiteLLMProvider("openrouter/free/model", acompletion=flaky, base_delay=0.0)
    assert (await provider.complete([{"role": "user", "content": "hi"}])).text == "ok"
    assert attempts == 3


# --------------------------------------------------------------------------- FR-REPL-05, streaming


async def test_streaming_forwards_deltas_and_reassembles_the_response() -> None:
    """FR-REPL-05 — with on_token set the provider streams: each text delta reaches on_token as it
    arrives, and the reassembled response still carries the full text and the token counts."""
    pieces = ["Hel", "lo, ", "world"]
    seen_kwargs: list[dict[str, Any]] = []

    async def streaming_acompletion(**kwargs: Any) -> Any:
        seen_kwargs.append(kwargs)

        async def gen() -> Any:
            for piece in pieces:
                yield {"choices": [{"delta": {"content": piece}}]}

        return gen()

    def builder(chunks: list[Any], messages: Any = None) -> dict[str, Any]:
        text = "".join(c["choices"][0]["delta"]["content"] for c in chunks)
        return {
            "choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 3},
        }

    collected: list[str] = []
    provider = LiteLLMProvider(
        "openrouter/qwen/qwen3-coder",
        acompletion=streaming_acompletion,
        chunk_builder=builder,
        on_token=collected.append,
    )
    response = await provider.complete([{"role": "user", "content": "hi"}])

    assert collected == pieces  # streamed delta by delta, as it arrived
    assert response.text == "Hello, world"  # and reassembled whole for the loop
    assert response.out_tokens == 3
    assert seen_kwargs[0]["stream"] is True  # it actually asked the provider to stream


async def test_without_on_token_the_provider_does_not_stream() -> None:
    """The default path is unchanged: no on_token, no stream=True, one plain completion."""
    call = flaky()
    provider = LiteLLMProvider("m", acompletion=call)
    response = await provider.complete([{"role": "user", "content": "go"}])
    assert response.text == "ok"
    assert "stream" not in call.calls[0]


# --------------------------------------------------------------------------- fallback chain


class _Overloaded503(Exception):
    """The shape Loom actually sees: litellm wraps a mid-stream provider overload as
    `MidStreamFallbackError`, a `ServiceUnavailableError` whose `status_code` is 503 — so it is
    already retryable by status, and the crash was only that streaming had no fallback at all."""

    status_code = 503

    def __str__(self) -> str:
        return "litellm.MidStreamFallbackError: Service temporarily overloaded"


async def test_no_fallbacks_configured_behaves_exactly_as_a_single_provider() -> None:
    """The chain is one link by default: a failure raises after its own retries, no phantom
    extra attempt from an empty fallback list."""
    call = flaky(*[Throttled("no") for _ in range(10)])
    provider = LiteLLMProvider("m", acompletion=call, sleep=recording_sleep(), max_retries=2)
    with pytest.raises(Throttled):
        await provider.complete([{"role": "user", "content": "go"}])
    assert len(call.calls) == 2


async def test_a_fallback_takes_over_when_the_primary_is_overloaded(tmp_path: Path) -> None:
    """The whole point: an overloaded primary hands off to the next model, and the cost is
    attributed to the model that actually answered."""
    events: list[dict[str, Any]] = []
    seen: list[str] = []

    async def by_model(**kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs["model"])
        if kwargs["model"] == "primary/x":
            raise _Overloaded503()
        return {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    ledger = Ledger(tmp_path / "ledger.db")
    provider = LiteLLMProvider(
        "primary/x",
        fallback_models=["backup/y"],
        acompletion=by_model,
        sleep=recording_sleep(),
        max_retries=1,
        on_event=lambda kind, **f: events.append({"kind": kind, **f}),
        ledger=ledger,
        phase="build",
        run_id="r",
    )

    response = await provider.complete([{"role": "user", "content": "go"}])

    assert response.text == "ok"
    assert seen == ["primary/x", "backup/y"]  # tried the primary, then fell over
    switch = [e for e in events if e["kind"] == "fallback_switch"]
    assert switch and (switch[0]["from_model"], switch[0]["to_model"]) == ("primary/x", "backup/y")
    assert ledger.rows()[0]["model"] == "backup/y"  # cost belongs to who answered


async def test_the_last_link_in_the_chain_propagates_its_error() -> None:
    """When everything is down the final failure is raised, never swallowed into a silent pass."""

    async def always_down(**kwargs: Any) -> dict[str, Any]:
        raise _Overloaded503()

    provider = LiteLLMProvider(
        "primary/x",
        fallback_models=["backup/y", "ollama/local"],
        acompletion=always_down,
        sleep=recording_sleep(),
        max_retries=1,
    )
    with pytest.raises(_Overloaded503):
        await provider.complete([{"role": "user", "content": "go"}])


async def test_streaming_falls_over_when_the_stream_dies_before_the_first_delta() -> None:
    """The reported failure: the primary drops on connect, before any token is shown. Nothing is
    on screen yet, so switching providers is clean."""
    collected: list[str] = []
    seen: list[str] = []

    async def by_model(**kwargs: Any) -> Any:
        seen.append(kwargs["model"])

        async def primary() -> Any:
            raise _Overloaded503()
            yield  # pragma: no cover - only here to make this an async generator

        async def backup() -> Any:
            for piece in ["He", "llo"]:
                yield {"choices": [{"delta": {"content": piece}}]}

        return primary() if kwargs["model"] == "primary/x" else backup()

    def builder(chunks: list[Any], messages: Any = None) -> dict[str, Any]:
        text = "".join(c["choices"][0]["delta"]["content"] for c in chunks)
        return {
            "choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2},
        }

    provider = LiteLLMProvider(
        "primary/x",
        fallback_models=["backup/y"],
        acompletion=by_model,
        chunk_builder=builder,
        on_token=collected.append,
    )
    response = await provider.complete([{"role": "user", "content": "hi"}])

    assert response.text == "Hello"
    assert collected == ["He", "llo"]  # only the fallback's tokens ever reached the screen
    assert seen == ["primary/x", "backup/y"]


async def test_streaming_does_not_fall_over_once_a_delta_is_on_screen() -> None:
    """The invariant the hand-rolled plan got wrong: after a token is printed, a provider switch
    would print a second answer on top of it. So a mid-stream drop *after* the first delta fails
    the turn instead, surfacing the original error."""
    collected: list[str] = []
    seen: list[str] = []

    async def by_model(**kwargs: Any) -> Any:
        seen.append(kwargs["model"])

        async def primary() -> Any:
            yield {"choices": [{"delta": {"content": "Par"}}]}
            raise _Overloaded503()

        async def backup() -> Any:
            yield {"choices": [{"delta": {"content": "SHOULD-NOT-APPEAR"}}]}

        return primary() if kwargs["model"] == "primary/x" else backup()

    provider = LiteLLMProvider(
        "primary/x",
        fallback_models=["backup/y"],
        acompletion=by_model,
        chunk_builder=lambda chunks, messages=None: {"choices": [{"message": {"content": ""}}]},
        on_token=collected.append,
    )
    with pytest.raises(_Overloaded503):
        await provider.complete([{"role": "user", "content": "hi"}])

    assert collected == ["Par"]  # what was already streamed, and nothing more
    assert seen == ["primary/x"]  # the fallback was never tried
