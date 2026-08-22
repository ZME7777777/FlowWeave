.PHONY: install dev check web-check api-check migration-check compose-check platform-image-check sandbox-images dependency-builder-image openhands-image openhands-image-provenance openhands-contract-check openhands-smoke sandbox-smoke web-dev api-dev worker-dev e2e infra-up rebuild-deploy infra-down

COMPOSE = docker compose --env-file .env -f infra/compose.yaml

install:
	pnpm install --frozen-lockfile
	cd services/platform && uv sync --frozen

dev:
	@echo "Run in separate terminals: make api-dev, make worker-dev and make web-dev"

web-dev:
	pnpm --filter @flowweave/web dev

api-dev:
	cd services/platform && uv run uvicorn flowweave.bootstrap.api:create_app --reload --port 8080 --factory

worker-dev:
	cd services/platform && uv run python -m flowweave.bootstrap.worker

web-check:
	pnpm --filter @flowweave/web lint
	pnpm --filter @flowweave/web typecheck
	pnpm --filter @flowweave/web build

migration-check:
	cd services/platform && uv run python scripts/migration_check.py

api-check:
	cd services/platform && uv run ruff format --check . && uv run ruff check .
	cd services/platform && PYRIGHT_PYTHON_FORCE_VERSION=1.1.405 uv run pyright
	cd services/platform && uv run pytest

compose-check:
	SANDBOX_RUNTIME_NETWORK_MODE=egress DOCKER_CONTROLLER_API_KEY=flowweave-compose-api-key-0000000000000 DOCKER_CONTROLLER_WORKER_API_KEY=flowweave-compose-worker-key-000000000 $(COMPOSE) config --format json | python3 services/platform/scripts/compose_security_check.py

platform-image-check:
	docker build -f services/platform/Dockerfile -t flowweave-platform-check .
	docker run --rm --entrypoint sh flowweave-platform-check -c 'python -c "import quickjs" && docker --version && ! command -v gcc >/dev/null 2>&1'

sandbox-images:
	docker build -f infra/sandbox/python/Dockerfile -t flowweave-sandbox-python:1 .
	docker build -f infra/sandbox/javascript/Dockerfile -t flowweave-sandbox-javascript:1 .

dependency-builder-image:
	docker build -f infra/dependency-builder/Dockerfile -t flowweave-dependency-builder:1 .

openhands-image:
	docker build -f infra/openhands/Dockerfile -t flowweave-openhands-runtime:1 .

openhands-image-provenance: openhands-image
	docker image inspect --format='{{.Id}}' flowweave-openhands-runtime:1
	docker run --rm --entrypoint /bin/sh flowweave-openhands-runtime:1 -c 'cat /runtime/openhands-source-provenance.json'

openhands-contract-check: openhands-image-provenance
	docker run --rm --entrypoint /runtime/.venv/bin/python flowweave-openhands-runtime:1 /runtime/contract_check.py

openhands-smoke: openhands-image
	python3 services/platform/scripts/openhands_smoke_check.py

sandbox-smoke: sandbox-images
	$(COMPOSE) exec -T worker python - < services/platform/scripts/sandbox_smoke_check.py

check: api-check web-check compose-check

e2e:
	pnpm --filter @flowweave/web e2e

infra-up: sandbox-images dependency-builder-image openhands-image
	$(COMPOSE) up -d --build --force-recreate postgres migration runtime-provider api worker web

# Rebuild every local image without cache, then recreate the complete stack.
# Persistent database, artifact and workspace data are preserved.
rebuild-deploy:
	docker build --no-cache -f infra/sandbox/python/Dockerfile -t flowweave-sandbox-python:1 .
	docker build --no-cache -f infra/sandbox/javascript/Dockerfile -t flowweave-sandbox-javascript:1 .
	docker build --no-cache -f infra/dependency-builder/Dockerfile -t flowweave-dependency-builder:1 .
	docker build --no-cache -f infra/openhands/Dockerfile -t flowweave-openhands-runtime:1 .
	$(COMPOSE) build --no-cache migration runtime-provider api worker web
	$(COMPOSE) up -d --force-recreate --remove-orphans postgres workspace-init migration runtime-provider api worker web

infra-down:
	$(COMPOSE) down
