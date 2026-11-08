SHELL := /bin/bash
SCRIPTS := lab/scripts
VENV := .venv
PY := $(VENV)/bin/python

DELAY ?= 150
LOSS ?= 5

.PHONY: help lab-up lab-down status test cover e2e degrade-a restore-a degrade-b restore-b \
        optimizer-start optimizer-logs venv clean

help:
	@echo "lab-up        build images and deploy the containerlab lab"
	@echo "lab-down      destroy the lab"
	@echo "status        BGP best path on edge, traceroute client->target, optimizer status"
	@echo "test          unit tests (pytest, local .venv)"
	@echo "cover         unit tests with coverage report"
	@echo "e2e           end-to-end scenario against a running lab"
	@echo "degrade-a     netem on edge<->ISP-A (DELAY=$(DELAY) ms, LOSS=$(LOSS) %)"
	@echo "restore-a     remove netem from edge<->ISP-A"
	@echo "degrade-b / restore-b   same for ISP-B"
	@echo "optimizer-start   start the optimizer node again (after docker kill)"
	@echo "optimizer-logs    follow optimizer container logs"

lab-up:
	$(SCRIPTS)/deploy.sh

lab-down:
	$(SCRIPTS)/destroy.sh

status:
	$(SCRIPTS)/status.sh

STAMP := $(VENV)/.installed

$(STAMP): pyproject.toml
	python3 -m venv $(VENV)
	$(PY) -m pip install -q --disable-pip-version-check -e '.[dev]'
	@touch $(STAMP)

venv: $(STAMP)

test: $(STAMP)
	$(PY) -m pytest tests/unit

cover: $(STAMP)
	$(VENV)/bin/coverage run --branch -m pytest tests/unit
	$(VENV)/bin/coverage report -m --include='optimizer/*'

e2e:
	tests/e2e/e2e.sh

degrade-a:
	$(SCRIPTS)/degrade.sh a $(DELAY) $(LOSS)

restore-a:
	$(SCRIPTS)/restore.sh a

degrade-b:
	$(SCRIPTS)/degrade.sh b $(DELAY) $(LOSS)

restore-b:
	$(SCRIPTS)/restore.sh b

optimizer-start:
	$(SCRIPTS)/optimizer-start.sh

optimizer-logs:
	docker logs -f clab-bgpopt-optimizer

clean:
	rm -rf $(VENV) .pytest_cache *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
