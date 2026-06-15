.PHONY: build-base build push

default:
	@echo "Error: Please specify a target (e.g., 'make build-base' or 'make build')"
	@exit 1

# Commit & push perubahan lokal. Pesan commit: make push MSG="pesan".
MSG ?= update
push:
	git add -A
	git commit -m "$(MSG)"
	git push

build-base:
	git pull
	docker build -t urisuzy/transub-base:vllm -f Dockerfile-base .
	docker push urisuzy/transub-base:vllm

build:
	git pull
	docker build -t urisuzy/transub:vllm .
	docker push urisuzy/transub:vllm

build-release:
	git pull
	@if [ -z "$(word 1,$(MAKECMDGOALS))" ]; then \
		echo "Error: Version parameter is required. Usage: make build-release 1.0.0"; \
		exit 1; \
	else \
		echo "Building with tag: $(word 2,$(MAKECMDGOALS))"; \
		docker build -t urisuzy/transub:vllm-$(word 2,$(MAKECMDGOALS)) .; \
		docker push urisuzy/transub:vllm-$(word 2,$(MAKECMDGOALS)); \
	fi

%:
	@:
