# Короткие команды. Linux/WSL: make <цель>. Переменные окружения — из .env.
PY ?= .venv/bin/python
SHELL := /bin/bash

.PHONY: help venv render check lint test test-docker up up-dev down migrate-up migrate-down \
        seed reset health backup restore deploy experiment analysis validate

help:            ## список целей
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

venv:            ## окружение разработки (Python 3.12)
	python3.12 -m venv .venv && $(PY) -m pip install -e ".[analysis,dev]"

render:          ## пересоздать infrastructure/generated из bench.yaml
	$(PY) -m infrastructure.render

check:           ## генерация актуальна + статические проверки
	$(PY) -m infrastructure.render --check
	$(PY) -m ruff check . && $(PY) -m black --check . && \
	$(PY) -m mypy apps serverless database infrastructure experiments scripts

lint: check

test:            ## все тесты (PostgreSQL: pgserver или ARCH_BENCH_TEST_DSN)
	$(PY) -m pytest

test-docker:     ## тесты в контейнере против postgres:16
	docker compose -f docker-compose.test.yml run --rm tests

up:              ## локально: монолит + PostgreSQL (UI http://localhost:8000)
	docker compose up -d --wait

up-dev:          ## локально, образ из рабочей копии, журнал запросов
	docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build --wait

down:
	docker compose down

migrate-up:      ## alembic upgrade head
	$(PY) -m database.manage migrate up

migrate-down:    ## alembic downgrade base
	$(PY) -m database.manage migrate down

seed:            ## детерминированный seed (DATABASE_URL)
	$(PY) -m database.manage seed

reset:           ## рабочие таблицы := эталон seed
	$(PY) -m database.manage reset

health:
	$(PY) -m database.manage health

backup:          ## резервная копия из работающей конфигурации
	./scripts/backup-db.sh

restore:         ## восстановление: make restore FILE=backups/....dump
	./scripts/restore-db.sh $(FILE)

deploy:          ## сервер: make deploy ARCH=monolith|three-tier|microservices|serverless
	./scripts/deploy.sh $(ARCH)

experiment:      ## сервер: полная серия 4 × 10 прогонов + анализ + проверка для статьи
	./scripts/run_all.sh

analysis:
	$(PY) -m experiments.analysis.run_analysis

validate:
	$(PY) scripts/validate_results.py --for-article --deep
