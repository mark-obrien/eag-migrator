.DEFAULT_GOAL := help
SHELL := /bin/bash

# Run a migrator command inside the container:  make cli CMD="discover --side v2"
COMPOSE := docker compose
RUN := $(COMPOSE) run --rm --entrypoint eagm migrator

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: setup
setup: .env ## Create .env from .env.example if it does not exist

.env:
	@cp .env.example .env
	@echo "Created .env from .env.example — edit it with your real connection details."

.PHONY: build
build: setup ## Build the image (includes Chromium for capture/login)
	$(COMPOSE) build

.PHONY: build-slim
build-slim: setup ## Build without Chromium — database-to-database only
	WITH_BROWSER=false $(COMPOSE) build

.PHONY: up
up: setup ## Start the databases named in COMPOSE_PROFILES (see .env)
	$(COMPOSE) up -d

.PHONY: down
down: ## Stop everything (volumes survive)
	$(COMPOSE) down

.PHONY: clean
clean: ## Stop everything and delete the database volumes
	$(COMPOSE) down -v

.PHONY: logs
logs: ## Tail container logs
	$(COMPOSE) logs -f

.PHONY: shell
shell: ## Shell inside the migrator container
	$(COMPOSE) run --rm --entrypoint bash migrator

.PHONY: cli
cli: ## Run any eagm command:  make cli CMD="plan --limit 100"
	$(RUN) $(CMD)

.PHONY: dashboard
dashboard: setup ## Start the web dashboard (EAGM_DASHBOARD_PORT, default 19080)
	$(COMPOSE) up -d dashboard
	@echo "Dashboard: http://127.0.0.1:$${EAGM_DASHBOARD_PORT:-19080}"
	@test -n "$$EAGM_DASHBOARD_TOKEN" && \
		echo "  token required — append ?token=$$EAGM_DASHBOARD_TOKEN" || true

.PHONY: dashboard-logs
dashboard-logs: ## Tail the dashboard logs
	$(COMPOSE) logs -f dashboard

.PHONY: dashboard-stop
dashboard-stop: ## Stop the dashboard
	$(COMPOSE) stop dashboard

.PHONY: doctor
doctor: ## Check both database connections
	$(RUN) doctor

# --- web source: no database, no API ---------------------------------------

.PHONY: recon
recon: ## Inspect the live v2 site:  make recon URL=https://example.com
	@test -n "$(URL)" || (echo "Set URL=https://…" && exit 1)
	$(RUN) recon $(URL)

.PHONY: login
login: ## Store a session:  EAGM_COOKIE='sid=...' make login URL=https://...
	@test -n "$(URL)" || (echo "Set URL=https://…" && exit 1)
	@test -n "$$EAGM_COOKIE$$EAGM_AUTH_TOKEN" || \
		(echo "Set EAGM_COOKIE='<Cookie header>' or EAGM_AUTH_TOKEN=<token>" && exit 1)
	$(COMPOSE) run --rm -e EAGM_COOKIE -e EAGM_AUTH_TOKEN \
		--entrypoint eagm migrator login $(URL)

.PHONY: capture
capture: ## Record the site's network calls (needs the browser in the image)
	@test -n "$(URL)" || (echo "Set URL=https://…" && exit 1)
	$(RUN) capture $(URL)

.PHONY: draft-html
draft-html: ## Draft selectors from a server-rendered list screen:  make draft-html URL=https://…/customers
	@test -n "$(URL)" || (echo "Set URL=https://…" && exit 1)
	$(RUN) draft-html $(URL)

.PHONY: build-browser
build-browser: setup ## Rebuild with Chromium and restart the dashboard
	WITH_BROWSER=true $(COMPOSE) build
	@$(COMPOSE) ps --services --filter status=running | grep -qx dashboard \
		&& $(COMPOSE) up -d --force-recreate dashboard || true

.PHONY: harvest
harvest: ## Pull the site into state/staging.sqlite
	$(RUN) harvest

.PHONY: api-get
api-get: ## Read a v3 API endpoint:  make api-get PATH_=/api/V1/pricing-profiles
	@test -n "$(PATH_)" || (echo "Set PATH_=/api/V1/..." && exit 1)
	$(RUN) api-get $(PATH_)

.PHONY: staging
staging: ## Show what is in the staging database
	$(RUN) staging

.PHONY: discover
discover: ## Introspect both databases into profiles/
	$(RUN) discover --side both

.PHONY: scaffold
scaffold: ## Draft config/mapping.draft.yaml from the profiles
	$(RUN) scaffold

.PHONY: plan
plan: ## Dry run — transform everything, write nothing
	$(RUN) plan

.PHONY: migrate
migrate: ## Run the migration for real
	$(RUN) run --yes

.PHONY: verify
verify: ## Verify the most recent migration
	$(RUN) verify

.PHONY: runs
runs: ## List previous runs
	$(RUN) runs

.PHONY: test
test: ## Run the test suite in the container
	$(COMPOSE) run --rm --entrypoint sh migrator -c "pip install -q pytest && python -m pytest -q"

.PHONY: test-local
test-local: ## Run the test suite on the host
	python -m pytest -q
