SHELL := /bin/bash

.PHONY: run

run:; set -a; source .env; set +a; dcli exec -- uv run main.py


# Production API (no auto-reload - best for trading bot on VPS)
run-api:; set -a; source .env; set +a; uv run uvicorn app:app --host 0.0.0.0 --port 8000
run-api-dcli:; set -a; source .env; set +a; dcli exec -- uv run uvicorn app:app --host 0.0.0.0 --port 8000

# Development API with auto-reload (use this while you're coding)
run-api-dev:; set -a; source .env; set +a; dcli exec -- uv run uvicorn app:app --host 0.0.0.0 --port 8000 --reload