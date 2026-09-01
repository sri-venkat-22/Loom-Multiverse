"""Blueprint loader and merge. WP-6.1 — FR-BP-01.

A blueprint is a partial Design that pre-loads known-good choices for a family of apps: a
pinned stack, a scaffold command, an expected file manifest and rubric fragments. `--blueprint
<path>` loads one and merges it into the Design phase's output, blueprint-wins on every
conflict, with one event logged so the override is never silent.

The blueprint file is `blueprint.json` — a subset of the `Design` schema with every field
optional. Nested manifests and weighted criteria are native in JSON and painful in TOML, and
the Pydantic models already exist, so JSON it is.

Nothing here imports a terminal library; the merge is a pure function over `contracts` models.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from loom.contracts import Criterion, Design, FileSpec, Rubric

#: `loom/blueprints/` — where first-party blueprints named by `--blueprint <name>` are found.
FIRST_PARTY = Path(__file__).resolve().parent

#: The file inside a blueprint directory that holds the partial Design.
BLUEPRINT_FILE = "blueprint.json"


class BlueprintRubric(BaseModel):
    """Rubric fragments a blueprint contributes. Criteria are full `Criterion`s — they are
    pinned known-good, not sketches — but every field of the rubric itself is optional."""

    model_config = ConfigDict(extra="forbid")

    criteria: list[Criterion] = Field(default_factory=list)
    threshold: float | None = None
    hard_fail: list[str] = Field(default_factory=list)


class Blueprint(BaseModel):
    """A partial `Design`. Every field optional: a blueprint pins only what it knows."""

    model_config = ConfigDict(extra="forbid")

    name: str = ""
    stack: list[str] = Field(default_factory=list)
    scaffold_command: str | None = None
    file_manifest: list[FileSpec] = Field(default_factory=list)
    rubric: BlueprintRubric | None = None


def resolve_blueprint_path(ref: str) -> Path:
    """A `--blueprint` value to the `blueprint.json` it names.

    Accepts a directory (its `blueprint.json`), a file (used directly), or a first-party name
    like `fastapi-sqlite-api` (resolved under `loom/blueprints/`).
    """
    p = Path(ref).expanduser()
    if p.is_dir():
        return p / BLUEPRINT_FILE
    if p.is_file():
        return p
    first_party = FIRST_PARTY / ref / BLUEPRINT_FILE
    if first_party.is_file():
        return first_party
    names = sorted(d.name for d in FIRST_PARTY.iterdir() if (d / BLUEPRINT_FILE).is_file())
    raise FileNotFoundError(
        f"no blueprint at {ref!r}. Pass a path to a blueprint directory or its "
        f"{BLUEPRINT_FILE}, or a first-party name ({', '.join(names) or 'none installed'})."
    )


def load_blueprint(ref: str) -> Blueprint:
    """Load and validate the blueprint `ref` names. Raises if it is missing or malformed."""
    path = resolve_blueprint_path(ref)
    return Blueprint.model_validate_json(path.read_text(encoding="utf-8"))


def merge_blueprint(design: Design, blueprint: Blueprint) -> tuple[Design, list[str]]:
    """Merge the blueprint into a produced Design, blueprint-wins, returning the conflicts.

    The returned list names every field the blueprint overrode (`scaffold_command`,
    `file:<path>`, `criterion:<name>`, `threshold`) so the phase can log them. Stack and
    hard_fail are unions rather than overrides, so they are never conflicts.

    Rubric criteria merge by name, then all weights are renormalised to sum to 1.0 — merging two
    independently-weighted rubrics is the only way to keep the result gradable, and it is what
    makes the merged Design pass the Design phase's own weight-sum gate.
    """
    conflicts: list[str] = []

    # Stack: blueprint's canonical entries first, then the model's extras, de-duplicated.
    stack = [*blueprint.stack, *(s for s in design.stack if s not in blueprint.stack)]

    scaffold_command = design.scaffold_command
    if blueprint.scaffold_command is not None and blueprint.scaffold_command != scaffold_command:
        conflicts.append("scaffold_command")
        scaffold_command = blueprint.scaffold_command

    # File manifest by path; dict preserves the model's order and appends blueprint-new files.
    by_path: dict[str, FileSpec] = {f.path: f for f in design.file_manifest}
    for spec in blueprint.file_manifest:
        if spec.path in by_path:
            conflicts.append(f"file:{spec.path}")
        by_path[spec.path] = spec

    rubric = design.rubric
    if blueprint.rubric is not None:
        by_name: dict[str, Criterion] = {c.name: c for c in design.rubric.criteria}
        for c in blueprint.rubric.criteria:
            if c.name in by_name:
                conflicts.append(f"criterion:{c.name}")
            by_name[c.name] = c
        criteria = list(by_name.values())
        total = sum(c.weight for c in criteria)
        # ponytail: renormalise proportionally. total is always > 0 (weights are gt 0).
        criteria = [c.model_copy(update={"weight": c.weight / total}) for c in criteria]

        threshold = design.rubric.threshold
        bp_threshold = blueprint.rubric.threshold
        if bp_threshold is not None and bp_threshold != threshold:
            conflicts.append("threshold")
            threshold = bp_threshold

        names = {c.name for c in criteria}
        hard_fail = [
            n
            for n in dict.fromkeys([*design.rubric.hard_fail, *blueprint.rubric.hard_fail])
            if n in names
        ]
        rubric = Rubric(criteria=criteria, threshold=threshold, hard_fail=hard_fail)

    merged = Design(
        summary=design.summary,
        stack=stack,
        scaffold_command=scaffold_command,
        file_manifest=list(by_path.values()),
        rubric=rubric,
    )
    return merged, conflicts
