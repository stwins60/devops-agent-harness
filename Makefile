PYTHON ?= python
VENV   ?= .venv
BIN    := $(VENV)/Scripts
ifeq ($(OS),Windows_NT)
PY := $(BIN)/python.exe
else
BIN := $(VENV)/bin
PY := $(BIN)/python
endif

.PHONY: help venv install test lint demo demo-jira demo-incident demo-plan mock-server up down clean tools runbooks

help:
	@echo "make install      - create venv and install the harness in editable mode"
	@echo "make test         - run the test-suite"
	@echo "make demo         - run the four definition-of-done commands in --mock mode"
	@echo "make mock-server  - start the mock Jira/GitHub HTTP server on :8089"
	@echo "make up / down    - docker compose local environment"

venv:
	$(PYTHON) -m venv $(VENV)

install: venv
	$(PY) -m pip install -e ".[dev]"

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m compileall -q agent tools adapters apps

demo:
	$(PY) -m apps.cli.main --mock "Why is my Kubernetes API deployment failing?"
	$(PY) -m apps.cli.main --mock --yes jira DEVOPS-382
	$(PY) -m apps.cli.main --mock --yes incident "production API is returning 503"
	$(PY) -m apps.cli.main --mock plan "upgrade our Kubernetes worker nodes"

demo-jira:
	$(PY) -m apps.cli.main --mock --yes jira DEVOPS-382

demo-incident:
	$(PY) -m apps.cli.main --mock --yes incident "production API is returning 503"

demo-plan:
	$(PY) -m apps.cli.main --mock plan "upgrade our Kubernetes worker nodes"

mock-server:
	$(PY) -m apps.mockserver.server --port 8089

tools:
	$(PY) -m apps.cli.main --mock tools list

runbooks:
	$(PY) -m apps.cli.main runbooks list

up:
	docker compose up -d --build

down:
	docker compose down -v

clean:
	rm -rf tasks/*/ .agent/audit build dist *.egg-info
