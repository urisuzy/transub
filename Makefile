.PHONY: build-base build

build-base:
	git pull
	docker build -t urisuzy/transub-base:latest -f Dockerfile-base .
	docker push urisuzy/transub-base:latest

build:
	git pull
	docker build -t urisuzy/transub:latest .
	docker push urisuzy/transub:latest
