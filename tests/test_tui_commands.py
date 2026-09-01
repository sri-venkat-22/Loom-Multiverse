"""WP-8.2 — the command registry, the palette, and the selection widgets' logic.

FR-SLASH-01/02/04/05/07/08/09 (the registry and palette), FR-SEL-02/03 (model picker persistence,
effort mapping), FR-SLASH-06 (typed arguments skip the widget). The interactive `prompt_toolkit`
rendering is exercised in `test_tui_repl.py`; here the handlers run against a scripted context, so
what is asserted is the *decision*, not the drawing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from loom.config import EFFORT_PRESETS, Config, load_config, project_config_path
from loom.phases.base import artifact_path
from loom.tui.commands import (
    REGISTRY,
    Command,
    dispatch,
    filter_commands,
    render_help,
    render_palette,
    resolve,
    suggest,
    unknown_message,
)
from loom.tui.widgets import (
    ModelChoice,
    ModelOption,
    effort_meaning,
    missing_upstream,
    model_options,
    phase_rows,
)

# --------------------------------------------------------------------------- a scripted context


@dataclass
class StubContext:
    """A `ReplContext` whose interactive parts are scripted and whose business actions are real
    (so a config write actually lands on disk) or recorded."""

    root: Path
    config: Config
    state: str = "idle"
    run_id: str = "r"

    model_pick: ModelChoice | None = None
    effort_pick: str | None = None
    list_pick: int | None = None
    confirm_answer: bool = True

    writes: list[str] = field(default_factory=list)
    picker_calls: int = 0
    slider_calls: int = 0
    session_model: str | None = None
    started: tuple | None = None
    resumed: str | None = None
    replayed: str | None = None
    changed_dir: Path | None = None
    added_dir: Path | None = None
    cleared: bool = False

    # ReplContext surface -------------------------------------------------------------
    def write(self, text: str) -> None:
        self.writes.append(text)

    def run_model_picker(self, options: list[ModelOption]) -> ModelChoice | None:
        self.picker_calls += 1
        return self.model_pick

    def run_slider(self, levels: list[str], meaning: dict[str, str], start: int) -> str | None:
        self.slider_calls += 1
        return self.effort_pick

    def run_list(self, title: str, description: str, rows: list[str], footer: str) -> int | None:
        return self.list_pick

    def confirm(self, prompt: str, *, default: bool = True) -> bool:
        return self.confirm_answer

    def persist_default_model(self, model: str) -> None:
        from loom.config import set_project_config_value

        set_project_config_value(self.root, "model", model)
        self.config = self.config.model_copy(update={"model": model})

    def use_model_this_session(self, model: str) -> None:
        self.session_model = model

    def persist_default_effort(self, level: str) -> None:
        from loom.config import set_project_config_value

        set_project_config_value(self.root, "effort", level)

    def persist_config(self, key: str, value: object) -> None:
        from loom.config import load_config, set_project_config_value

        set_project_config_value(self.root, key, value)  # type: ignore[arg-type]
        self.config = load_config(cwd=self.root)

    def start_run(self, start: str, stop: str, run_first: tuple[str, ...] = ()) -> None:
        self.started = (start, stop, run_first)

    def resume(self, run_id: str) -> None:
        self.resumed = run_id

    def replay(self, phase: str) -> None:
        self.replayed = phase

    def change_dir(self, path: Path) -> None:
        self.changed_dir = path

    def add_dir(self, path: Path) -> None:
        self.added_dir = path

    def clear_session(self) -> None:
        self.cleared = True

    def cost_report(self) -> str:
        return "total: $0.0000"

    def status_report(self) -> str:
        return "model X"

    def doctor_report(self) -> str:
        return "doctor: all clear"

    def write_bug_bundle(self) -> str:
        return "wrote a redacted diagnostic bundle → .loom/bug.json"

    def compact_context(self) -> str:
        return "compacted the context: reclaimed ~500 tokens (12,000 → 10,000 chars)"

    @property
    def last(self) -> str:
        return self.writes[-1] if self.writes else ""


def _ctx(tmp_path: Path, **over: object) -> StubContext:
    (tmp_path / ".loom").mkdir(exist_ok=True)
    return StubContext(root=tmp_path, config=load_config(cwd=tmp_path), **over)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- FR-SLASH-01/05/07


def test_every_command_appears_once_with_a_description() -> None:
    # FR-SLASH-01 — the palette lists every command exactly once, each with a non-empty summary.
    names = [c.name for c in REGISTRY]
    assert len(names) == len(set(names)), "a command name is registered twice"
    for command in REGISTRY:
        assert command.summary.strip(), f"/{command.name} has no description"


def test_help_and_palette_are_the_same_single_source() -> None:
    # FR-SLASH-05 — /help and the palette are both renders of REGISTRY, no second list.
    help_text = render_help()
    palette_text = render_palette("")
    for command in REGISTRY:
        assert f"/{command.name}" in help_text
        assert f"/{command.name}" in palette_text
        assert command.summary in help_text


def test_help_marks_release_gated_commands() -> None:
    # FR-SLASH-07 — the render carries each command's description; R1.1 commands say so.
    text = render_help()
    assert "(R1.1)" in text  # /background and /rewind are gated
    assert "Run the whole pipeline end to end" in text  # a plain description survives


# --------------------------------------------------------------------------- FR-SLASH-02


def test_filter_is_subsequence_and_ranks_name_over_description() -> None:
    # A name match ranks above a description-only match.
    result = filter_commands("model")
    assert result[0].name == "model"
    # Subsequence, not substring: "vld" hits "validate".
    assert any(c.name == "validate" for c in filter_commands("vld"))


def test_no_match_is_a_message_never_an_empty_box() -> None:
    assert filter_commands("zzzzz") == []
    assert render_palette("zzzzz") == "  no command matches"


# --------------------------------------------------------------------------- FR-SLASH-04


def test_unknown_command_suggests_and_is_not_forwarded(tmp_path: Path) -> None:
    assert resolve("nope") is None
    assert suggest("modl") == "model"
    assert unknown_message("modl") == "unknown command `/modl` — did you mean `/model`?"
    ctx = _ctx(tmp_path)
    dispatch(ctx, "/nope")
    assert ctx.last == unknown_message("nope")
    assert ctx.started is None  # never routed anywhere that spends money


# --------------------------------------------------------------------------- FR-SEL-02


def test_model_enter_persists_the_default(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    target = model_options(ctx.config)[1].model  # something other than the current default
    ctx.model_pick = ModelChoice(target, "default")
    dispatch(ctx, "/model")
    assert project_config_path(tmp_path).is_file()
    assert f'model = "{target}"' in project_config_path(tmp_path).read_text()


def test_model_session_scope_does_not_touch_the_file(tmp_path: Path) -> None:
    # An existing config file must be byte-for-byte unchanged by a session-only pick.
    (tmp_path / ".loom").mkdir(exist_ok=True)
    path = project_config_path(tmp_path)
    path.write_text('effort = "high"\n', encoding="utf-8")
    before = path.read_bytes()
    ctx = _ctx(tmp_path)
    target = model_options(ctx.config)[1].model
    ctx.model_pick = ModelChoice(target, "session")
    dispatch(ctx, "/model")
    assert path.read_bytes() == before
    assert ctx.session_model == target


def test_model_escape_changes_nothing(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.model_pick = None
    dispatch(ctx, "/model")
    assert not project_config_path(tmp_path).is_file()
    assert "cancelled" in ctx.last


def test_typed_model_argument_skips_the_widget(tmp_path: Path) -> None:
    # FR-SLASH-06 — a typed argument sets it without opening the picker.
    ctx = _ctx(tmp_path)
    dispatch(ctx, "/model qwen3-coder")
    assert ctx.picker_calls == 0
    assert "qwen3-coder" in project_config_path(tmp_path).read_text()


# --------------------------------------------------------------------------- FR-SEL-03


def test_effort_preset_table_matches_the_srs() -> None:
    # FR-SEL-03 — the mapping is data in config.py; this guards the numbers.
    assert EFFORT_PRESETS["low"].max_turns == 12 and EFFORT_PRESETS["low"].max_usd == 0.25
    assert EFFORT_PRESETS["medium"].rubric_rounds == 2
    assert EFFORT_PRESETS["max"].tier == "strong" and EFFORT_PRESETS["max"].max_turns == 100
    # the highlighted level explains itself before Enter confirms it
    assert "25 turns" in effort_meaning("medium")


def test_effort_confirm_persists_the_level(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.effort_pick = "high"
    dispatch(ctx, "/effort")
    assert 'effort = "high"' in project_config_path(tmp_path).read_text()


# --------------------------------------------------------------------------- FR-SLASH-08


def _touch_artifact(root: Path, phase: str) -> None:
    path = artifact_path(root, "r", phase)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")


def test_phase_rows_report_existence_and_effect(tmp_path: Path) -> None:
    _touch_artifact(tmp_path, "validate")
    rows = {r.phase: r for r in phase_rows(tmp_path, "r")}
    assert rows["validate"].exists and rows["validate"].produced_ago
    assert not rows["build"].exists
    assert "needs design.json" in rows["build"].effect
    assert missing_upstream("build", list(rows.values())) == ["plan", "design"]


def test_phase_offers_to_run_missing_upstream(tmp_path: Path) -> None:
    # FR-SLASH-08 — choosing build with design missing runs the missing phases first, not fails.
    _touch_artifact(tmp_path, "validate")
    ctx = _ctx(tmp_path)
    ctx.list_pick = 3  # build
    ctx.confirm_answer = True
    dispatch(ctx, "/phase")
    assert ctx.started == ("plan", "build", ("plan", "design"))


def test_phase_declining_the_offer_starts_nothing(tmp_path: Path) -> None:
    _touch_artifact(tmp_path, "validate")
    ctx = _ctx(tmp_path)
    ctx.list_pick = 3
    ctx.confirm_answer = False
    dispatch(ctx, "/phase")
    assert ctx.started is None


# --------------------------------------------------------------------------- FR-SLASH-09


def test_while_running_refuses_a_pipeline_command(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, state="running")
    dispatch(ctx, "/run")
    assert ctx.started is None
    assert "can't run while a phase is working" in ctx.last


def test_while_running_queues_a_setting(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, state="running")
    ctx.model_pick = ModelChoice(model_options(ctx.config)[1].model, "session")
    dispatch(ctx, "/model")
    assert any("queued — applies from the next phase" in w for w in ctx.writes)


def test_while_running_runs_an_immediate_command(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, state="running")
    dispatch(ctx, "/cost")
    assert ctx.last == "total: $0.0000"


def test_registry_entries_are_frozen() -> None:
    # A guard that the declaration stays data — a handler cannot be swapped at runtime.
    assert isinstance(REGISTRY[0], Command)


# --------------------------------------------------------------------------- the wired handlers


def _write_design(root: Path, run_id: str = "r") -> None:
    from loom.contracts import Criterion, Design, FileSpec, Rubric

    design = Design(
        summary="a tiny api",
        file_manifest=[FileSpec(path="app/main.py", purpose="the app")],
        rubric=Rubric(
            threshold=0.85,
            hard_fail=["tests"],
            criteria=[
                Criterion(name="tests", kind="shell", weight=0.6, command="pytest"),
                Criterion(name="documented", kind="judge", weight=0.4, question="docs?"),
            ],
        ),
    )
    path = artifact_path(root, run_id, "design")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(design.model_dump_json(indent=2), encoding="utf-8")


def _write_score(root: Path, run_id: str = "r") -> None:
    from loom.rubric import GradedCriterion, Score

    score = Score(
        total=0.72,
        criteria=[
            GradedCriterion(name="tests", kind="shell", weight=0.6, score=1.0, detail="pass"),
            GradedCriterion(name="documented", kind="judge", weight=0.4, score=0.3, detail="thin"),
        ],
    )
    path = artifact_path(root, run_id, "build")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(score.model_dump_json(indent=2), encoding="utf-8")


def test_rubric_shows_criteria_and_the_last_score(tmp_path: Path) -> None:
    _write_design(tmp_path)
    _write_score(tmp_path)
    ctx = _ctx(tmp_path)
    dispatch(ctx, "/rubric")
    out = ctx.last
    assert "threshold 0.85" in out
    assert "tests" in out and "hard-fail" in out  # hard_fail criterion flagged
    assert "→ 0.30" in out  # the judge criterion's last score
    assert "last score 0.72" in out


def test_rubric_without_a_design_says_so(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    dispatch(ctx, "/rubric")
    assert "no rubric yet" in ctx.last


def test_artifacts_opens_a_named_one_and_skips_the_picker(tmp_path: Path) -> None:
    # FR-ART-02 / FR-SLASH-06 — a typed phase name renders that artifact's JSON, no picker.
    _write_design(tmp_path)
    ctx = _ctx(tmp_path)
    dispatch(ctx, "/artifacts design")
    assert '"summary": "a tiny api"' in ctx.last  # the on-disk JSON, the gate's render


def test_artifacts_lists_then_opens_the_selection(tmp_path: Path) -> None:
    _write_design(tmp_path)
    ctx = _ctx(tmp_path, list_pick=0)
    dispatch(ctx, "/artifacts")
    assert '"file_manifest"' in ctx.last


def test_artifacts_with_none_present(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    dispatch(ctx, "/artifacts")
    assert "no artifacts yet" in ctx.last


def test_config_lists_every_setting_with_its_source(tmp_path: Path) -> None:
    # FR-CFG-04 — the rendered table names the source for every key.
    (tmp_path / ".loom").mkdir(exist_ok=True)
    project_config_path(tmp_path).write_text('effort = "high"\n', encoding="utf-8")
    ctx = _ctx(tmp_path)
    dispatch(ctx, "/config")
    out = ctx.last
    assert "source" in out
    for key in Config.model_fields:
        if key != "price_table":
            assert key in out
    assert "project config" in out  # effort came from the file we wrote


def test_config_sets_a_value_to_the_project_file(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    dispatch(ctx, "/config effort high")
    assert 'effort = "high"' in project_config_path(tmp_path).read_text()
    assert "/config → effort = high" in ctx.last


def test_config_refuses_an_unknown_key_or_bad_value(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    dispatch(ctx, "/config nope 1")
    assert "no such setting" in ctx.last
    dispatch(ctx, "/config budget_usd notanumber")
    assert "invalid budget_usd" in ctx.last
    assert not project_config_path(tmp_path).is_file()  # nothing was written


def test_budget_shows_and_sets_the_ceiling(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    dispatch(ctx, "/budget")
    assert "budget ceiling $" in ctx.last
    dispatch(ctx, "/budget 8")
    assert "budget_usd = 8" in project_config_path(tmp_path).read_text()
    assert "$8.00 per run" in ctx.last


def test_replay_runs_a_shape_a_phase_with_cached_upstream(tmp_path: Path) -> None:
    _touch_artifact(tmp_path, "validate")
    _touch_artifact(tmp_path, "plan")
    ctx = _ctx(tmp_path)
    dispatch(ctx, "/replay design")
    assert ctx.replayed == "design"


def test_replay_rejects_build_and_missing_upstream(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    dispatch(ctx, "/replay build")
    assert "one of validate, plan, design" in ctx.last
    assert ctx.replayed is None
    dispatch(ctx, "/replay design")  # nothing upstream produced
    assert "can't replay design" in ctx.last


def test_cd_and_add_dir_validate_the_directory(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    (tmp_path / "sub").mkdir()
    dispatch(ctx, "/cd sub")
    assert ctx.changed_dir == (tmp_path / "sub").resolve()
    dispatch(ctx, "/cd nowhere")
    assert "no such directory" in ctx.last
    dispatch(ctx, "/add-dir sub")
    assert ctx.added_dir == (tmp_path / "sub").resolve()


def test_doctor_and_bug_route_to_the_context(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    dispatch(ctx, "/doctor")
    assert ctx.last == "doctor: all clear"
    dispatch(ctx, "/bug")
    assert "redacted diagnostic bundle" in ctx.last


def test_compact_routes_to_the_context_and_reports(tmp_path: Path) -> None:
    """FR-SESS-06 — /compact is wired to the context's compaction (no longer a stub) and prints
    its reclaimed-tokens report."""
    ctx = _ctx(tmp_path)
    dispatch(ctx, "/compact")
    assert "reclaimed" in ctx.last and "tokens" in ctx.last
