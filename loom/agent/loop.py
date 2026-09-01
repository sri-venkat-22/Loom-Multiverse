"""The tool-calling loop. FR-AGENT-01…06.

This file knows nothing about any provider. It is handed something satisfying
`contracts.Provider` and something satisfying `Grader`, and it does not care what either is —
which is why every test below runs against `FakeLLM` and a five-line fake rubric, with no
network and no money.

Four things here exist because the blueprint addendum's sample loop got them wrong:

* no `caching=True` — that is litellm's *response* cache, which in an agent loop can replay one
  tool call forever;
* the USD check happens before the next call, not after;
* the feedback appended on a failed grade is the actual failure output, not "still fails";
* stall detection, so a model that cannot fix the repo stops being paid to try.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from loom.agent.context import COMPACT_ABOVE_CHARS, compaction_report, transcript_chars
from loom.agent.tools.registry import ToolNotFound, ToolRegistry
from loom.contracts import Provider, ToolCall
from loom.security import redact

Status = Literal["passed", "budget_exhausted", "stalled", "blocked"]

#: A graded round has to improve the total by at least this much to count as progress.
STALL_EPSILON = 0.01

#: Consecutive non-improving rounds before the loop gives up. Three, because two is noise.
STALL_ROUNDS = 3

#: The tool the loop handles itself rather than dispatching (FR-ASK-04).
ASK_USER = "ask_user"


@runtime_checkable
class Score(Protocol):
    """What `rubric.grade()` returns. WP-4.2 implements it; the loop only reads these two."""

    total: float

    def as_feedback(self) -> str: ...


class Grader(Protocol):
    """What the build phase passes in as `rubric`. Deliberately not `contracts.Rubric`: that is
    the *specification* of a rubric, this is something that can run one."""

    threshold: float

    async def grade(self) -> Score: ...


class LoopResult(BaseModel):
    """FR-AGENT-01. `messages` is the full transcript, so a caller can persist or compact it."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    status: Status
    turns_used: int = Field(ge=0)
    usd_spent: float = Field(ge=0.0)
    messages: list[dict[str, Any]] = Field(default_factory=list)
    score: Any = None


async def run_agent_loop(
    *,
    provider: Provider,
    system: str,
    task: str,
    tools: ToolRegistry,
    max_turns: int,
    max_usd: float,
    rubric: Grader | None = None,
    on_event: Callable[..., Any] | None = None,
    ask_user_fn: Callable[..., Any] | None = None,
    compact_above_chars: int = COMPACT_ABOVE_CHARS,
) -> LoopResult:
    """Run until the task passes, the money or the turns run out, or progress stops."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": task},
    ]
    specs = tools.specs()

    usd = 0.0
    turns = 0
    best: Score | None = None
    baseline = float("-inf")
    flat_rounds = 0

    def emit(kind: str, **fields: Any) -> None:
        if on_event is not None:
            on_event(kind, **fields)

    def finish(status: Status) -> LoopResult:
        return LoopResult(
            status=status, turns_used=turns, usd_spent=usd, messages=messages, score=best
        )

    for turn in range(1, max_turns + 1):
        # FR-AGENT-02: before the call, never after. An abort must not be charged for a
        # completion it has already decided to throw away.
        if usd >= max_usd:
            return finish("budget_exhausted")

        # NFR-REL — keep a long build under the context window. Done at the turn boundary, where
        # every tool_call already has its result, so compaction (which only shrinks old tool
        # output and never drops a message) cannot orphan a call from its answer.
        if transcript_chars(messages) >= compact_above_chars:
            compacted, reclaimed, _ = compaction_report(messages)
            if reclaimed > 0:
                messages[:] = compacted
                emit("compacted", reclaimed_chars=reclaimed, messages=len(messages))

        response = await provider.complete(messages, specs)
        turns = turn
        usd += response.usd_cost
        emit(
            "turn",
            turn=turn,
            usd=round(usd, 6),
            tool_calls=[c.name for c in response.tool_calls],
            text=bool(response.text),
        )

        if response.tool_calls:
            messages.append(_assistant_message(response.text, response.tool_calls))
            for tool_call in response.tool_calls:
                emit(
                    "tool_call",
                    name=tool_call.name,
                    id=tool_call.id,
                    args=_safe_args(tool_call.arguments),
                )
                result = await _dispatch(tool_call, tools=tools, ask_user_fn=ask_user_fn)
                emit("tool_result", name=tool_call.name, id=tool_call.id, chars=len(result))
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "content": result,
                    }
                )
            continue

        messages.append({"role": "assistant", "content": response.text or ""})

        if not response.text:
            # Nothing said and nothing asked for. Re-prompting an empty response just buys
            # another one, so stop and let the caller decide.
            return finish("blocked")

        if rubric is None:
            return finish("passed")

        score = await rubric.grade()
        emit("graded", total=round(score.total, 4), threshold=rubric.threshold)
        if best is None or score.total > best.total:
            best = score
        if score.total >= rubric.threshold:
            return finish("passed")

        if score.total >= baseline + STALL_EPSILON:
            baseline = score.total
            flat_rounds = 0
        else:
            flat_rounds += 1
            if flat_rounds >= STALL_ROUNDS:
                return finish("stalled")

        messages.append({"role": "user", "content": score.as_feedback()})

    return finish("budget_exhausted")


def _safe_args(arguments: dict[str, Any]) -> dict[str, Any]:
    """SEC-06 — a tool argument is the one part of a tool call that reaches the event log.

    ponytail: guarding at the emit site, not at the choke point. The complete fix is redaction
    inside `Session.log_event`, which would cover every event from every module; it is one line
    there and belongs in WP-5.4, where SEC-06 is actually verified end to end.
    """
    return {k: redact(v) if isinstance(v, str) else v for k, v in arguments.items()}


def _assistant_message(text: str | None, calls: list[ToolCall]) -> dict[str, Any]:
    """The OpenAI-shaped assistant turn. Arguments go back as a JSON string, which is what every
    provider echoes and what `ToolCall` already knows how to read back."""
    return {
        "role": "assistant",
        "content": text or "",
        "tool_calls": [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
            }
            for c in calls
        ],
    }


async def _dispatch(
    tool_call: ToolCall,
    *,
    tools: ToolRegistry,
    ask_user_fn: Callable[..., Any] | None,
) -> str:
    """Every failure here becomes text the model can read. FR-AGENT-04, FR-AGENT-05.

    `KeyboardInterrupt` and friends are not caught — an interrupt is not a tool error, and
    FR-SESS-09 needs it to unwind.
    """
    if tool_call.name == ASK_USER:
        if ask_user_fn is None:
            return (
                "ERROR: there is no way to ask the user in this run (non-interactive). State "
                "your assumption and continue."
            )
        try:
            answer = ask_user_fn(**tool_call.arguments)
            if inspect.isawaitable(answer):
                answer = await answer
            return str(answer)
        except Exception as exc:  # noqa: BLE001 - the model gets the text, the run continues
            return f"ERROR: could not ask the user: {type(exc).__name__}: {exc}"

    try:
        return await tools.execute(tool_call.name, tool_call.arguments)
    except ToolNotFound as exc:
        return f"ERROR: {exc}"
    except Exception as exc:  # noqa: BLE001 - FR-AGENT-04, it must not propagate
        return f"ERROR: {tool_call.name} raised {type(exc).__name__}: {exc}"
