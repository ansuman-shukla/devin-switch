.PHONY: format lint test pre-commit app

format:
	uv run ruff format .

lint:
	uv run ruff check .
	uv run ruff format --check .

test:
	uv run pytest -q --tb=short

pre-commit: format lint test

app:
	uv tool install --force --python 3.11 .
	uv run python scripts/build_app.py
