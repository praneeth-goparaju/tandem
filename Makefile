.PHONY: help install demo test typecheck audit scan clean hooks-install hooks-pii

help:
	@echo "Targets:"
	@echo "  install        — create .venv and install Python deps (includes the Claude Agent SDK)"
	@echo "  demo           — dry-run the tracked PhD-student example overlay"
	@echo "  test           — run the pytest suite"
	@echo "  typecheck      — run blocking mypy static type checks"
	@echo "  audit          — scan Python dependencies for CVEs"
	@echo "  scan           : scan every tracked file for personal values, including your private terms"
	@echo "  clean          — remove pycache + build artifacts"
	@echo "  hooks-install  — install pre-commit hooks (gitleaks + PII guard)"
	@echo "  hooks-pii      — regenerate tools/pii_blocklist.txt from user/profile.yaml"

install:
	python3.12 -m venv --clear .venv
	./.venv/bin/python -m pip install --upgrade pip
	./.venv/bin/pip install -e '.[dev]'

demo:
	USER_DIR=examples/phd_student ./.venv/bin/python -m src.cli --run scout --dry-run

test:
	./.venv/bin/python -m pytest tests/ -q

typecheck:
	./.venv/bin/python -m mypy

audit:
	./.venv/bin/python -m pip_audit .
	./.venv/bin/python -m pip_audit --skip-editable

scan:
	./.venv/bin/python3 tools/check_pii.py --tree --private

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name '*.pyc' -delete
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage

hooks-install:
	./.venv/bin/pre-commit install
	$(MAKE) hooks-pii
	@echo "Pre-commit hooks installed. The first commit will trigger hook downloads."

hooks-pii:
	./.venv/bin/python3 tools/gen_pii_blocklist.py
