.PHONY: build-base build

default:
	@echo "Error: Please specify a target (e.g., 'make build-base' or 'make build')"
	@exit 1

build-base:
	git pull
	docker build -t urisuzy/transub-base:latest -f Dockerfile-base .
	docker push urisuzy/transub-base:latest

build:
	git pull
	docker build -t urisuzy/transub:latest .
	docker push urisuzy/transub:latest

build-release:
	git pull
	@if [ -z "$(word 1,$(MAKECMDGOALS))" ]; then \
		echo "Error: Version parameter is required. Usage: make build-release 1.0.0"; \
		exit 1; \
	else \
		echo "Building with tag: $(word 2,$(MAKECMDGOALS))"; \
		docker build -t urisuzy/transub:$(word 2,$(MAKECMDGOALS)) .; \
		docker push urisuzy/transub:$(word 2,$(MAKECMDGOALS)); \
	fi

%:
	@:
