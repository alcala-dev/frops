.PHONY: help install lint format typecheck test check build clean

help:
	@echo "Targets:"
	@echo "  install    Editable install with dev extras + pre-commit hooks"
	@echo "  lint       Ruff lint + format check"
	@echo "  format     Ruff format (writes changes)"
	@echo "  typecheck  Mypy"
	@echo "  test       Pytest with coverage"
	@echo "  check      lint + typecheck + test"
	@echo "  build      Build sdist + wheel into dist/"
	@echo "  clean      Remove build artifacts and caches"

install:
	python -m pip install --upgrade pip
	python -m pip install -e ".[dev]"
	pre-commit install

lint:
	ruff check .
	ruff format --check .

format:
	ruff format .
	ruff check --fix .

typecheck:
	mypy

test:
	pytest

check: lint typecheck test

build:
	python -m pip install --upgrade build
	python -m build

clean:
	rm -rf build/ dist/ *.egg-info src/*.egg-info
	rm -rf .ruff_cache .mypy_cache .pytest_cache htmlcov .coverage
	find . -type d -name __pycache__ -exec rm -rf {} +
