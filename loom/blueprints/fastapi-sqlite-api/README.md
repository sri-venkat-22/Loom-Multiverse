# `fastapi-sqlite-api` blueprint

A partial Design for the most common thing a founder asks Loom to build: a small REST API
backed by a file database. It pins a known-good FastAPI + standard-library `sqlite3` layout, a
scaffold command, an expected file manifest, and three rubric criteria — including one
acceptance check (`serves_openapi`) the build cannot satisfy by weakening its own tests.

## Use it

```bash
loom run "A REST API for a simple to-do list" --blueprint fastapi-sqlite-api
```

`--blueprint` also takes a path to this directory or to its `blueprint.json`. The blueprint is
merged into the Design phase's output: on any conflict the blueprint wins, and a
`blueprint_merged` event records what it overrode.

## Fixture idea (FR-BP-02)

Its target is the `todo-api` eval fixture — *"A REST API for a simple to-do list using
FastAPI."* Presence of this blueprint must measurably raise the mean eval score. To measure,
run the eval fixtures once with `Config(blueprint="fastapi-sqlite-api")` on the FastAPI-shaped
ideas (`todo-api`, `unit-converter`) and once without, and compare `results/report.json`.

## What it deliberately does not pin

No scaffold *assets* (template files copied into the repo) — only a scaffold *command* and a
file manifest the build phase writes itself. Blueprint-shipped source templates are a later
concern; see WP-6.2b.
