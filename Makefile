SHELL := /bin/bash

.PHONY: help up down logs test lint typecheck migrate seed dev shell clean

help:
	@echo "Available targets:"
	@echo "  make up        - Start containers (postgres, redis, api)"
	@echo "  make down      - Stop and remove containers"
	@echo "  make logs      - Follow API logs"
	@echo "  make test      - Run tests (pytest -n 8)"
	@echo "  make lint      - Run ruff check + format"
	@echo "  make typecheck - Run mypy"
	@echo "  make migrate   - Run alembic upgrade head"
	@echo "  make seed      - Seed canonical data"
	@echo "  make dev       - Start dev environment with hot reload"
	@echo "  make shell     - Shell into API container"
	@echo "  make clean     - Remove containers, volumes, and images"

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f api

test:
	docker compose exec -T api pytest -n 8 --tb=short -q

lint:
	docker compose exec -T api ruff check . && docker compose exec -T api ruff format --check .

typecheck:
	docker compose exec -T api mypy src/

migrate:
	docker compose exec -T api alembic upgrade head

seed:
	docker compose exec -T api python -m scripts.seed_canonical

dev:
	docker compose up -d postgres redis && \
	docker compose run --rm -p 8000:8000 --service-ports api uvicorn bancaemdia.main:app --reload --host 0.0.0.0 --port 8000

shell:
	docker compose exec api bash

clean:
	docker compose down -v --rmi all