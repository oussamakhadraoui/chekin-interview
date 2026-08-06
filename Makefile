.PHONY: up down logs test lint fmt migrate

up:            ## start postgres + api on :8000
	docker compose up --build

down:
	docker compose down -v

logs:
	docker compose logs -f api

db:            ## start only postgres (what the tests need)
	docker compose up -d db

test: db       ## run the test suite against a real postgres
	pytest -v

lint:
	ruff check app tests

fmt:
	ruff format app tests && ruff check --fix app tests

migrate:       ## autogenerate a migration: make migrate m="add foo"
	alembic revision --autogenerate -m "$(m)"
