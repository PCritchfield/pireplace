PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

.PHONY: test
test:
	$(PYTHON) -m pytest -q

.PHONY: dev
dev: ## Run the fireplace on this laptop (needs mpv + ffmpeg)
	./scripts/dev.sh

.PHONY: sample-media
sample-media: ## Generate sample video and music under .dev/media
	./scripts/make_sample_media.sh
