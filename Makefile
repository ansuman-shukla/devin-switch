.PHONY: help setup format format-check lint test typecheck secrets check pre-commit install app build dmg smoke-app

help:
	@printf '%s\n' \
	  'make setup        Install locked development dependencies with uv' \
	  'make check        Check lint, formatting, tests, Swift, and secrets' \
	  'make format       Format Python source and tests' \
	  'make install      Install the ds CLI with uv' \
	  'make app          Build and install a local macOS app' \
	  'make build        Build the Python wheel and source archive' \
	  'make dmg          Build a portable macOS app and disk image' \
	  'make smoke-app    Verify the portable app offline in isolated state'

setup:
	uv sync --locked --python 3.11

format:
	uv run --locked ruff format .

format-check:
	uv run --locked ruff format --check .

lint:
	uv run --locked ruff check .
	$(MAKE) format-check

test:
	uv run --locked pytest -q --tb=short

typecheck:
	swiftc -typecheck -parse-as-library -target "$$(uname -m)-apple-macos14.0" -framework SwiftUI -framework AppKit macos/DevinSwitch.swift macos/AccountViews.swift

secrets:
	uv run --locked python scripts/check_secrets.py

check: lint test typecheck secrets

pre-commit: format check

install:
	env -u XDG_DATA_HOME -u XDG_CONFIG_HOME -u XDG_CACHE_HOME -u XDG_STATE_HOME uv tool install --force --reinstall-package devin-switch --python 3.11 .

app: install
	env -u XDG_DATA_HOME -u XDG_CONFIG_HOME -u XDG_CACHE_HOME -u XDG_STATE_HOME uv run --locked python scripts/build_app.py

build:
	uv build

dmg:
	uv run --locked --group build --python 3.11 python scripts/build_app.py --standalone --dmg

smoke-app:
	uv run --locked python scripts/smoke_app.py "dist/Devin Switch.app"
