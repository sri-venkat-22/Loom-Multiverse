"""The Design phase. WP-3.4 — FR-DES-01/03/05.

`Design` is the only artifact the Build phase reads, so this phase is where a run is actually
decided. A vague file manifest produces a repo that builds something adjacent to the product,
and a rubric that cannot be satisfied honestly produces a build that burns its budget or games
its own tests.

`contracts.Design` already enforces the structural half — unique paths, resolvable
dependencies, no cycles, `hard_fail` names that exist (FR-DES-02). What is added here is the
half a type cannot express: a rubric with enough criteria, weights that sum, and at least one
shell criterion that runs the suite the build is supposed to write.

The ground truth for this prompt is `tests/fixtures/urlshortener/design.json`, hand-written
before the prompt existed. FR-DES-04 measures the prompt against that fixture; anything the
fixture does that this prompt does not ask for is a gap in the prompt.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from loom.blueprints.loader import Blueprint, merge_blueprint
from loom.contracts import PRD, Design, Validation
from loom.phases.base import Phase

#: FR-DES-03. Two is the floor a type could not enforce; the prompt asks for four to six.
MIN_CRITERIA = 2

#: Weights are floats a model wrote by hand, so exact equality is a trap. Two percent is wider
#: than any rounding and narrower than a real mistake.
WEIGHT_TOLERANCE = 0.02

#: A shell criterion whose command contains one of these is running a test suite. Crude on
#: purpose: the alternative is a parser for every runner, and a false negative here costs one
#: repair round while a false positive costs a build that is never really tested.
TEST_RUNNERS = ("pytest", "unittest", "npm test", "go test", "cargo test", "phpunit", "rspec")


class DesignPhase(Phase):
    """`PRD` in, `Design` — including its rubric — out."""

    name: ClassVar[str] = "design"
    artifact_model: ClassVar[type] = Design
    prompt_name: ClassVar[str] = "design"

    def __init__(
        self,
        *,
        prompt_path: Path | None = None,
        blueprint: Blueprint | None = None,
        on_event: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(prompt_path=prompt_path)
        self.blueprint = blueprint
        self._on_event = on_event
        self._merge_logged = False

    def prepare_input(
        self,
        *,
        prd: PRD | None = None,
        validation: Validation | None = None,
        notes: str = "",
        **_: Any,
    ) -> str:
        if prd is None:
            raise ValueError("the design phase needs a PRD artifact")
        lines = [
            f"# {prd.product_name}",
            "",
            "## Problem",
            "",
            prd.problem.strip(),
            "",
            "## Users",
            *(f"- {u}" for u in prd.users),
            "",
            "## v0 features — build exactly these",
            *(f"- {f}" for f in prd.v0_features),
        ]
        if prd.non_goals:
            lines += ["", "## Non-goals — do not design for these"]
            lines += [f"- {n}" for n in prd.non_goals]
        if prd.success_metrics:
            lines += ["", "## Success metrics", *(f"- {m}" for m in prd.success_metrics)]
        if validation is not None and validation.risks:
            lines += ["", "## Risks the research found", *(f"- {r}" for r in validation.risks)]
        if notes.strip():
            lines += ["", "## Constraints from the founder", "", notes.strip()]
        if self.blueprint is not None:
            lines += self._blueprint_section()
        return "\n".join(lines)

    def _blueprint_section(self) -> list[str]:
        """Bias the model toward the blueprint's choices. Also puts the blueprint in the task,
        so the cache key differs per blueprint and two blueprints never share a cache entry."""
        bp = self.blueprint
        assert bp is not None
        out = ["", f"## Blueprint: {bp.name or 'preset'} — prefer these known-good choices", ""]
        if bp.stack:
            out += ["Stack: " + ", ".join(bp.stack), ""]
        if bp.scaffold_command:
            out += [f"Scaffold command: `{bp.scaffold_command}`", ""]
        if bp.file_manifest:
            out += ["Expected files (add more as the idea needs):"]
            out += [f"- `{f.path}` — {f.purpose}" for f in bp.file_manifest]
            out += [""]
        if bp.rubric is not None and bp.rubric.criteria:
            out += ["These acceptance criteria are already fixed; do not restate them:"]
            for c in bp.rubric.criteria:
                out.append(f"- {c.name}: {c.description or c.command or c.question}")
            out += [""]
        out += ["These are merged into your output automatically; align your design with them."]
        return out

    def refine(self, artifact: Any) -> Any:
        """FR-DES-03 — raise, so the repair loop hands the model back its own broken rubric.

        A rubric this phase let through un-gradable is not caught again downstream: the build
        phase would spend a whole budget being told it scored 0.6 out of a possible 0.6.

        FR-BP-01 — the blueprint merges here, before the gradability gate, so a blueprint that
        pins a solid rubric can rescue a model's weak one instead of triggering a repair round.
        """
        design: Design = artifact
        if self.blueprint is not None:
            design, conflicts = merge_blueprint(design, self.blueprint)
            if not self._merge_logged and self._on_event is not None:
                self._on_event(
                    "blueprint_merged",
                    blueprint=self.blueprint.name or "blueprint",
                    conflicts=conflicts,
                )
                self._merge_logged = True
        rubric = design.rubric

        if len(rubric.criteria) < MIN_CRITERIA:
            raise ValueError(
                f"the rubric has {len(rubric.criteria)} criterion; it needs at least "
                f"{MIN_CRITERIA}. One criterion is a pass/fail check, not a rubric."
            )

        total = sum(c.weight for c in rubric.criteria)
        if abs(total - 1.0) > WEIGHT_TOLERANCE:
            weights = ", ".join(f"{c.name}={c.weight}" for c in rubric.criteria)
            raise ValueError(
                f"the criterion weights sum to {total:.3f}, not 1.0 ({weights}). A threshold "
                "means nothing against a total that is not 1.0 — reweight them."
            )

        if not any(_runs_a_test_suite(c.command) for c in rubric.criteria if c.kind == "shell"):
            raise ValueError(
                "no shell criterion runs the project's test suite. Add one whose command runs "
                f"the tests the manifest says will be written (one of: {', '.join(TEST_RUNNERS)})."
            )

        return design


def _runs_a_test_suite(command: str | None) -> bool:
    if not command:
        return False
    return any(runner in command.lower() for runner in TEST_RUNNERS)
