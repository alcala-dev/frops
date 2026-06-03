# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `frops view sku <SKU> --action` access check: BMNs that classify as
  NOOP with zero detected CW codes (i.e. no AWX failure to act on) are
  now probed via `jumpipmitool -c "chassis power status" <BMN>` and
  enriched with `bmns -o wide` (for the canonical WORKFLOW / STATE / TS
  display). A new `=== Access check (NOOP nodes without CW codes) ===`
  table renders after the plan, listing each node with its reachability
  verdict. Runs regardless of `--yes` (read-only diagnostic) and skips
  cleanly when there are no NOOP-clean candidates. Failures do not
  affect the process exit code. Checks run in a thread pool (8 workers)
  so a large NOOP list completes quickly.
- New module `frops.access` (`AccessReport`, `check_access`,
  `check_all`, `access_check_targets`, `render_access_summary`) — pure,
  dependency-injected on a `capture` callable so tests don't spawn
  subprocesses.
- `BMNTarget.workflow` and `BMNTarget.state` (sourced from
  `flcc.coreweave.com/workflow` and `…/state` labels) populated by
  `_build_targets` so the access pass has the data without a second
  kubectl round-trip.
- `frops view sku <SKU> --action` HO-ticket resolution: when a `CW0201`
  BMN has a matching JIRA HO ticket in `Awaiting Support`, the planner
  appends a status block to the ticket's Description instead of running
  the `cwctl return-to-triage` fallback. Match is by BMN name, CW-NODE
  (gmac), or hardware serial — whichever appears in the ticket summary.
  Falls back to the existing cwctl command when nothing matches.
  Soft-fails gracefully (note printed, fallback path taken) if JIRA
  creds are absent or the search itself errors.
- `frops.jira` module — minimal Atlassian Cloud client (stdlib `urllib`,
  no new runtime deps): `JIRAClient.search(jql)`,
  `JIRAClient.append_to_description(key, block)`, `build_search_jql`
  helper, plus an `_adf_to_text` / `_wrap_as_adf` pair for ADF round-trip.
  Auth via `JIRA_EMAIL` + `JIRA_TOKEN` env vars (token at
  https://id.atlassian.com/manage-profile/security/api-tokens).
- `BMNTarget.serial` (sourced from
  `metadata.labels."ds.coreweave.com/status.asset.serial"`) plus a
  `search_identifiers` property that dedups bmn/cw-node/serial for JQL.
- `PlannedAction.jira_issue` field set by the resolver; when populated,
  `execute_plan` dispatches the action to a `jira_runner` instead of the
  shell runner. `actionable_actions` now includes JIRA-resolved actions.
- `frops view sku <SKU> --action [--yes/-y]` (Phase B execution): after
  rendering the plan, prompts `Run these? [y/N]` and — on yes, or with
  `--yes` skipping the prompt — executes each actionable `cwctl` command
  via `run_command` (streaming output, preserving colors). HO-ticket
  actions still execute the return-to-triage fallback; JIRA search /
  update lands in a later phase. An `=== Execution summary ===` block
  reports succeeded/failed counts and lists failed BMNs with their exit
  codes; the worst per-action rc propagates as the process exit code.
  Partial failure does not abort the run — remaining BMNs are still
  attempted. `--yes` without `--action` exits 2 with a clear error.
- `frops.action.execute_plan(actions, runner)` plus `ExecutionResult`,
  `ExecutionSummary`, `actionable_actions`, and `render_execution_summary`
  helpers. Execution accepts a `runner` callable so tests can drive it
  without spawning subprocesses.
- `frops view sku <SKU> --action` (Phase A, read-only): after the
  existing display, runs `awxstat -l mgmt|bmc` per BMN with a non-empty
  `CW-NODE`, parses CW error codes, and prints a per-node remediation
  plan. Power-drain is planned for `CW0211`/`CW0102` on `GPU-GH200-01`;
  HO ticket / return-to-triage is planned for `CW0201`.
- New modules: `frops.awx` (parser for `awxstat` text output, returns
  `AWXReport` + `CWError`) and `frops.action` (classifier, `BMNTarget`,
  `PlannedAction`, plan renderer). Both are pure-functional with full
  unit coverage so the policy and parsing can be exercised without
  hitting kubectl/awxstat.
- `SKU_VIEW_TEMPLATE_JSON` alongside the existing wide-format template,
  used by `--action` to fetch structured BMN data without affecting the
  colored human view.
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
