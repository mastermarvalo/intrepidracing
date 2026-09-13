.PHONY: up down restart logs build lint test check-magic check

up:
	sudo docker compose up -d

down:
	sudo docker compose down

restart:
	sudo docker compose build && sudo docker compose up -d

logs:
	sudo docker compose logs -f

build:
	sudo docker compose build

# ── Local dev checks (no Docker) ────────────────────────────────────────

lint:
	uv run ruff check bot/ scripts/ tests/

test:
	uv run pytest

check-magic:
	uv run python scripts/check_magic_numbers.py

check: lint check-magic test
