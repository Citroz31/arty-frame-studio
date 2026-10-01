PYTHON ?= python

.PHONY: tests rtl check format format-check run
tests:
	$(PYTHON) -m pytest
rtl:
	$(PYTHON) firmware/sim/run_tests.py
check:
	$(PYTHON) -m ruff check src tests firmware/sim
	$(PYTHON) -m mypy src/arty_frame_studio
format:
	$(PYTHON) -m ruff format src tests firmware/sim
format-check:
	$(PYTHON) -m ruff format --check src tests firmware/sim
run:
	$(PYTHON) -m arty_frame_studio.app
