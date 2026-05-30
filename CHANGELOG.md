# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `frops view sku <SKU>` subcommand for listing BMNs of a given SKU that
  are not in `production`/`ready`/`rma`/`broken`/`dev`/`debug` state.
  Supports the same `-u <user>` ownership filter as the fail-type views.
- `SKU_VIEW_TEMPLATE` and `SKU_EXCLUDED_STATES` in `frops.catalog` so the
  SKU query is data-driven.
- `src/frops/` package layout with `__main__` so `python -m frops` works.
- `pyproject.toml` with `hatchling` build backend, `frops` console script,
  and bundled `ruff` / `mypy` / `pytest` / `coverage` configs.
- `.pre-commit-config.yaml` running ruff, ruff-format, mypy, and basic hygiene
  hooks.
- GitHub Actions CI: lint + type-check, test matrix on Python 3.10–3.12, and
  build job that uploads sdist + wheel as an artifact.
- `README.md`, `LICENSE` (Apache-2.0), `CONTRIBUTING.md`, and `Makefile`.

### Changed
- `frops analyze` now streams each step's output directly instead of
  capturing it, preserving terminal colors from `kubectl` / `kubecolor` /
  `yq`. The `(no output)` placeholder is removed for live runs (the
  dry-run preview still shows section blocks).
- Moved `frops.py` → `src/frops/cli.py` and `modules/commands.py` →
  `src/frops/commands.py`.

## [0.1.0] - 2026-05-29

### Added
- Initial `frops` CLI with `view` and `analyze` subcommands.
