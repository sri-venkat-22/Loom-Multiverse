.PHONY: check test fast lint types cassettes milestone pipeline

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

# WP-4.5. Real API calls and real money; prints a full report either way.
milestone:
	uv run pytest -m live tests/e2e/test_build_urlshortener.py -q -s

# WP-4.7. The whole pipeline from an idea string. Costs more than `milestone`; same deal.
pipeline:
	uv run pytest -m live tests/e2e/test_pipeline_urlshortener.py -q -s
