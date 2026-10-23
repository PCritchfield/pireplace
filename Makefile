PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

.PHONY: test
test:
	$(PYTHON) -m pytest -q
