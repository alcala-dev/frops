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
- `frops view sku <SKU>` — list BMNs of a given SKU that need attention
  (i.e. *not* in `production`/`ready`/`rma`/`broken`/`dev`/`debug` state).
- `frops analyze bmn <name>` — run an ordered set of inspection commands
  against a specific BMN and print labeled output sections.

Terminal colors emitted by `kubectl` / `kubecolor` / `yq` are preserved
end-to-end — `frops` streams command output rather than capturing it.

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

### View nodes by SKU

```bash
# All GH200 nodes that aren't in production/ready/rma/broken/dev/debug
frops view sku GPU-GH200-01

# Same query, scoped to one owner
frops view sku GPU-GH200-01 -u jdoe
```

The excluded states are `production`, `ready`, `rma`, `broken`, `dev`,
`debug`. To adjust the list, edit `SKU_EXCLUDED_STATES` in
[`src/frops/catalog.py`](src/frops/catalog.py).

### Plan remediation actions for a SKU

`frops view sku <SKU> --action` shows the existing colored table, then
inspects each returned BMN's AWX jobs and prints a per-node remediation
plan. By default it stops there and asks you to pick which group(s) to
run; passing `--yes` (or `-y`) skips the prompt and runs every available
group. Use `--dry-run` if you want the plan without any prompt or
execution.

```bash
# Plan only, then prompt for which groups to run
frops view sku GPU-GH200-01 --action

# Plan and run everything available without prompting (CI / scripted use)
frops view sku GPU-GH200-01 --action --yes

# Plan only, never prompt or execute
frops --dry-run view sku GPU-GH200-01 --action
```

The prompt looks like:

```text
Run?  [a]ll  /  [p]ower-drain (3)  /  [h]o-ticket (1)  /  [n]oop-access (6)  /  Enter to abort:
```

Only groups present in the plan are offered. Pick one letter, or several
comma-separated (`p,h` runs power-drain + ho-ticket but skips the NOOP
access check). Empty input — or Ctrl-C — aborts; nothing runs.

What `--action` does, per BMN with a non-empty `CW-NODE`:

1. Captures `awxstat -l mgmt <BMN>` and `awxstat -l bmc <BMN>`.
2. Parses each output's `cw_error_codes={…}` block for `CWXXXX` codes.
3. Classifies the BMN:
   - **`CW0211` or `CW0102` on SKU `GPU-GH200-01`** → power-drain via
     `cwctl flcc node --one-off -w orphan -s power-drain …`
   - **`CW0201`** → search the `HO` JIRA project for an open ticket in
     `Awaiting Support` whose summary mentions the BMN name, CW-NODE
     (gmac), or hardware serial. If found, the action becomes "append a
     status block to that ticket's Description". If nothing matches (or
     JIRA creds aren't set), falls back to
     `cwctl flcc node -w return-to-triage …`.
   - Other / no codes → no-op (eligible for the access check).
4. Prints a grouped plan with the exact commands that would run and a
   totals line.
5. Prompts you to pick which groups to execute (or with `--yes`, runs
   every group).
6. For each selected actionable group (`p` and/or `h`): runs each
   `cwctl` command in order, streaming its output. A failure on one BMN
   does **not** abort the rest — the run continues and the worst exit
   code propagates as the process exit. An `=== Execution summary ===`
   block at the end lists any failures with their `cwctl` exit codes.
7. If `n` is selected (or under `--yes` when NOOP-clean BMNs exist):
   runs a diagnostic access pass — `jumpipmitool -c "chassis power
   status"` per node, plus `bmns -o wide` for the canonical workflow /
   state / TS display — and prints an `=== Access check ===` table.
   Reachability failures are reported but don't affect the process exit
   code. Runs in parallel (8-thread pool).

BMNs without a `CW-NODE` are listed as skipped — there's nothing to
action on a node that hasn't joined a cluster.

#### JIRA HO-ticket auth

The HO-ticket resolver authenticates to JIRA Cloud via HTTP Basic with
your Atlassian account email and an API token:

```bash
export JIRA_EMAIL=you@coreweave.com
export JIRA_TOKEN=<your-api-token>
```

Generate a token at
<https://id.atlassian.com/manage-profile/security/api-tokens>. If either
var is unset, `--action` still runs — it just skips the HO lookup and
falls back to `cwctl return-to-triage` for `CW0201` BMNs (a one-line
note explains why on stderr).

The policy lives in [`src/frops/action.py`](src/frops/action.py) and the
AWX parser in [`src/frops/awx.py`](src/frops/awx.py); see those modules
to add new CW-code mappings or extend the eligible SKU list.

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
