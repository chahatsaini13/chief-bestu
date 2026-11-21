.PHONY: up down logs test env

env:
	@test -f .env || (cp .env.example .env && echo ">> Created .env from .env.example. Put your real LLM_API_KEYS in it, then run 'make up' again." && exit 1)

up: env
	docker compose up --build -d
	@echo "Backend:  http://localhost:8000/health"
	@echo "Frontend: http://localhost:3000"

down:
	docker compose down

logs:
	docker compose logs -f

test:
	cd backend && pip install -r requirements-dev.txt && python -m pytest -q
