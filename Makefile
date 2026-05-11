SHELL := /bin/bash

.PHONY: dev-run-api, dev-run-bot, server-run-api, server-run-bot

UV_CACHE_DIR=/var/cache/uv-rebalancer
UV_PYTHON_INSTALL_DIR=/var/lib/uv-rebalancer/python

# Client/Dev API
dev-run-api:; set -a; source .env; set +a; dcli exec -- uv run uvicorn app:app --host 0.0.0.0 --port 8000
dev-run-bot:; set -a; source .env; set +a; dcli exec -- uv run bot.py

# Server side commands
server-run-api:; sudo -u rebalancer bash -c 'set -a; source /etc/kraken-secrets/kraken.env; set +a; UV_CACHE_DIR=$(UV_CACHE_DIR) UV_PYTHON_INSTALL_DIR=$(UV_PYTHON_INSTALL_DIR) uv run uvicorn app:app --host 127.0.0.1 --port 8000'
server-run-bot:; sudo -u rebalancer bash -c 'set -a; source /etc/kraken-secrets/kraken.env; set +a; UV_CACHE_DIR=$(UV_CACHE_DIR) UV_PYTHON_INSTALL_DIR=$(UV_PYTHON_INSTALL_DIR) uv run bot.py'