"""FR-PIPE-05, WP-3.0 — the content-addressed phase cache.

The failure this guards against is the expensive one and the cheap one at once: a key that is
too coarse serves yesterday's artifact for today's prompt, and a key that is too fine never
hits and the cache is decoration.
"""

from __future__ import annotations

from pathlib import Path

from loom.cache import CONFIG_SLICE, PhaseCache, cache_key, config_slice
from loom.config import Config


def key(**over: object) -> str:
    base: dict[str, object] = {
        "phase": "design",
        "prompt": "you are designing",
        "task": "a url shortener",
        "model": "openrouter/qwen/qwen3-coder",
        "config": {"model": "openrouter/qwen/qwen3-coder", "max_turns": 25},
    }
    base.update(over)
    return cache_key(**base)  # type: ignore[arg-type]


def test_the_same_inputs_give_the_same_key() -> None:
    assert key() == key()


def test_every_part_of_the_key_actually_changes_it() -> None:
    """One byte anywhere busts it — the prompt especially, which is what gets tuned."""
    assert key(prompt="you are designing.") != key()
    assert key(task="a url shortener ") != key()
    assert key(model="anthropic/claude-sonnet-5") != key()
    assert key(phase="plan") != key()
    assert key(config={"model": "x", "max_turns": 25}) != key()


def test_parts_cannot_smear_into_each_other() -> None:
    """Without a separator, ("ab", "c") and ("a", "bc") hash the same and one phase serves
    another phase's answer. This is the test that says the separator is load-bearing."""
    assert key(prompt="ab", task="c") != key(prompt="a", task="bc")


def test_config_keys_that_cannot_change_the_answer_stay_out_of_the_key() -> None:
    """A user trying a colour scheme should not pay to re-run every phase."""
    assert "theme" not in CONFIG_SLICE
    assert set(config_slice(Config())) == set(CONFIG_SLICE)


def test_a_hit_returns_exactly_what_was_stored(tmp_path: Path) -> None:
    cache = PhaseCache(tmp_path)
    cache.put(key(), '{"summary": "x"}', phase="design")
    assert cache.get(key()) == '{"summary": "x"}'


def test_a_miss_is_none_not_an_error(tmp_path: Path) -> None:
    assert PhaseCache(tmp_path).get(key()) is None


def test_the_cache_lives_where_it_is_safe_to_delete(tmp_path: Path) -> None:
    cache = PhaseCache(tmp_path)
    path = cache.put(key(), "{}", phase="design")
    assert path is not None
    assert path.parent == tmp_path / ".loom" / "cache"


def test_a_corrupt_entry_is_a_miss(tmp_path: Path) -> None:
    """Half a JSON file from a killed process must cost one re-run, not a crash."""
    cache = PhaseCache(tmp_path)
    cache.put(key(), "{}", phase="design")
    cache.path_for(key()).write_text('{"artifact": ', encoding="utf-8")
    assert cache.get(key()) is None


def test_disabled_is_the_same_code_path(tmp_path: Path) -> None:
    """`--no-cache` — nothing is read and nothing is written, with no branch at the call site."""
    off = PhaseCache(tmp_path, enabled=False)
    assert off.put(key(), "{}") is None
    PhaseCache(tmp_path).put(key(), '{"summary": "x"}')
    assert off.get(key()) is None
