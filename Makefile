# Convenience targets. Everything here is a short shell command you can also run by hand —
# nothing depends on make, and nothing is hidden behind it.

PYTHON ?= python3
PORT   ?= 8765
CLIENTS ?= 25
JOBS   ?= 2
DATA_DIR ?= $(CURDIR)/.loadtest-data

.PHONY: help test lint loadtest client-test client-browser-test docs docs-serve protocol-reference

help:
	@echo "test        run the Python test suite (add -m fenics where dolfinx is installed)"
	@echo "lint        ruff over the server package"
	@echo "client-test build and test the browser packages"
	@echo "client-browser-test  drive the viewer's gestures in a real Chromium"
	@echo "loadtest    start a server, run server/loadtest.py against it, stop it"
	@echo "            variables: CLIENTS=$(CLIENTS) JOBS=$(JOBS) PORT=$(PORT)"
	@echo "docs        build the documentation site into site/"
	@echo "docs-serve  serve the docs with live reload on :8001"
	@echo "protocol-reference  regenerate docs/reference-protocol.md from the models"

test:
	cd server && $(PYTHON) -m pytest -q

lint:
	cd server && $(PYTHON) -m ruff check .

# Regenerate first: the site should never be built from a stale protocol page.
docs: protocol-reference
	$(PYTHON) -m mkdocs build

docs-serve: protocol-reference
	$(PYTHON) -m mkdocs serve --dev-addr 127.0.0.1:8001

protocol-reference:
	$(PYTHON) server/tools/generate_protocol_reference.py

client-test:
	npm --prefix client install
	npm --prefix client run build
	npm --prefix client run test --workspaces

# The gesture suite, which needs a real browser. It skips itself with a message when there
# is no Chromium on the machine rather than failing a run that never asked for one; set
# FENIXSPOON_CHROMIUM to point it at a specific binary.
client-browser-test:
	npm --prefix client run build
	npm --prefix client run test:browser --workspace @fenix-spoon/viewer

# Starts a server on $(PORT), waits for it, runs the load test, then stops it — including
# on failure, so a red run doesn't leave a process holding the port.
loadtest:
	@rm -rf $(DATA_DIR)
	@FENIXSPOON_DATA_DIR=$(DATA_DIR) $(PYTHON) -m uvicorn fenixspoon.main:app \
		--app-dir server --port $(PORT) --log-level warning & echo $$! > .loadtest.pid; \
	for i in $$(seq 1 30); do \
		curl -sf http://127.0.0.1:$(PORT)/api/v1/solvers > /dev/null && break; sleep 1; \
	done; \
	$(PYTHON) server/loadtest.py --url http://127.0.0.1:$(PORT) \
		--clients $(CLIENTS) --jobs $(JOBS) --pid $$(cat .loadtest.pid); \
	status=$$?; \
	kill $$(cat .loadtest.pid) 2>/dev/null; rm -f .loadtest.pid; \
	exit $$status
