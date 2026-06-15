.PHONY: push build up down logs

default:
	@echo "Targets: make up | down | logs | build | push MSG=\"pesan\""
	@exit 1

# Commit & push perubahan lokal. Pesan commit: make push MSG="pesan".
MSG ?= update
push:
	git add -A
	git commit -m "$(MSG)"
	git push

# Build image (single-stage, tanpa base image).
build:
	docker compose build

# Jalankan service via docker compose (baca config dari .env).
up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f
