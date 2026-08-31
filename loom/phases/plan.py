"""The Plan phase. WP-3.3 — FR-PLAN-01/02/03.

Reads the `Validation` artifact, writes a `PRD`. No tools beyond `ask_user`: there is nothing
to look up that the validation did not already find, and a phase with a search tool will use it.
"""

from __future__ import annotations

from typing import Any, ClassVar

from loom.contracts import PRD, Validation
from loom.phases.base import Phase

#: FR-PLAN-02. Six features is not a v0, it is a quarter. The cap is enforced here rather than
#: only asked for in the prompt, because "keep it small" is the instruction models discard first.
V0_FEATURE_CAP = 5

OVERFLOW_NOTE = "deferred from v0 by the scope cap"


class PlanPhase(Phase):
    """`Validation` in, `PRD` out."""

    name: ClassVar[str] = "plan"
    artifact_model: ClassVar[type] = PRD
    prompt_name: ClassVar[str] = "plan"

    def prepare_input(
        self, *, validation: Validation | None = None, notes: str = "", **_: Any
    ) -> str:
        if validation is None:
            raise ValueError("the plan phase needs a Validation artifact")
        lines = [
            "# The validated idea",
            "",
            validation.idea.strip(),
            "",
            f"Verdict: **{validation.verdict}**",
            "",
            validation.reasoning.strip(),
        ]
        if validation.competitors:
            lines += ["", "## What already exists", *(f"- {c}" for c in validation.competitors)]
        if validation.risks:
            lines += ["", "## Risks found", *(f"- {r}" for r in validation.risks)]
        if notes.strip():
            lines += ["", "## Constraints from the founder", "", notes.strip()]
        return "\n".join(lines)

    def refine(self, artifact: Any) -> Any:
        """FR-PLAN-02 — the overflow is moved, not dropped.

        Rewriting rather than raising: which five of seven features are the v0 is a judgement
        the model already made by ordering them, and buying a repair round to re-make it is
        money for nothing.
        """
        prd: PRD = artifact
        if len(prd.v0_features) <= V0_FEATURE_CAP:
            return prd
        keep = prd.v0_features[:V0_FEATURE_CAP]
        moved = [f"{f} ({OVERFLOW_NOTE})" for f in prd.v0_features[V0_FEATURE_CAP:]]
        return prd.model_copy(update={"v0_features": keep, "non_goals": [*prd.non_goals, *moved]})
