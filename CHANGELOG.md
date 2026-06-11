# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **XID-109 "ready for return-to-ready" table.** `view sku --action` now
  prints two parallel XID-109 summary tables above the planned-actions
  block: the existing "waiting for return-to-fleetops" list (BMNs whose
  flcc/nlcc handoff has not completed) and a new "ready for
  return-to-ready" list (BMNs at PhaseState reason `nlcc`, eligible for
  `cwctl flcc node -w return-to-ready`). The actionable summary prints
  first so the operator sees the actionable pool before scrolling.
  Column layout matches the waiting table (BMN / CW-NODE / CWNC-STATE /
  PHASE-REASON / HO TICKET) with the same yellow-BMN + cyan-HO-TICKET
  highlights. SKU-agnostic — applies to any GPU SKU with XID 109 codes,
  not just H100-01.

### Fixed
- IBP reseat no longer silently skips ibp-flap BMNs whose PhaseState
  reason is `flcc`. The `phase_reason == "nlcc"` gate was borrowed
  from XID-109 (where `cwctl flcc return-to-ready` legitimately
  requires NLCC handoff) but IBP reseat just files a DCT ticket — no
  state-machine precondition exists. Since `IBMultipleFlaps` is an
  FLCC alert, most ibp-flap BMNs sit at `phase_reason="flcc"` and
  were dropped without surfacing. The trigger is now just CWNC-STATE
  = `triage` + an open HO ticket whose description mentions ibp.
  `phase_reason` is still captured on the candidate for diagnostic
  display in the skipped block.

### Fixed
- Quote the JIRA project key in every JQL string the tool emits. `DO`
  collides with a reserved JQL word (legacy boolean OR), so JIRA rejected
  unquoted `project = DO` with `HTTP 400 Bad Request: 'DO' is a reserved
  JQL word`. The DRIVE_INSPECT and IBP_RESEAT dedup queries — and the
  generic HO search builder — now emit `project = "..."` for any project
  key, which is safe for both reserved and non-reserved keys.

### Added
- **IBP reseat DCT ticket workflow** for `view sku --action`. Mirrors the
  XID-109 state-machine trigger (CWNC-STATE = `triage` + PhaseState
  reason = `nlcc`) plus an HO ticket whose description mentions ibp
  issues (`ibp`, `ibp2`-style, or the `IBMultipleFlaps` alert reason).
  Surviving candidates become `ActionKind.IBP_RESEAT` with a rendered
  `cwctl ticket dct-action network <SERIAL> -m "ibpN down. Please
  reseat and clean ibpN on node …" -r <ZONE>` command. The specific
  ibp interface (e.g. `ibp2`) is extracted from the HO description;
  falls back to generic `ibp` when no number is present.
- DO project dedup against **open** tickets only (`statusCategory !=
  Done`) so a previously-closed reseat doesn't suppress a new flap.
  If an open DO ticket about ibp / reseat / fiber for the node exists,
  the candidate is downgraded to NOOP and surfaced in a new
  `=== IBP reseat candidates skipped (N) ===` block above the plan.
  JIRA outages (failed query) trigger the same defensive downgrade.
- New `ActionKind.IBP_RESEAT` + prompt letter `[i]bp-reseat`. Combos
  work (`i` alone, `a` for all, `p,i` to combine with power-drain).
  Precedence:
  `POWER_DRAIN > DRIVE_INSPECT > XID_109_RETURN_TO_READY > IBP_RESEAT > HO_TICKET > NOOP`.
- New `frops.ibp_reseat` module: pure-functional, capture-injected.
  `description_mentions_ibp`, `find_ibp_label_in_description`,
  `build_do_search_jql`, `collect_ibp_reseat_candidates`, and
  `apply_ibp_reseat_overrides` are all individually testable.

### Fixed
- DRIVE_INSPECT's `cwctl ticket -r <REGION>` value now comes from the
  BMN's `ds.coreweave.com/physical-topology.zone` label (e.g. `RNO2A`),
  not `…/physical-topology.region` (e.g. `RNO2`). cwctl's ticket region
  is the granular zone string. The `BMNTarget.region` field name is
  unchanged because it describes "what cwctl expects in -r" — only the
  source label moves.

### Added
- **Drive-inspect DCT ticket workflow** for `view sku --action`. When a
  GH200 BMN reports `CW0810: No drives were detected` in AWX, the
  planner files a DCT ticket via
  `cwctl ticket dct-action device <SERIAL> -m "..." -r <REGION>` — but
  only after checking JIRA project `DO` for an existing open OR closed
  ticket about drive inspect/install for the node, so we don't create
  duplicates. If a ticket already exists, the action is downgraded to
  NOOP and the ticket key is surfaced in a new
  `=== Existing DO tickets / unchecked ===` block above the plan. If
  JIRA can't be queried at all, every eligible BMN is downgraded
  defensively (creating a duplicate is worse than skipping for one
  cycle).
- New `ActionKind.DRIVE_INSPECT` in `frops.action` and prompt letter
  `[d]rives-ticket`. Standard prompt combos apply (`d` alone, `a` for
  all, `p,d` to combine with power-drain). Precedence between
  classifications:
  power-drain > drive-inspect > ho-ticket > NOOP.
- New `frops.drive_inspect` module (pure + injectable, like
  `frops.xid109`): `build_search_jql(identifiers)` renders the
  no-status-filter DO project JQL with stemmed drive/install/inspect
  keywords; `resolve_drive_inspect(actions, targets, search_fn)`
  applies the dedup pass and returns
  `(rewritten_actions, [DriveInspectResolution, ...])`.
- `BMNTarget.region` field (sourced from
  `ds.coreweave.com/physical-topology.region` label) so the
  `cwctl ticket` command can render the `-r RNO2`-style flag from
  the kubectl JSON without an extra round-trip.

### Added
- `frops.colors` — small TTY-aware ANSI helper (`yellow`/`cyan`/
  `magenta`/`dim`). Respects `NO_COLOR`, `FROPS_NO_COLOR`, and the
  `FROPS_FORCE_COLOR` override for scripted captures. Used by every
  renderer that prints BMN identifiers, HO ticket keys, or DEVICESLOT
  values:
  - **BMN names** rendered in yellow (first table, plan, access check,
    XID 109 waiting list, missing-CW-NODE section, execution summary).
  - **HO ticket keys** rendered in cyan (plan's "append to HO-12345"
    line, XID 109 waiting `HO TICKET` column, execution summary).
  - **DEVICESLOT** values rendered in magenta (first table).
  - `(none)` placeholders rendered dim so empty-CW-NODE rows
    visually subside.
- `frops.view_table.render_sku_view_table(raw)` — position-aware
  Python renderer for `kubectl get bmns -o wide` that replaces the
  prior `awk + column -t` shell pipeline.

### Changed
- `frops view sku <SKU>` first table now goes through
  `capture_command` + `frops.view_table.render_sku_view_table`
  instead of the previous `awk + column -t` pipeline. This fixes a
  regression where rows with an **empty CW-NODE** had their
  `EXISTS`/`ONLINE` values shifted left into the CW-NODE column
  (awk's default field splitter collapses runs of whitespace, so a
  missing cell silently disappears). The renderer slices each row at
  the header's column positions, so empty cells stay empty —
  `(none)` is substituted for missing CW-NODE so the row is
  unambiguous. The 17-column subset is unchanged.
- The renderer **truncates** long `CLUSTER` (28 chars) and
  `NODE-PROFILE` (24 chars) values with a `…` indicator so the
  trimmed table fits on typical terminal widths. Full values are
  still available via `bmns -o wide <BMN>` for deep dives. Tune the
  caps in `frops.view_table.TRUNCATE_LIMITS`.
- The auto-wrap toggle (`\e[?7l` / `\e[?7h`) stays around the SKU
  view render so wide rows still truncate cleanly at narrow terminal
  widths.

### Removed
- `SKU_VIEW_COLUMN_PIPELINE` from `frops.catalog` — the shell pipeline
  is replaced by the Python renderer above.

### Fixed
- `JIRAClient.search` now hits `/rest/api/3/search/jql` instead of the
  legacy `/rest/api/3/search` endpoint, which Atlassian retired and now
  returns `HTTP 410 Gone`. The HO-ticket resolver and XID-109 pipeline
  both went silent on the affected BMNs after the deprecation. Payload
  shape is unchanged for our use case (jql + maxResults + fields). See
  https://developer.atlassian.com/changelog/#CHANGE-2046.

### Added
- **XID-109 return-to-ready pipeline** in `view sku --action`. When a
  BMN is in `CWNC-STATE=triage` and has an open `HO` ticket whose
  description mentions XID 109 (covers `XID 109`, `Xid109`,
  `XID-109`, and the embedded `…TimeoutXid109` form), the planner
  splits matches into two lists based on `PhaseState.reason`:
  - **List A (actionable)**: `reason=nlcc` (both flcc and nlcc have
    reached triage). Becomes a new `ActionKind.XID_109_RETURN_TO_READY`
    in the plan, with command
    `cwctl flcc node -w return-to-ready <BMN> -o -m "sending node back
    to ready, node failed prod for XID 109"`.
  - **List B (waiting)**: any other `reason`. Rendered above the plan
    as `=== XID 109 BMNs waiting for return-to-fleetops (N) ===` with
    each BMN's CWNC-STATE, PhaseState reason, and HO ticket. Not
    actionable until propagation completes.
  Power-drain takes precedence — a BMN already classified as
  `power-drain` is never reclassified. JIRA creds are required; the
  pipeline soft-skips with a stderr note when they're missing.
- New prompt letter **`[x]`** for the XID-109 group. Standard combos
  apply (`x` alone, `a` for all, `p,x` for combinations).
- New module `frops.xid109` (pure-functional + capture-injected like
  `frops.access`): `parse_cwnc_states(bmns_wide_output)` extracts
  `{bmn: CWNC-STATE}` from a batched `bmns -o wide` call;
  `description_mentions_xid_109(text)` runs the regex match;
  `fetch_phase_reason(bmn, capture)` reads the `PhaseState` condition
  via `kubectl get bmn -o jsonpath`; `collect_xid109_candidates(...)`
  runs the four-stage filter pipeline; `split_by_actionable` /
  `render_waiting_summary` shape the output.
- `JIRAClient.fetch_description(issue_key)` promoted to public — used
  by the XID-109 pipeline to scan ticket descriptions.

### Changed
- `frops view sku <SKU> --action` prompt now offers per-group selection
  via letter shortcuts:
  `Run? [a]ll / [p]ower-drain (N) / [h]o-ticket (N) / [n]oop-access (N)
  / Enter to abort`. Only groups present in the plan appear. Multi-pick
  via comma (`p,h`). Replaces the old `[y/N]` binary prompt.
- The NOOP access check is now **gated by the `n` selection** at the
  prompt (or run automatically under `--yes`'s "all" selection). Pre-
  vious release ran it unconditionally; now it only fires when the
  operator opts in.
- `--yes` semantics unchanged in intent — still skips the prompt and
  selects every available group, including the access check when
  NOOP-clean targets exist.

### Changed
- Access check enforces a **20-second wall-clock timeout** on each
  `jumpipmitool` probe. Unreachable BMCs that previously could wedge
  the IPMI handshake for minutes now report
  `timed out after 20s` and the row is marked unreachable; the pass
  continues with the next BMN. Detail is rendered as the explicit
  timeout string (not partial ipmitool output) so the failure mode is
  unambiguous in the access table.
- `frops.commands.capture_command(command, timeout=None)` — new
  optional `timeout` arg. When the timeout fires, returns
  `(partial_output_with_marker, 124)` — exit code 124 matches GNU
  `timeout`'s convention so callers can distinguish a timeout from a
  regular non-zero exit. Subprocess is killed when the timeout fires.
- BMNs without a `status.reportedNodeInfo.nodeName` (no CW-NODE) are
  no longer silently logged via the
  `(skipped N BMN(s) with empty CW-NODE: …)` line. They now get:
  - A dedicated **`=== BMNs missing CW-NODE (N) ===`** table above the
    plan, listing each BMN with its `WORKFLOW` / `WORKFLOW-STEP` /
    `STATE` from labels — enough to triage what they're doing without
    having to re-run `bmns -o wide`.
  - Inclusion in the **access-check pool** so `jumpipmitool` probes
    them for reachability alongside the NOOP-no-codes BMNs. The
    `[n]oop-access` count in the prompt is the combined total. Their
    rows in the rendered access table read `(none)` in the `CW-NODE`
    column so they're unambiguous.
- Access-check section header renamed from
  `=== Access check (NOOP nodes without CW codes) ===` to
  `=== Access check (NOOP + missing CW-NODE) ===` to reflect the
  combined pool.

### Added
- `frops.access.render_missing_cwnode_summary(targets)` — renderer for
  the new dedicated section.
- `_build_targets` now returns `(targets, missing_cwnode)` where both
  are `list[BMNTarget]`. CW-NODE-less items skip the awxstat lookup
  (no node to query) but carry full SKU / serial / workflow / state
  labels so the diagnostic surface has everything it needs.
- `WORKFLOW-STEP` column in the `--action` access-check table, between
  `WORKFLOW` and `STATE`. Sourced from
  `flcc.coreweave.com/workflow-step` label on the BMN. New
  `BMNTarget.workflow_step` field carries it from `_build_targets` into
  `AccessReport.workflow_step` for rendering.
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
