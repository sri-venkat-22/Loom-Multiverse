"""ask_user — the tool a phase reaches for when it would otherwise guess. FR-ASK-01…05.

Stakeholder requirement, verbatim: "if the software has any doubts it should ask user through
the terminal". This is the pipeline half of that. WP-8.2 is the terminal half — it supplies
`prompt_fn`, which is the only thing here that knows a TTY exists.

The cap is the point. An agent allowed to ask freely will interview the user instead of making
a decision, and a founder who wanted an interview would have written the spec themselves.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Annotated, Any

from pydantic import Field

from loom.agent.tools.registry import Tool, tool

#: Questions per phase. FR-ASK-02.
CAP = 3

CAP_MESSAGE = (
    "cap reached — you have asked the maximum number of questions for this phase. State your "
    "assumption in the artifact and continue."
)

NO_DEFAULT = (
    "this run is unattended and the question declared no default — state your assumption in the "
    "artifact and continue."
)


def ask_user_tool() -> Tool:
    """The schema the model sees. The handler exists only to fail loudly.

    FR-ASK-04 routes every `ask_user` call to the caller's `ask_user_fn` inside the loop, so this
    body is unreachable — and if a future refactor makes it reachable, it says so rather than
    silently answering a user's question with nobody in the room.
    """

    @tool(name="ask_user")
    def ask_user(
        question: Annotated[str, Field(description="One question, in plain language.")],
        options: Annotated[
            list[str] | None, Field(description="Suggested answers, if there is a short list.")
        ] = None,
        default: Annotated[
            str | None, Field(description="What to assume if nobody answers. Declare one.")
        ] = None,
    ) -> str:
        """Ask the user one question when the answer would change what you build.

        Limited to three per phase. Prefer stating an assumption over spending a question.
        """
        raise RuntimeError(
            "ask_user must be routed to ask_user_fn by the agent loop, never executed through "
            "the tool registry (FR-ASK-04)"
        )

    return ask_user


class AskUser:
    """Callable, passed to `run_agent_loop(ask_user_fn=...)`.

    `prompt_fn` is whatever actually asks — a widget in an interactive session, absent in a
    piped one. Absent is not an error: it is the same path as `--yes`, which is what makes
    FR-HEADLESS work without a second code path.
    """

    def __init__(
        self,
        *,
        prompt_fn: Callable[..., Any] | None = None,
        yes: bool = False,
        on_event: Callable[..., Any] | None = None,
        phase: str = "",
        cap: int = CAP,
        default_answer: str | None = None,
    ) -> None:
        self.prompt_fn = prompt_fn
        self.yes = yes
        self.on_event = on_event
        self.phase = phase
        self.cap = cap
        self.default_answer = default_answer
        self.asked = 0

    def start_phase(self, phase: str) -> None:
        """New phase, fresh budget of questions."""
        self.phase = phase
        self.asked = 0

    async def __call__(
        self,
        question: str,
        options: list[str] | None = None,
        default: str | None = None,
        **_ignored: Any,
    ) -> str:
        if self.asked >= self.cap:
            return CAP_MESSAGE
        self.asked += 1

        self._emit(
            "ask_user", question=question, options=options, default=default, phase=self.phase
        )

        interactive = self.prompt_fn is not None and not self.yes
        if interactive:
            assert self.prompt_fn is not None
            answer = self.prompt_fn(question=question, options=options, default=default)
            if inspect.isawaitable(answer):
                answer = await answer
            answer = str(answer)
        else:
            answer = default or self.default_answer or NO_DEFAULT

        self._emit("user_answer", answer=answer, assumed=not interactive, phase=self.phase)
        return answer

    def _emit(self, kind: str, **fields: Any) -> None:
        if self.on_event is not None:
            self.on_event(kind, **fields)
