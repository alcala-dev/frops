# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `src/frops/` package layout with `__main__` so `python -m frops` works.
- `pyproject.toml` with `hatchling` build backend, `frops` console script,
  and bundled `ruff` / `mypy` / `pytest` / `coverage` configs.
- `.pre-commit-config.yaml` running ruff, ruff-format, mypy, and basic hygiene
  hooks.
- GitHub Actions CI: lint + type-check, test matrix on Python 3.10–3.12, and
  build job that uploads sdist + wheel as an artifact.
- `README.md`, `LICENSE` (Apache-2.0), `CONTRIBUTING.md`, and `Makefile`.

### Changed
- Moved `frops.py` → `src/frops/cli.py` and `modules/commands.py` →
  `src/frops/commands.py`.

## [0.1.0] - 2026-05-29

### Added
- Initial `frops` CLI with `view` and `analyze` subcommands.
