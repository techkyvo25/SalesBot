# Works with GNU Make on Linux/macOS and on Windows from Git Bash (make via `winget install ezwinports.make`).
# Recipes are plain POSIX sh: no bash-isms, no PowerShell.

ADMIN := apps/admin

.PHONY: help setup dev-api dev-admin require-docker db-up db-down db-reset migrate test lint format load-clinic simulate-intake console demo demo-seed demo-reset voice-agent voice-ops voice-tunnel voice-tunnel-stop voice-api-restart voice-tunnel-check voice-tools-log voice-test-patient voice-test-patient-remove

help:
	@echo "setup      install Python (uv) + admin (npm) deps and git hooks"
	@echo "dev-api    run FastAPI on http://localhost:8000 (reload)"
	@echo "dev-admin  run the admin app on http://localhost:5173"
	@echo "db-up      [requires Docker + Supabase CLI] start local Supabase"
	@echo "db-down    [requires Docker + Supabase CLI] stop local Supabase"
	@echo "db-reset   [requires Docker + Supabase CLI] recreate local DB from migrations + seed.sql"
	@echo "migrate    push migrations to the LINKED remote project (ask before linking)"
	@echo "test       Python tests (DB tests skip if Supabase is down) + admin build"
	@echo "lint       ruff + eslint + tsc"
	@echo "format     ruff format + fix"
	@echo "load-clinic FILE=x.yaml        load a clinic setup into SUPABASE_DB_URL (dev)"
	@echo "simulate-intake ARGS='...'     send a signed fake registration (dev, see INTAKE_CONTRACT)"
	@echo "console ARGS='--lang mr'       chat with the appointment flow in the terminal (no DB)"
	@echo "console ARGS='--db'            the same chat against SUPABASE_DB_URL's demo clinic"
	@echo "demo                           the local demo page on http://127.0.0.1:8000/demo (docs/DEMO.md)"
	@echo "demo-seed / demo-reset         seed / rebuild the FAKE demo clinic only"
	@echo "voice-agent PACK=clinic        print the Sarvam agent setup rendered from a pack"
	@echo "voice-ops ARGS='spend'         spend caps, test-run guard, outbound dry run, cost actuals"
	@echo "voice-tunnel / voice-tunnel-stop  API + Cloudflare quick tunnel for the first voice test"
	@echo "voice-tunnel-check             /health, tool without token (401), with token (200)"
	@echo "voice-tools-log                last 20 voice requests (endpoint, status, duration)"
	@echo "voice-test-patient[-remove]    fake patient for Sarvam's browser test (demo clinic)"

setup:
	uv sync --all-packages
	npm --prefix $(ADMIN) install
	uv run pre-commit install

dev-api:
	uv run --package salesbot-api uvicorn salesbot_api.main:app --reload --port 8000

dev-admin:
	npm --prefix $(ADMIN) run dev

# Local DB is deferred (docs/DECISIONS.md). These targets need Docker running and the Supabase CLI.
require-docker:
	@command -v supabase >/dev/null 2>&1 || { echo "ERROR: Supabase CLI not found. Install it (see README) and retry."; exit 1; }
	@docker info >/dev/null 2>&1 || { echo "ERROR: Docker is not running. db-up/db-down/db-reset require Docker and the Supabase CLI."; echo "Start Docker Desktop, or use a hosted Supabase dev project instead (docs/DECISIONS.md)."; exit 1; }

db-up: require-docker
	supabase start

db-down: require-docker
	supabase stop

db-reset: require-docker
	supabase db reset

migrate:
	supabase db push

test:
	uv run pytest
	npm --prefix $(ADMIN) run build

lint:
	uv run ruff check .
	uv run ruff format --check .
	npm --prefix $(ADMIN) run lint

format:
	uv run ruff check --fix .
	uv run ruff format .

load-clinic:
	uv run --package salesbot-api python -m salesbot_api.tools.load_clinic $(FILE)

simulate-intake:
	uv run --package salesbot-api python -m salesbot_api.tools.simulate_intake $(ARGS)

console:
	uv run --package salesbot-api python -m salesbot_api.tools.console $(ARGS)

# The demo targets set DEMO_MODE=true for their own process only; .env keeps it off.
demo-seed:
	DEMO_MODE=true uv run --package salesbot-api python -m salesbot_api.demo.seed seed

demo-reset:
	DEMO_MODE=true uv run --package salesbot-api python -m salesbot_api.demo.seed reset

demo: demo-seed
	@echo "Demo page: http://127.0.0.1:8000/demo  (localhost only; Ctrl+C to stop)"
	DEMO_MODE=true uv run --package salesbot-api uvicorn salesbot_api.main:app --host 127.0.0.1 --port 8000

# LITERAL=1: the clinic's name, phone and emergency numbers written in, no {{variables}}
# (CLINIC=path/to/clinic.yaml, default: the demo clinic).
voice-agent:
	uv run python -m salesbot_core.packs.render_agent --pack $(or $(PACK),clinic) 	  $(if $(LITERAL),--literal --clinic-file $(or $(CLINIC),apps/api/src/salesbot_api/demo/clinic.yaml))

voice-ops:
	uv run --package salesbot-api python -m salesbot_api.tools.voice_ops $(ARGS)

# First voice test (docs/SARVAM_AGENT.md, browser test). The tunnel refuses DEMO_MODE=true.
# FIELDS=1 (voice-tunnel, voice-api-restart): also log non-personal request fields, temporarily.
voice-tunnel:
	$(if $(FIELDS),VOICE_CALL_LOG_FIELDS=true) uv run --package salesbot-api python -m salesbot_api.tools.voice_tunnel start

voice-tunnel-stop:
	uv run --package salesbot-api python -m salesbot_api.tools.voice_tunnel stop

voice-api-restart:
	$(if $(FIELDS),VOICE_CALL_LOG_FIELDS=true) uv run --package salesbot-api python -m salesbot_api.tools.voice_tunnel restart-api

voice-tunnel-check:
	uv run --package salesbot-api python -m salesbot_api.tools.voice_tunnel check

voice-tools-log:
	@tail -n 20 .local/voice-tools.log 2>/dev/null || echo "no voice requests logged yet"

voice-test-patient:
	DEMO_MODE=true uv run --package salesbot-api python -m salesbot_api.demo.seed voice-patient

voice-test-patient-remove:
	DEMO_MODE=true uv run --package salesbot-api python -m salesbot_api.demo.seed voice-patient-remove
