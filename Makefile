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
