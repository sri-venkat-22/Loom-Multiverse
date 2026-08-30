.PHONY: check test fast lint types cassettes

check: lint types test

test:
	uv run pytest -q

# The on-every-save tier: no real git, no full build loops. Under 10s (NFR-TEST-01).
fast:
	uv run pytest -q -m "not slow"

lint:
	uv run ruff check . && uv run ruff format --check .

types:
	uv run mypy loom

# Re-record the provider cassettes. Real API calls, real money — see tests/cassettes/.
cassettes:
	uv run pytest -m live tests/test_providers.py -k record -q
