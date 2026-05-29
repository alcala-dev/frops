# frops

[![CI](https://github.com/alcala-dev/frops/actions/workflows/ci.yml/badge.svg)](https://github.com/alcala-dev/frops/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

**FROps** (Field Reliability Ops) is a small command-line helper for triaging
CoreWeave bare-metal node (BMN) workflow failures. It wraps `kubectl`, `yq`,
and `awxstat` calls into ergonomic subcommands so you can list and inspect
nodes in various FLCC failure states without remembering long label selectors.

## Features

- `frops view <fail_type>` — list BMNs in a given failure state (zap, test,
  field-diag, etc.), optionally filtered by ownership label.
- `frops analyze bmn <name>` — run an ordered set of inspection commands
  against a specific BMN and print labeled output sections.

## Requirements

- Python **3.10+**
- `kubectl` configured against the relevant cluster
- `yq` ([mikefarah/yq](https://github.com/mikefarah/yq))
- `awxstat` (internal CoreWeave tool, available on jump hosts)

## Installation

### From a clone

```bash
git clone https://github.com/alcala-dev/frops.git
cd frops
pip install .
```

This installs the `frops` console script onto your `PATH`.

### Editable install (for development)

```bash
pip install -e ".[dev]"
pre-commit install
```

## Usage

### View failures

```bash
# All failed BMNs
frops view fails

# Only zap failures (node-zap or dpu-zap)
frops view zapfails

# Filter by ownership label
frops view testfails -u jdoe
```

Available fail types: `fails`, `zapfails`, `nodezapfails`, `dpuzapfails`,
`testfails`, `fielddiagfails`.

### Analyze a specific BMN

```bash
frops analyze bmn ss929610x4724071
```

Output is grouped into labeled sections: *Overview*, *Messages*, *AWX Mgmt*,
and *AWX BMC*.

### Module invocation

```bash
python -m frops view fails
```

## Development

Common tasks are wired into a `Makefile`:

```bash
make install     # editable install with dev extras
make lint        # ruff lint + format check
make format      # ruff format
make typecheck   # mypy
make test        # pytest with coverage
make check       # lint + typecheck + test
make build       # build sdist + wheel
```

Or run the tools directly:

```bash
ruff check .
ruff format .
mypy
pytest
```

### Project layout

```
frops/
├── src/frops/        # package source
│   ├── cli.py        # argparse setup + subcommand handlers
│   └── commands.py   # subprocess helpers (run, capture)
├── tests/            # pytest suite
├── pyproject.toml    # build, deps, and tool configs
└── .github/workflows/ci.yml
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Changelog: [CHANGELOG.md](CHANGELOG.md).

## License

[Apache-2.0](LICENSE)
