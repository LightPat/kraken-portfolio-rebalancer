SHELL := /bin/bash

.PHONY: run

UV_CACHE_DIR=/var/cache/uv-rebalancer
UV_PYTHON_INSTALL_DIR=/var/lib/uv-rebalancer/python

run:; set -a; source .env; set +a; dcli exec -- uv run main.py

run-bot:; set -a; source .env; set +a; uv run bot.py
run-bot-dcli:; set -a; source .env; set +a; dcli exec -- uv run bot.py

# Production API (no auto-reload - best for trading bot on VPS)
run-api:; set -a; source .env; set +a; uv run uvicorn app:app --host 0.0.0.0 --port 8000
run-api-dcli:; set -a; source .env; set +a; dcli exec -- uv run uvicorn app:app --host 0.0.0.0 --port 8000

# Development API with auto-reload (use this while you're coding)
run-api-dev:; set -a; source .env; set +a; dcli exec -- uv run uvicorn app:app --host 0.0.0.0 --port 8000 --reload

# Server side commands
server-run-api:; sudo -u rebalancer bash -c 'set -a; source /etc/kraken-secrets/kraken.env; set +a; UV_CACHE_DIR=$(UV_CACHE_DIR) UV_PYTHON_INSTALL_DIR=$(UV_PYTHON_INSTALL_DIR) uv run uvicorn app:app --host 0.0.0.0 --port 8000'
server-run-bot:; sudo -u rebalancer bash -c 'set -a; source /etc/kraken-secrets/kraken.env; set +a; UV_CACHE_DIR=$(UV_CACHE_DIR) UV_PYTHON_INSTALL_DIR=$(UV_PYTHON_INSTALL_DIR) uv run bot.py'