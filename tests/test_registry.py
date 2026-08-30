"""FR-TOOL-01 — schema generation from a typed handler, and errors the model can act on."""

from __future__ import annotations

from typing import Annotated, Literal

import pytest
from pydantic import Field

from loom.agent.tools.registry import Tool, ToolNotFound, ToolRegistry, tool


@tool
def read_file(
    path: Annotated[str, Field(description="Workspace-relative path.")],
    start_line: int | None = None,
) -> str:
    """Read a file.

    Longer prose that the schema should not carry into the one-line description.
    """
    return f"{path}:{start_line}"


@tool(name="run_bash")
async def _bash(command: str, timeout: int = 5) -> str:
    """Run a shell command."""
    return f"ran {command} for {timeout}"


def test_a_typed_handler_produces_an_openai_tool_schema() -> None:
    spec = read_file.spec()
    assert spec["type"] == "function"
    function = spec["function"]
    assert function["name"] == "read_file"
    assert function["description"].startswith("Read a file.")
    params = function["parameters"]
    assert params["type"] == "object"
    assert set(params["properties"]) == {"path", "start_line"}
    assert params["required"] == ["path"]  # only the one without a default
    assert params["properties"]["path"]["description"] == "Workspace-relative path."


def test_the_name_defaults_to_the_function_and_can_be_overridden() -> None:
    assert read_file.name == "read_file"
    assert _bash.name == "run_bash"


def test_optional_types_and_literals_survive_schema_generation() -> None:
    @tool
    def pick(kind: Literal["shell", "judge"], note: str = "") -> str:
        """Pick one."""
        return kind

    props = pick.spec()["function"]["parameters"]["properties"]
    assert props["kind"]["enum"] == ["shell", "judge"]
    assert props["note"]["default"] == ""


async def test_execute_calls_a_sync_handler() -> None:
    registry = ToolRegistry([read_file])
    assert await registry.execute("read_file", {"path": "a.py"}) == "a.py:None"


async def test_execute_awaits_an_async_handler() -> None:
    registry = ToolRegistry([_bash])
    assert await registry.execute("run_bash", {"command": "pytest"}) == "ran pytest for 5"


async def test_an_unknown_name_raises_a_structured_error_never_a_keyerror() -> None:
    """FR-TOOL-01 — and FR-AGENT-05 depends on `available` to build its tool message."""
    registry = ToolRegistry([read_file, _bash])
    with pytest.raises(ToolNotFound) as caught:
        await registry.execute("read_flie", {})
    assert caught.value.name == "read_flie"
    assert caught.value.available == ["read_file", "run_bash"]
    assert "read_file" in str(caught.value)


async def test_bad_arguments_return_a_readable_error_rather_than_raising() -> None:
    """The model has to be able to correct itself from this string alone."""
    registry = ToolRegistry([read_file])
    result = await registry.execute("read_file", {})
    assert result.startswith("ERROR")
    assert "path" in result and "required" in result.lower()


async def test_wrong_types_are_coerced_where_pydantic_can_and_reported_where_it_cannot() -> None:
    registry = ToolRegistry([_bash])
    assert await registry.execute("run_bash", {"command": "x", "timeout": "9"}) == "ran x for 9"
    bad = await registry.execute("run_bash", {"command": "x", "timeout": "soon"})
    assert bad.startswith("ERROR") and "timeout" in bad


async def test_unexpected_arguments_are_ignored_not_fatal() -> None:
    """A stray key is not worth failing a call the model otherwise got right."""
    registry = ToolRegistry([read_file])
    assert await registry.execute("read_file", {"path": "a.py", "reasoning": "..."}) == "a.py:None"


async def test_a_handler_that_raises_propagates_for_the_loop_to_frame() -> None:
    """FR-AGENT-04 puts the error in a tool message; the registry does not swallow it here,
    because a registry that hides exceptions hides bugs in the tools too."""

    @tool
    def boom() -> str:
        """Always fails."""
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError, match="kaboom"):
        await ToolRegistry([boom]).execute("boom", {})


def test_a_registry_is_a_set_of_names() -> None:
    registry = ToolRegistry([read_file, _bash])
    assert registry.names == ["read_file", "run_bash"]
    assert "read_file" in registry and "write_file" not in registry
    assert len(registry) == 2
    assert [s["function"]["name"] for s in registry.specs()] == ["read_file", "run_bash"]


def test_duplicate_names_are_refused() -> None:
    with pytest.raises(ValueError, match="already registered"):
        ToolRegistry([read_file, read_file])


def test_a_registry_can_be_built_empty_and_added_to() -> None:
    """SEC-03 — Shape A phases construct one *without* the fs and bash tools."""
    registry = ToolRegistry()
    assert registry.names == [] and registry.specs() == []
    registry.add(read_file)
    assert registry.names == ["read_file"]


def test_handlers_are_plain_functions_so_a_tool_stays_callable() -> None:
    assert isinstance(read_file, Tool)
    assert read_file.handler("x.py", 3) == "x.py:3"


def test_a_handler_without_a_docstring_is_refused() -> None:
    """The description is what the model chooses on. An undescribed tool is a bug."""
    with pytest.raises(ValueError, match="description"):

        @tool
        def undocumented(x: str) -> str:
            return x
