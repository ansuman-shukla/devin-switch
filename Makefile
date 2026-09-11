.PHONY: format lint test pre-commit

format:
	uv run ruff format .

lint:
	uv run ruff check .
	uv run ruff format --check .

test:
	uv run pytest -q --tb=short

pre-commit: format lint test
