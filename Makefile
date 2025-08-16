# Simple automation for doc2pdf
# Default target shows help
.DEFAULT_GOAL := help
SHELL := /bin/bash

# Tools & env
PYTHON ?= python
PIP ?= pip
VENVDIR ?= .venv
ACTIVATE = source $(VENVDIR)/bin/activate

# Presets for the ADK docs example
URL_ADK ?= https://google.github.io/adk-docs/
MAXPAGES ?= 500
CONCURRENCY ?= 3
TIMEOUT ?= 90

# Generic variables for custom jobs
START ?=
SITEMAP ?=
INCLUDE ?=
EXCLUDE ?=
OUT ?= out.pdf

.PHONY: help venv install install-deps adk-full adk-guides adk-python-api crawl sitemap clean

help:
	@echo "Targets:"
	@echo "  venv               - create virtual environment (.venv)"
	@echo "  install            - pip install -e . and install Playwright Chromium"
	@echo "  install-deps       - install system deps for Playwright (Linux best-effort)"
	@echo "  adk-full           - crawl ADK docs (exclude Java API & query dupes) -> adk-no-java.pdf"
	@echo "  adk-guides         - guides only (no API reference) -> adk-guides.pdf"
	@echo "  adk-python-api     - Python API reference only -> adk-python-api.pdf"
	@echo "  crawl              - crawl from START=... with INCLUDE/EXCLUDE/OUT vars"
	@echo "  sitemap            - enumerate from SITEMAP=... with INCLUDE/EXCLUDE/OUT vars"
	@echo "  clean              - remove _build/ and PDFs"

venv:
	$(PYTHON) -m venv $(VENVDIR)
	@echo "Activate with: source $(VENVDIR)/bin/activate  (Windows: $(VENVDIR)\\Scripts\\activate)"

install: venv
	$(ACTIVATE); $(PIP) install -e .
	$(ACTIVATE); $(PYTHON) -m playwright install chromium

install-deps:
	$(PYTHON) -m playwright install-deps chromium || true

adk-full:
	$(ACTIVATE); doc2pdf \
	  --start $(URL_ADK) \
	  --max-pages $(MAXPAGES) \
	  --exclude "/api-reference/java/,?" \
	  --concurrency $(CONCURRENCY) \
	  --timeout $(TIMEOUT) \
	  --out adk-no-java.pdf

adk-guides:
	$(ACTIVATE); doc2pdf \
	  --start $(URL_ADK) \
	  --max-pages 400 \
	  --include "/get-started/,/agents/,/tools/,/tutorials/,/streaming/" \
	  --exclude "/api-reference/,?" \
	  --concurrency $(CONCURRENCY) \
	  --timeout $(TIMEOUT) \
	  --out adk-guides.pdf

adk-python-api:
	$(ACTIVATE); doc2pdf \
	  --start $(URL_ADK)/api-reference/python/ \
	  --max-pages 400 \
	  --include "/api-reference/python/" \
	  --exclude "?,/api-reference/java/" \
	  --concurrency $(CONCURRENCY) \
	  --timeout $(TIMEOUT) \
	  --out adk-python-api.pdf

crawl:
	@if [ -z "$(START)" ]; then echo "START is required (e.g., make crawl START=https://site/docs OUT=site.pdf)"; exit 2; fi
	$(ACTIVATE); doc2pdf \
	  --start "$(START)" \
	  --max-pages $(MAXPAGES) \
	  $(if $(INCLUDE),--include "$(INCLUDE)",) \
	  $(if $(EXCLUDE),--exclude "$(EXCLUDE)",) \
	  --concurrency $(CONCURRENCY) \
	  --timeout $(TIMEOUT) \
	  --out "$(OUT)"

sitemap:
	@if [ -z "$(SITEMAP)" ]; then echo "SITEMAP is required (e.g., make sitemap SITEMAP=https://site/sitemap.xml OUT=site.pdf)"; exit 2; fi
	$(ACTIVATE); doc2pdf \
	  --sitemap "$(SITEMAP)" \
	  $(if $(INCLUDE),--include "$(INCLUDE)",) \
	  $(if $(EXCLUDE),--exclude "$(EXCLUDE)",) \
	  --concurrency $(CONCURRENCY) \
	  --timeout $(TIMEOUT) \
	  --out "$(OUT)"

clean:
	rm -rf _build *.pdf