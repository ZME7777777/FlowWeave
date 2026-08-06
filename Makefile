.PHONY: install dev check web-check api-check migration-check compose-check platform-image-check sandbox-images sandbox-smoke web-dev api-dev worker-dev e2e infra-up infra-up-openhands infra-down

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
	docker compose -f infra/compose.yaml config --quiet

platform-image-check:
	docker build -f services/platform/Dockerfile -t flowweave-platform-check .
	docker run --rm --entrypoint sh flowweave-platform-check -c 'python -c "import quickjs" && docker --version && ! command -v gcc >/dev/null 2>&1'

sandbox-images:
	docker build -f infra/sandbox/python/Dockerfile -t flowweave-sandbox-python:1 .
	docker build -f infra/sandbox/javascript/Dockerfile -t flowweave-sandbox-javascript:1 .

sandbox-smoke: sandbox-images
	docker compose -f infra/compose.yaml exec -T worker python - < services/platform/scripts/sandbox_smoke_check.py

check: api-check web-check compose-check

e2e:
	pnpm --filter @flowweave/web e2e

infra-up: sandbox-images
	docker compose -f infra/compose.yaml up -d --build postgres migration api worker web

infra-up-openhands: sandbox-images
	docker compose -f infra/compose.yaml up -d --build postgres migration openhands-agent-server api worker web

infra-down:
	docker compose -f infra/compose.yaml down
