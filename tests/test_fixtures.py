"""The hand-written fixture Designs, checked before anyone spends money on them.

`tests/fixtures/*/design.json` is both the build fixture for WP-4.5 and the ground truth that
`prompts/design.md` is later tuned to reproduce (FR-DES-04). Everything downstream inherits its
judgment, so the failure this file exists to prevent is a live run that costs a dollar and fails
for a reason that has nothing to do with whether the agent can build a repo — a criterion the
deny-list refuses, an acceptance script with a typo in it, an evidence glob pointing at a file
the design never asked for.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

import pytest

from loom.contracts import Criterion, Design
from loom.security import check_command

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DESIGNS = sorted(FIXTURES.glob("*/design.json"))


def load(path: Path) -> Design:
    return Design.model_validate_json(path.read_text(encoding="utf-8"))


def ids(paths: list[Path]) -> list[str]:
    return [p.parent.name for p in paths]


pytestmark = pytest.mark.parametrize("path", DESIGNS, ids=ids(DESIGNS))


def test_the_fixture_is_a_valid_design(path: Path) -> None:
    """Which also gets the manifest DAG, unique paths and hard_fail resolution for free."""
    design = load(path)
    assert design.summary and design.file_manifest
    assert json.loads(path.read_text(encoding="utf-8"))  # and it is readable JSON, not a blob


def test_the_rubric_satisfies_fr_des_03(path: Path) -> None:
    """Two criteria, weights summing to ~1.0, and at least one shell criterion that runs the
    generated test suite."""
    rubric = load(path).rubric
    assert len(rubric.criteria) >= 2
    assert sum(c.weight for c in rubric.criteria) == pytest.approx(1.0, abs=0.01)
    assert any(
        c.kind == "shell" and c.command and "pytest" in c.command for c in rubric.criteria
    ), "no criterion runs the test suite"


def test_every_shell_criterion_survives_the_deny_list(path: Path, tmp_path: Path) -> None:
    """A criterion the guard refuses scores 0.0 with "denied" — every round, for free, forever.

    This is the cheapest possible place to find that out.
    """
    for criterion in load(path).rubric.criteria:
        if criterion.kind == "shell":
            check_command(criterion.command or "", root=tmp_path)


def test_the_scaffold_command_survives_the_deny_list(path: Path, tmp_path: Path) -> None:
    design = load(path)
    if design.scaffold_command:
        check_command(design.scaffold_command, root=tmp_path)


def test_inline_python_in_a_criterion_actually_compiles(path: Path) -> None:
    """An acceptance check is usually a `python -c` one-liner, and a hard-fail one at that. A
    typo in it fails every build at 0.0 and looks exactly like the model's fault."""
    for criterion in load(path).rubric.criteria:
        for source in _inline_python(criterion):
            compile(source, f"{criterion.name}:-c", "exec")


def test_judge_evidence_points_at_files_the_design_promises(path: Path) -> None:
    """An evidence glob that matches nothing yields "(no evidence files matched)" and a bad
    grade the model cannot act on."""
    design = load(path)
    promised = [Path(spec.path) for spec in design.file_manifest]
    for criterion in design.rubric.criteria:
        if criterion.kind != "judge":
            continue
        assert criterion.evidence_paths, f"{criterion.name} judges with no evidence"
        for pattern in criterion.evidence_paths:
            assert any(p.match(pattern) for p in promised), (
                f"{criterion.name} reads {pattern!r}, which no file in the manifest matches"
            )


def test_a_hard_fail_criterion_is_not_written_by_the_model(path: Path) -> None:
    """The anti-gaming property, as a test.

    If the only hard-fail criterion is "the model's own test suite passes", then weakening a
    test is enough to pass the rubric, and FR-BUILD-04's clause in the prompt is the only thing
    standing in the way. At least one hard-fail criterion has to check behaviour the model does
    not get to define.
    """
    rubric = load(path).rubric
    hard = [c for c in rubric.criteria if c.name in rubric.hard_fail]
    assert hard, "nothing is a hard fail; a broken build can still score well"
    independent = [c for c in hard if c.kind == "shell" and c.command and "pytest" not in c.command]
    assert independent, (
        "every hard-fail criterion runs the model's own tests, so weakening a test is enough "
        "to pass the rubric"
    )


def test_the_threshold_is_the_one_the_milestone_targets(path: Path) -> None:
    """WP-4.5: rubric >= 0.85. A fixture tuned to 0.6 would pass a milestone nobody set."""
    assert load(path).rubric.threshold >= 0.85


def _inline_python(criterion: Criterion) -> list[str]:
    """The argument of every `python -c` in a criterion's command."""
    found: list[str] = []
    tokens: list[Any] = shlex.split(criterion.command or "")
    for index, token in enumerate(tokens):
        if token == "-c" and index + 1 < len(tokens) and "python" in " ".join(tokens[:index]):
            found.append(tokens[index + 1])
    return found
