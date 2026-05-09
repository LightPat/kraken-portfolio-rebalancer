SHELL := /bin/bash

.PHONY: run

run:; set -a; source .env; set +a; dcli exec -- uv run main.py