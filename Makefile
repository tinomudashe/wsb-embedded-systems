SHELL := /bin/bash

# Prefer a modern interpreter when one is installed (CI pins 3.12 separately).
# The check-python target below refuses anything older than 3.12.
PYTHON ?= $(or $(shell for p in python3.12 python3.14 python3.13 python3; do \
	command -v $$p >/dev/null 2>&1 && { echo $$p; break; }; done),python3)

VENV := server/.venv
PIP := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest
STAMP := $(VENV)/.requirements-installed

.PHONY: help check-python venv test test-cov build-web build-firmware dev-up dev-down clean

help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z0-9_-]+:.*?## / {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

check-python:
	@command -v $(PYTHON) >/dev/null 2>&1 || { echo "error: no Python interpreter found; set PYTHON=/path/to/python3.12"; exit 1; }
	@$(PYTHON) -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else "error: Python 3.12+ required (found %d.%d); set PYTHON=/path/to/python3.12" % sys.version_info[:2])'

$(STAMP): check-python server/requirements.txt server/requirements-dev.txt
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r server/requirements.txt -r server/requirements-dev.txt
	@touch $(STAMP)

venv: $(STAMP) ## Create server/.venv and install runtime + dev requirements

test: $(STAMP) ## Run the backend test suite
	cd server && .venv/bin/pytest

test-cov: $(STAMP) ## Run tests with a coverage report
	cd server && .venv/bin/pytest --cov=app --cov-report=term-missing

build-web: ## Install web deps (npm ci) and build into server/static
	cd web && npm ci && npm run build

build-firmware: ## Compile the ESP32 CYD firmware (requires pio)
	@command -v pio >/dev/null 2>&1 || { echo "error: pio not found; install PlatformIO first (pip install platformio)"; exit 1; }
	@if [ ! -f firmware/include/secrets.h ]; then \
		echo "firmware/include/secrets.h missing; copying secrets.example.h"; \
		echo "edit the copied file with your WiFi/device credentials before flashing"; \
		cp firmware/include/secrets.example.h firmware/include/secrets.h; \
	fi
	cd firmware && pio run -e cyd

dev-up: ## Start the local API + Mosquitto with docker compose
	@if [ ! -f .env ]; then \
		echo "error: .env is missing; run 'cp .env.example .env' and set JWT_SECRET/MQTT_PASS"; \
		exit 1; \
	fi
	docker compose up --build -d

dev-down: ## Stop the local docker compose stack
	docker compose down

clean: ## Remove build/test artifacts (keeps server/.venv and server/static)
	rm -rf web/dist server/.pytest_cache server/htmlcov server/.coverage
	find server -type d -name __pycache__ -prune -exec rm -rf {} +
