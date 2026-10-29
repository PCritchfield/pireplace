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

# deploy: HOST=pireplace.local builds a one-host inventory; INVENTORY=path uses a file.
# The required hotspot_password comes from the inventory or ANSIBLE_ARGS='-e @secrets.yml'.
INVENTORY ?=
HOST ?=
ANSIBLE_ARGS ?=
BIN := $(if $(wildcard .venv/bin/ansible-playbook),.venv/bin/,)
UNITS_SYSTEM := deploy/systemd/pireplace-control.service deploy/systemd/pireplace-wifi.service deploy/systemd/pireplace-sync.service deploy/systemd/pireplace-sync.timer
UNITS_USER := deploy/systemd/pireplace-video.service deploy/systemd/pireplace-music.service

.PHONY: lint
lint: ## Playbook syntax check, ansible-lint, systemd-analyze verify (reports what it skips)
	$(PYTHON) scripts/yaml_check.py deploy/ansible/site.yml
	@if command -v $(BIN)ansible-playbook >/dev/null 2>&1; then \
		$(BIN)ansible-playbook --syntax-check -i localhost, deploy/ansible/site.yml; \
	else echo "SKIPPED: ansible-playbook not installed (--syntax-check not run)"; fi
	@if command -v $(BIN)ansible-lint >/dev/null 2>&1; then \
		$(BIN)ansible-lint deploy/ansible/site.yml; \
	else echo "SKIPPED: ansible-lint not installed (playbook lint not run)"; fi
	@if command -v systemd-analyze >/dev/null 2>&1; then \
		out=$$( { systemd-analyze verify $(UNITS_SYSTEM); systemd-analyze --user verify $(UNITS_USER); } 2>&1 ); \
		real=$$(printf '%s\n' "$$out" | grep -v 'is not executable: No such file or directory' || true); \
		[ -z "$$out" ] || printf '%s\n' "$$out"; \
		if [ -n "$$real" ]; then echo "systemd-analyze: errors above"; exit 1; fi; \
		echo "systemd-analyze: units OK (missing-binary warnings ignored: mpv is installed on the Pi, not here)"; \
	else echo "SKIPPED: systemd-analyze not installed (unit verify not run)"; fi

.PHONY: card
card: ## Printable QR card: HOTSPOT_PASSWORD=... make card (writes build/card.html)
	./scripts/make_card.sh

.PHONY: deploy
deploy: ## Provision the Pi: make deploy HOST=pireplace.local (or INVENTORY=path)
	@test -n "$(INVENTORY)$(HOST)" || { echo "usage: make deploy HOST=pireplace.local  (or INVENTORY=path)"; exit 2; }
	$(BIN)ansible-playbook -i $(if $(INVENTORY),$(INVENTORY),$(HOST),) $(ANSIBLE_ARGS) deploy/ansible/site.yml
