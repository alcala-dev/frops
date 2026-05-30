# Contributing to frops

Thanks for taking the time to contribute. This document captures the workflow
for landing changes.

## Development setup

```bash
git clone https://github.com/alcala-dev/frops.git
cd frops
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

Verify your setup:

```bash
make check
```

## Branching & commits

- Branch off `main`. Name branches descriptively (`feature/...`, `fix/...`,
  `docs/...`, `refactor/...`).
- Follow [Conventional Commits](https://www.conventionalcommits.org/):
  - `feat:` — user-visible feature
  - `fix:` — bug fix
  - `docs:` — documentation only
  - `refactor:` — code change that neither fixes a bug nor adds a feature
  - `test:` — adding or updating tests
  - `chore:` / `build:` / `ci:` — tooling, build, or CI changes
- Keep commits focused. Squash trivial fixups before opening a PR.

## Style

- Code is formatted by `ruff format` (line length 100).
- Lint, type-check, and test must all pass locally before pushing:
  ```bash
  make check
  ```
- Tests live under `tests/` and follow `test_*.py` naming.
- Public functions should have type hints. `mypy --strict` runs in CI.

## Adding a fail type

To add a new entry to `frops view`:

1. Add the new fail type to `FAIL_TYPES` and `FAIL_COMMANDS` in
   [`src/frops/catalog.py`](src/frops/catalog.py).
2. Add a unit test in [`tests/`](tests/) that asserts the command is
   constructed correctly (including the `-u <user>` filter path).
3. Update the README's fail-type list.
4. Add a CHANGELOG entry under `[Unreleased]`.

## Adjusting the SKU view

`frops view sku <SKU>` is data-driven by two constants in
[`src/frops/catalog.py`](src/frops/catalog.py):

- `SKU_EXCLUDED_STATES` — tuple of FLCC states to exclude from the
  result set. Edit this to expand or shrink the "needs attention" view.
- `SKU_VIEW_TEMPLATE` — the kubectl command template; touch this only if
  the label key changes or you need a different selector shape.

After changing either, update `tests/test_catalog.py` accordingly and add
a CHANGELOG entry.

## Adding an analyze target

To add a new target to `frops analyze`:

1. Add the new target to `ANALYZE_COMMANDS` in
   [`src/frops/catalog.py`](src/frops/catalog.py) with an ordered list of
   `(label, command_template)` tuples. Use `{name}` as the resource-name
   placeholder.
2. Add a unit test that asserts the templates render correctly for a sample
   name.
3. Update the README and CHANGELOG.

## Releasing

1. Bump `__version__` in `src/frops/__init__.py`.
2. Move entries under `[Unreleased]` in `CHANGELOG.md` to a new
   `[X.Y.Z] - YYYY-MM-DD` section.
3. Open a release PR. Once merged, tag the commit:
   ```bash
   git tag -a vX.Y.Z -m "Release vX.Y.Z"
   git push origin vX.Y.Z
   ```

## Reporting issues

Open an issue at <https://github.com/alcala-dev/frops/issues> and include:

- frops version (`frops --version` once that's wired up, or the commit SHA)
- Python version
- Steps to reproduce
- Expected vs actual output
