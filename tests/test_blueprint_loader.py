"""WP-6.1 — the blueprint loader and merge. FR-BP-01, FR-BP-02.

A blueprint is a partial Design merged into the Design phase's output, blueprint-wins on every
conflict with a logged event. These tests pin the merge rules and prove the one first-party
blueprint (`fastapi-sqlite-api`) merges into a valid, gradable Design.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from loom.blueprints.loader import (
    FIRST_PARTY,
    Blueprint,
    load_blueprint,
    merge_blueprint,
    resolve_blueprint_path,
)
from loom.contracts import Criterion, Design, FileSpec, Rubric
from loom.phases.design import DesignPhase
from loom.testing.fake_llm import FakeLLM


def model_design(**over: object) -> Design:
    """A plausible model-produced Design, before any blueprint touches it."""
    base = dict(
        summary="a to-do list REST API",
        stack=["Python 3.11", "Flask"],
        scaffold_command="uv init -q",
        file_manifest=[
            FileSpec(path="app/main.py", purpose="the model's own main"),
            FileSpec(path="app/todo.py", purpose="logic", depends_on=["app/main.py"]),
        ],
        rubric=Rubric(
            threshold=0.80,
            hard_fail=["tests"],
            criteria=[
                Criterion(name="tests", kind="shell", weight=0.6, command="pytest"),
                Criterion(name="readable", kind="judge", weight=0.4, question="clear?"),
            ],
        ),
    )
    base.update(over)
    return Design(**base)  # type: ignore[arg-type]


def a_blueprint(**over: object) -> Blueprint:
    base: dict[str, object] = dict(
        name="bp",
        stack=["Python 3.11", "FastAPI"],
        scaffold_command="uv init --name app -q",
        file_manifest=[
            FileSpec(path="app/db.py", purpose="sqlite persistence"),
            FileSpec(path="app/main.py", purpose="the blueprint's main"),
        ],
        rubric={
            "threshold": 0.85,
            "hard_fail": ["app_boots"],
            "criteria": [
                {"name": "tests", "kind": "shell", "weight": 0.4, "command": "uv run pytest -q"},
                {"name": "app_boots", "kind": "shell", "weight": 0.2, "command": "python -c 'x'"},
            ],
        },
    )
    base.update(over)
    return Blueprint.model_validate(base)


# --------------------------------------------------------------------------- resolution


def test_a_first_party_name_resolves(tmp_path: Path) -> None:
    assert resolve_blueprint_path("fastapi-sqlite-api") == (
        FIRST_PARTY / "fastapi-sqlite-api" / "blueprint.json"
    )


def test_a_directory_and_a_file_both_resolve(tmp_path: Path) -> None:
    (tmp_path / "blueprint.json").write_text("{}", encoding="utf-8")
    assert resolve_blueprint_path(str(tmp_path)) == tmp_path / "blueprint.json"
    assert resolve_blueprint_path(str(tmp_path / "blueprint.json")) == tmp_path / "blueprint.json"


def test_an_unknown_reference_lists_what_is_available() -> None:
    with pytest.raises(FileNotFoundError, match="fastapi-sqlite-api"):
        resolve_blueprint_path("no-such-blueprint")


# --------------------------------------------------------------------------- the merge rules


def test_blueprint_wins_and_every_conflict_is_reported() -> None:
    """FR-BP-01 — the four kinds of override, each named in the returned conflict list."""
    merged, conflicts = merge_blueprint(model_design(), a_blueprint())
    assert set(conflicts) == {
        "scaffold_command",
        "file:app/main.py",
        "criterion:tests",
        "threshold",
    }
    assert merged.scaffold_command == "uv init --name app -q"
    assert merged.rubric.threshold == 0.85
    main = next(f for f in merged.file_manifest if f.path == "app/main.py")
    assert main.purpose == "the blueprint's main"
    tests = next(c for c in merged.rubric.criteria if c.name == "tests")
    assert tests.command == "uv run pytest -q"


def test_non_conflicting_fields_are_added_not_replaced() -> None:
    merged, _ = merge_blueprint(model_design(), a_blueprint())
    # the blueprint's new file and criterion join the model's, nothing is dropped
    assert {f.path for f in merged.file_manifest} >= {"app/todo.py", "app/db.py", "app/main.py"}
    assert {c.name for c in merged.rubric.criteria} == {"tests", "readable", "app_boots"}


def test_stack_is_a_union_with_the_blueprint_first() -> None:
    merged, conflicts = merge_blueprint(model_design(), a_blueprint())
    assert merged.stack[:2] == ["Python 3.11", "FastAPI"]  # blueprint order first
    assert "Flask" in merged.stack  # the model's extra survives
    assert merged.stack.count("Python 3.11") == 1  # de-duplicated
    assert "stack" not in " ".join(conflicts)  # a union is not a conflict


def test_merged_rubric_weights_are_renormalised_to_one() -> None:
    """Merging two independently-weighted rubrics must still sum to 1.0, or the Design phase's
    own weight-sum gate would reject a design the blueprint just fixed."""
    merged, _ = merge_blueprint(model_design(), a_blueprint())
    assert sum(c.weight for c in merged.rubric.criteria) == pytest.approx(1.0)


def test_hard_fail_is_the_union_filtered_to_real_criteria() -> None:
    merged, _ = merge_blueprint(model_design(), a_blueprint())
    assert set(merged.rubric.hard_fail) == {"tests", "app_boots"}


def test_a_blueprint_without_a_rubric_leaves_the_model_rubric_untouched() -> None:
    bp = Blueprint(name="bp", stack=["FastAPI"])
    merged, conflicts = merge_blueprint(model_design(), bp)
    assert merged.rubric == model_design().rubric
    assert all(not c.startswith("criterion") and c != "threshold" for c in conflicts)


# ----------------------------------------------------------- the phase applies it (FR-BP-01)


def answer(design: Design) -> object:
    from loom.contracts import Response

    return Response(text=design.model_dump_json(), usd_cost=0.001)


async def test_the_phase_merges_the_blueprint_and_logs_the_event(tmp_path: Path) -> None:
    from loom.contracts import PRD

    events: list[tuple[str, dict]] = []
    phase = DesignPhase(blueprint=a_blueprint(), on_event=lambda k, **f: events.append((k, f)))
    prd = PRD(product_name="Todo", problem="lists", users=["me"], v0_features=["add a task"])
    outcome = await phase.execute(
        provider=FakeLLM([answer(model_design())]), inputs={"prd": prd}, root=tmp_path
    )
    design: Design = outcome.artifact
    assert design.scaffold_command == "uv init --name app -q"  # blueprint won
    kinds = [k for k, _ in events]
    assert "blueprint_merged" in kinds
    merged_event = next(f for k, f in events if k == "blueprint_merged")
    assert "criterion:tests" in merged_event["conflicts"]


def test_the_blueprint_biases_the_prompt_and_the_cache_key() -> None:
    """The blueprint text lands in the task, so the model is steered and two blueprints can
    never collide in the phase cache (the key is built from the task)."""
    from loom.contracts import PRD

    prd = PRD(product_name="Todo", problem="lists", users=["me"], v0_features=["add a task"])
    plain = DesignPhase().prepare_input(prd=prd)
    biased = DesignPhase(blueprint=a_blueprint()).prepare_input(prd=prd)
    assert "Blueprint:" in biased and "Blueprint:" not in plain
    assert "app/db.py" in biased


def test_no_blueprint_leaves_refine_an_identity() -> None:
    """The pre-blueprint contract: with no blueprint, refine returns the very same object."""
    design = model_design(
        rubric=Rubric(
            threshold=0.85,
            hard_fail=[],
            criteria=[
                Criterion(name="tests", kind="shell", weight=0.6, command="uv run pytest -q"),
                Criterion(name="readable", kind="judge", weight=0.4, question="clear?"),
            ],
        )
    )
    assert DesignPhase().refine(design) is design


# ---------------------------------------------------- the first-party blueprint (FR-BP-02)


def test_the_first_party_blueprint_loads_and_is_shaped_right() -> None:
    bp = load_blueprint("fastapi-sqlite-api")
    assert bp.name == "fastapi-sqlite-api"
    assert any("fastapi" in s.lower() for s in bp.stack)
    assert bp.rubric is not None
    # it pins the criterion a generated repo cannot be trusted without
    assert any(c.command and "pytest" in c.command for c in bp.rubric.criteria)


def test_the_first_party_blueprint_merges_into_a_gradable_design() -> None:
    """The mechanism behind FR-BP-02: merged into a thin model design, the result clears the
    Design phase's own gate — a valid manifest DAG, a gradable rubric, a real test criterion."""
    thin = Design(
        summary="a to-do list REST API",
        stack=[],
        file_manifest=[FileSpec(path="app/routes.py", purpose="the endpoints")],
        rubric=Rubric(
            threshold=0.5,
            hard_fail=[],
            criteria=[Criterion(name="readable", kind="judge", weight=1.0, question="clear?")],
        ),
    )
    merged, _ = merge_blueprint(thin, load_blueprint("fastapi-sqlite-api"))
    # refine (no blueprint on this instance) runs the gradability gate on the merged design
    assert DesignPhase().refine(merged) is merged
    assert merged.rubric.threshold == 0.85
    assert any(c.command and "pytest" in c.command for c in merged.rubric.criteria)


def test_the_shipped_blueprint_json_is_valid() -> None:
    raw = (FIRST_PARTY / "fastapi-sqlite-api" / "blueprint.json").read_text(encoding="utf-8")
    json.loads(raw)  # parses
    Blueprint.model_validate_json(raw)  # and validates against the schema
