"""SRS §6 (data model), §5.4 (provider interface), FR-DES-02."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from loom.contracts import (
    PRD,
    Criterion,
    Design,
    FileSpec,
    Provider,
    Response,
    Rubric,
    ToolCall,
    Validation,
)


def _criterion(name: str = "tests-pass", **kw: object) -> Criterion:
    base: dict[str, object] = {
        "name": name,
        "kind": "shell",
        "weight": 0.5,
        "command": "pytest -q",
    }
    base.update(kw)
    return Criterion(**base)  # type: ignore[arg-type]


def _rubric(**kw: object) -> Rubric:
    base: dict[str, object] = {"criteria": [_criterion()], "threshold": 0.85}
    base.update(kw)
    return Rubric(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- round trips


def test_design_round_trips_through_json() -> None:
    design = Design(
        summary="a url shortener",
        stack=["python", "fastapi"],
        scaffold_command="uv init",
        file_manifest=[
            FileSpec(path="app.py", purpose="routes", depends_on=["store.py"]),
            FileSpec(path="store.py", purpose="sqlite persistence"),
        ],
        rubric=_rubric(hard_fail=["tests-pass"]),
    )
    assert Design.model_validate_json(design.model_dump_json()) == design


def test_validation_and_prd_round_trip() -> None:
    v = Validation(idea="x", verdict="go", reasoning="because", risks=["none"])
    assert Validation.model_validate_json(v.model_dump_json()) == v
    p = PRD(product_name="x", problem="y", users=["devs"], v0_features=["shorten"])
    assert PRD.model_validate_json(p.model_dump_json()) == p


# --------------------------------------------------------------------------- rejections


@pytest.mark.parametrize("weight", [0.0, -0.1, 1.01, 2.0])
def test_weight_outside_zero_to_one_is_rejected(weight: float) -> None:
    with pytest.raises(ValidationError):
        _criterion(weight=weight)


def test_weight_of_exactly_one_is_allowed() -> None:
    assert _criterion(weight=1.0).weight == 1.0


def test_empty_criteria_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Rubric(criteria=[], threshold=0.5)


@pytest.mark.parametrize("threshold", [-0.01, 1.5])
def test_threshold_outside_zero_to_one_is_rejected(threshold: float) -> None:
    with pytest.raises(ValidationError):
        _rubric(threshold=threshold)


def test_hard_fail_naming_an_unknown_criterion_is_rejected() -> None:
    with pytest.raises(ValidationError, match="no such criterion"):
        _rubric(hard_fail=["typechecks"])


def test_duplicate_criterion_names_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate criterion names"):
        _rubric(criteria=[_criterion(), _criterion()])


def test_shell_criterion_without_a_command_is_rejected() -> None:
    with pytest.raises(ValidationError, match="needs a command"):
        Criterion(name="c", kind="shell", weight=0.5)


def test_judge_criterion_without_a_question_is_rejected() -> None:
    with pytest.raises(ValidationError, match="needs a question"):
        Criterion(name="c", kind="judge", weight=0.5)


def test_judge_criterion_needs_no_command() -> None:
    c = Criterion(name="c", kind="judge", weight=0.5, question="is the README useful?")
    assert c.command is None


# --------------------------------------------------------------------------- manifest DAG


def test_manifest_cycle_is_rejected() -> None:
    """FR-DES-02 — the manifest is a DAG."""
    with pytest.raises(ValidationError, match="dependency cycle"):
        Design(
            summary="s",
            file_manifest=[
                FileSpec(path="a.py", purpose="a", depends_on=["b.py"]),
                FileSpec(path="b.py", purpose="b", depends_on=["a.py"]),
            ],
            rubric=_rubric(),
        )


def test_manifest_self_cycle_is_rejected() -> None:
    with pytest.raises(ValidationError, match="dependency cycle"):
        Design(
            summary="s",
            file_manifest=[FileSpec(path="a.py", purpose="a", depends_on=["a.py"])],
            rubric=_rubric(),
        )


def test_manifest_dependency_on_an_unknown_path_is_rejected() -> None:
    """FR-DES-02 — no dependency on a path outside the manifest."""
    with pytest.raises(ValidationError, match="not in the manifest"):
        Design(
            summary="s",
            file_manifest=[FileSpec(path="a.py", purpose="a", depends_on=["ghost.py"])],
            rubric=_rubric(),
        )


def test_duplicate_manifest_paths_are_rejected() -> None:
    """FR-DES-02 — unique paths."""
    with pytest.raises(ValidationError, match="duplicate paths"):
        Design(
            summary="s",
            file_manifest=[
                FileSpec(path="a.py", purpose="a"),
                FileSpec(path="a.py", purpose="again"),
            ],
            rubric=_rubric(),
        )


def test_empty_manifest_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Design(summary="s", file_manifest=[], rubric=_rubric())


# --------------------------------------------------------------------------- agent protocol


def test_tool_call_arguments_arriving_as_a_json_string_are_parsed() -> None:
    """SRS §6 — Qwen emits this shape intermittently."""
    call = ToolCall(id="1", name="read_file", arguments='{"path": "a.py"}')  # type: ignore[arg-type]
    assert call.arguments == {"path": "a.py"}


def test_tool_call_empty_string_arguments_become_an_empty_dict() -> None:
    assert ToolCall(id="1", name="ls", arguments="").arguments == {}  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["{not json", '"a string"', "[1, 2]"])
def test_tool_call_unparseable_string_arguments_are_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        ToolCall(id="1", name="ls", arguments=bad)  # type: ignore[arg-type]


def test_response_defaults_and_negative_cost() -> None:
    r = Response()
    assert r.text is None and r.tool_calls == [] and r.usd_cost == 0.0
    assert r.wants_tools is False
    with pytest.raises(ValidationError):
        Response(usd_cost=-1.0)


def test_response_carries_the_raw_payload_untouched() -> None:
    sentinel = object()
    assert Response(raw=sentinel).raw is sentinel


def test_provider_protocol_is_satisfied_by_a_duck() -> None:
    """SRS §5.4 — the only seam between Loom and any model."""

    class Duck:
        async def complete(self, messages, tools=None):  # type: ignore[no-untyped-def]
            return Response(text="hi")

    assert isinstance(Duck(), Provider)
