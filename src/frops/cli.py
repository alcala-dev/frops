"""FROps CLI — argparse setup and subcommand handlers."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from frops import __version__
from frops.access import (
    access_check_targets,
    check_all,
    render_access_summary,
    render_missing_cwnode_summary,
)
from frops.action import (
    ActionKind,
    BMNTarget,
    PlannedAction,
    actionable_actions,
    classify,
    execute_plan,
    render_execution_summary,
    render_jira_block,
    render_plan,
    resolve_ho_tickets,
)
from frops.awx import AWXReport, parse_awxstat
from frops.catalog import (
    ANALYZE_COMMANDS,
    FAIL_COMMANDS,
    FAIL_TYPES,
    OWNERSHIP_LABEL_TEMPLATE,
    SKU_VIEW_TEMPLATE,
    SKU_VIEW_TEMPLATE_JSON,
)
from frops.commands import capture_command, run_command
from frops.cw0912 import (
    render_node_zap_rerun_summary,
    schedule_node_zap_reruns,
)
from frops.cw0912_remediation import (
    CW0912Candidate,
    apply_cw0912_overrides,
    clear_recovered_cw0912_states,
    collect_cw0912_candidates,
    persist_cw0912_state_after_execute,
    render_cw0912_skipped_summary,
    render_rma_template,
    resolve_cw0912_rma_tickets,
)
from frops.cw0912_state import (
    clear_state as clear_cw0912_state,
)
from frops.cw0912_state import (
    read_state as read_cw0912_state,
)
from frops.cw0912_state import (
    write_state as write_cw0912_state,
)
from frops.drive_inspect import (
    DriveInspectResolution,
    resolve_drive_inspect,
)
from frops.drive_inspect import (
    build_search_jql as build_do_search_jql,
)
from frops.ibp_reseat import (
    IBPReseatCandidate,
    apply_ibp_reseat_overrides,
    collect_ibp_reseat_candidates,
    render_ibp_skipped_summary,
)
from frops.ibp_reseat import (
    build_do_search_jql as build_ibp_do_search_jql,
)
from frops.jira import (
    DEFAULT_PROJECT as JIRA_PROJECT,
)
from frops.jira import (
    DEFAULT_STATUSES as JIRA_OPEN_STATUSES,
)
from frops.jira import (
    JIRAClient,
    JIRAError,
    build_search_jql,
)
from frops.view_table import render_sku_view_table
from frops.xid109 import (
    XID109Candidate,
    collect_xid109_candidates,
    fetch_phase_reason,
    parse_cwnc_states,
    render_actionable_summary,
    render_waiting_summary,
    return_to_ready_command,
    split_by_actionable,
)

VIEW_TYPES: tuple[str, ...] = (*FAIL_TYPES, "sku")


def _splice_user_filter(base: str, user_filter: str | None) -> str:
    """Splice an ownership label into a base command's trailing quoted selector.

    The base command ends with `…'`; the owner filter is inserted just before
    the closing quote so the resulting selector stays a single, valid kubectl
    argument.
    """
    if not user_filter:
        return base
    ownership_label = OWNERSHIP_LABEL_TEMPLATE.format(user=user_filter)
    return base[:-1] + f",{ownership_label}'"


def build_command(fail_type: str, user_filter: str | None) -> str:
    """Return the kubectl command for a fail type, optionally filtered by owner."""
    return _splice_user_filter(FAIL_COMMANDS[fail_type], user_filter)


def build_sku_command(sku: str, user_filter: str | None) -> str:
    """Return the raw kubectl wide-format command for a SKU view.

    Column trimming + colorization happens in Python (see
    `frops.view_table.render_sku_view_table`) so empty cells stay
    in their right column and BMN / DEVICESLOT get colors.
    """
    return _splice_user_filter(SKU_VIEW_TEMPLATE.format(sku=sku), user_filter)


def build_sku_command_json(sku: str, user_filter: str | None) -> str:
    """Return the JSON-output variant of the SKU view command.

    Used by `--action` to get structured BMN data (CW-NODE, labels) without
    affecting the human-facing colored output of the wide view.
    """
    return _splice_user_filter(SKU_VIEW_TEMPLATE_JSON.format(sku=sku), user_filter)


def format_section(label: str, cmd: str, output: str, rc: int) -> str:
    """Format a single analyze section with a labeled block header.

    Only used on the dry-run path; live runs stream the command's own output
    (preserving colors emitted by kubectl/kubecolor and friends).
    """
    status = f"  [exit {rc}]" if rc != 0 else ""
    body = output.rstrip() or "(no output)"
    return "\n".join([f"### {label} ###{status}", f"# {cmd}", "", body, ""])


def handle_view(args: argparse.Namespace) -> int:
    action_flag = getattr(args, "action", False)
    yes_flag = getattr(args, "yes", False)

    if yes_flag and not action_flag:
        print(
            "error: --yes requires --action (nothing to confirm without a plan)",
            file=sys.stderr,
        )
        return 2

    if args.fail_type == "sku":
        if not args.sku_value:
            print(
                "error: 'view sku' requires a SKU argument (e.g. GPU-GH200-01)",
                file=sys.stderr,
            )
            return 2
        cmd = build_sku_command(args.sku_value, args.user_filter)
        subject = f"sku={args.sku_value}"
    else:
        if args.sku_value:
            print(
                f"error: 'view {args.fail_type}' takes no extra positional argument",
                file=sys.stderr,
            )
            return 2
        if action_flag:
            print(
                "error: --action is only supported with 'view sku'",
                file=sys.stderr,
            )
            return 2
        cmd = build_command(args.fail_type, args.user_filter)
        subject = args.fail_type

    owner_label = f" (owner: {args.user_filter})" if args.user_filter else ""
    print(f"Viewing: {subject}{owner_label}")
    print(f"Command: {cmd}\n")

    if args.dry_run:
        if action_flag:
            note = "(--action would also fetch JSON BMN data + per-BMN awxstat output"
            note += "; --yes would then execute the rendered cwctl commands)" if yes_flag else ")"
            print(note)
        return 0

    # SKU view: capture the kubectl wide output so the position-aware
    # renderer can subset columns, substitute `(none)` for empty CW-NODE,
    # truncate the worst-offender columns, and color BMN / DEVICESLOT.
    # Auto-wrap is toggled off around the print so the (already trimmed)
    # row still truncates cleanly at the terminal edge on narrow widths;
    # try/finally restores wrap even on Ctrl-C / render error.
    # Other fail-type views keep streaming via run_command (colors
    # preserved end-to-end through kubecolor).
    if args.fail_type == "sku":
        raw, view_rc = capture_command(cmd)
        if view_rc == 0 and raw:
            sys.stdout.write("\033[?7l")  # DEC private: disable auto-wrap
            sys.stdout.flush()
            try:
                print(render_sku_view_table(raw))
            finally:
                sys.stdout.write("\033[?7h")  # DEC private: re-enable auto-wrap
                sys.stdout.flush()
        elif raw:
            # Forward kubectl's error output verbatim on non-zero exit.
            print(raw, end="" if raw.endswith("\n") else "\n")
    else:
        view_rc = run_command(cmd)
    if not action_flag:
        return view_rc

    plan_rc = _run_sku_action_plan(args.sku_value, args.user_filter, auto_yes=yes_flag)
    return max(view_rc, plan_rc)


def _run_sku_action_plan(sku: str, user_filter: str | None, *, auto_yes: bool = False) -> int:
    """Fetch BMN JSON + awxstat output, classify, print a plan, optionally execute.

    Phase A behavior (auto_yes=False, no prompt response, or all NOOPs) prints
    the plan and returns 0. Phase B execution runs each actionable command via
    `run_command` and returns the worst exit code among them.

    Returns a non-zero exit on hard failures (kubectl JSON fetch fails, JSON
    parse fails) or on partial execution failure. Per-BMN awxstat failures are
    logged and skipped — the BMN is excluded from the plan rather than
    aborting the whole pass.
    """
    json_cmd = build_sku_command_json(sku, user_filter)
    raw, rc = capture_command(json_cmd)
    if rc != 0:
        print(
            f"error: failed to fetch BMN JSON (exit {rc}):\n{raw}",
            file=sys.stderr,
        )
        return rc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"error: failed to parse kubectl JSON output: {exc}", file=sys.stderr)
        return 1

    items = data.get("items", [])
    if not items:
        print("\n=== Planned actions ===\n\n(no BMNs returned by the SKU query)")
        return 0

    targets, missing_cwnode = _build_targets(items, sku)

    if missing_cwnode:
        print()
        print(render_missing_cwnode_summary(missing_cwnode))

    actions = [classify(target) for target in targets]

    # Resolve HO-ticket actions against JIRA before rendering the plan so
    # the user sees "would update HO-12345" rather than the cwctl fallback.
    # Soft-fail on any JIRA issue (auth missing, network down): the actions
    # stay on the cwctl fallback path and we surface the reason once.
    jira_client = _try_make_jira_client()
    targets_by_bmn = {t.bmn: t for t in targets}
    if jira_client is not None and any(a.kind is ActionKind.HO_TICKET for a in actions):
        actions = _resolve_with_jira(actions, jira_client, targets_by_bmn)

    # XID-109 detour: for triage BMNs whose HO ticket mentions XID 109, run
    # the return-to-ready pipeline. Power-drain wins (per spec) — only NOOP
    # and HO-ticket actions are eligible to be reclassified as XID-109.
    # JIRA is required; we soft-skip when JIRA is unavailable.
    xid109_actionable, xid109_waiting = _maybe_run_xid109_pipeline(actions, targets, jira_client)
    if xid109_actionable:
        actions = _apply_xid109_overrides(actions, xid109_actionable)
        # Print the actionable summary before the waiting one so the
        # operator sees the "ready to run" pool first.
        print()
        print(render_actionable_summary(xid109_actionable))
    if xid109_waiting:
        print()
        print(render_waiting_summary(xid109_waiting))

    # Drive-inspect dedup: for DRIVE_INSPECT actions (CW0810 on GH200),
    # check the DO project for an existing ticket about drive inspect/
    # install for the node. If one exists (open OR closed) — or if JIRA
    # is unreachable — downgrade to NOOP so we don't risk creating a
    # duplicate DO ticket. The CLI surfaces the existing-ticket key in a
    # short informational block above the plan.
    actions, drive_resolutions = _maybe_resolve_drive_inspect(actions, targets_by_bmn, jira_client)
    if drive_resolutions:
        print()
        print(_render_drive_inspect_resolutions(drive_resolutions))

    # IBP reseat pipeline: triage BMNs with an HO ticket mentioning ibp +
    # PhaseState reason=nlcc become IBP_RESEAT (file a new DO ticket via
    # `cwctl ticket dct-action network …`), or get downgraded to NOOP when
    # an open DO ibp/reseat/fiber ticket already exists for the node.
    # Mirrors the XID-109 stage in that it filters by triage + HO desc +
    # nlcc phase, and the DRIVE_INSPECT stage in that it dedups against
    # the DO project. Power-drain / drive-inspect / xid-109 still win
    # for BMNs that qualify for multiple paths.
    ibp_candidates = _maybe_run_ibp_reseat_pipeline(actions, targets, jira_client)
    if ibp_candidates:
        actions = apply_ibp_reseat_overrides(actions, ibp_candidates)
        skipped = render_ibp_skipped_summary(ibp_candidates)
        if skipped:
            print()
            print(skipped)

    # CW0912 state-machine override (stage 2): if a GH200 already saw a
    # CW0912 power-drain (per-BMN state file at ~/.cache/frops/cw0912/)
    # and the current AWX job_id differs from the persisted one, the
    # node failed again → file a DCT tray reseat instead. Same-job
    # re-runs downgrade to NOOP with a "wait for the at-job" note.
    # Recovered BMNs (no CW0912 in current AWX) get their state cleared
    # here so a future flap restarts at stage 1.
    cw0912_candidates = _maybe_collect_cw0912_candidates(targets, jira_client)
    if cw0912_candidates:
        actions = apply_cw0912_overrides(actions, cw0912_candidates)
        # For CW0912_RMA_ESCALATE actions, look up the matching open HO
        # ticket. When found, the resolver swaps the cwctl `return-to-
        # triage` fallback for `jira_issue=<HO-key>` so the JIRA runner
        # adds the RMA-template comment. When not found, the cwctl
        # fallback runs and the operator re-runs --action on the next
        # pass for the comment to land.
        if any(a.kind is ActionKind.CW0912_RMA_ESCALATE for a in actions):
            actions = _resolve_cw0912_rma_with_jira(actions, jira_client, targets_by_bmn)
        cw0912_skipped = render_cw0912_skipped_summary(cw0912_candidates)
        if cw0912_skipped:
            print()
            print(cw0912_skipped)
    recovered = clear_recovered_cw0912_states(
        targets,
        state_reader=read_cw0912_state,
        state_clearer=clear_cw0912_state,
    )
    if recovered:
        print()
        print(
            f"info: cleared CW0912 state for {len(recovered)} recovered BMN(s): "
            f"{', '.join(recovered)}"
        )

    print()
    print(render_plan(actions))

    actionable = actionable_actions(actions)
    # Combined pool for the access pass: NOOP-no-codes from the plan AND any
    # CW-NODE-less BMNs. The reachability probe is the same shell call for
    # both, and the operator wants both groups checked under one prompt.
    access_pool = access_check_targets(actions, targets_by_bmn) + list(missing_cwnode)

    if not actionable and not access_pool:
        print("\nNothing to do.")
        return 0

    # Per-group selection: which ActionKinds run via execute_plan, and
    # whether the access-check pass fires. --yes selects everything;
    # otherwise we prompt with letter shortcuts
    # ([a]ll/[p]/[h]/[x]/[n]/Enter).
    if auto_yes:
        selected_kinds: set[ActionKind] = {a.kind for a in actionable}
        run_access = bool(access_pool)
    else:
        selected_kinds, run_access = _prompt_group_selection(actionable, access_pool)
        if not selected_kinds and not run_access:
            print("Aborted by user. Nothing was executed.")
            return 0

    plan_rc = 0
    to_execute = [a for a in actionable if a.kind in selected_kinds]
    if to_execute:
        print()  # blank line before cwctl streams its own output
        summary = execute_plan(
            to_execute,
            run_command,
            jira_runner=(
                _build_jira_runner(jira_client, targets_by_bmn) if jira_client is not None else None
            ),
        )
        print()
        print(render_execution_summary(summary))
        plan_rc = summary.worst_rc

        # CW0912 stage-1 follow-up: every successful power-drain whose
        # triggering codes include CW0912 gets a node-zap rerun queued
        # via at(1) at +15 minutes. Non-CW0912 power-drains are
        # untouched. Renders a single info block per affected BMN.
        reruns = schedule_node_zap_reruns(summary, targets_by_bmn)
        rerun_summary = render_node_zap_rerun_summary(reruns)
        if rerun_summary:
            print()
            print(rerun_summary)

        # Persist CW0912 state for every successful CW0912 action so the
        # next --action invocation can detect "same job_id → in-progress"
        # or "new job_id → escalate". POWER_DRAIN with CW0912 in codes
        # → stage POWER_DRAIN_SCHEDULED; CW0912_TRAY_RESEAT → stage
        # TRAY_RESEAT_FILED.
        if cw0912_candidates:
            persist_cw0912_state_after_execute(
                [(r.action, r.rc) for r in summary.results],
                cw0912_candidates,
                state_writer=write_cw0912_state,
                now=_utc_now_iso,
            )

    if run_access and access_pool:
        print()
        reports = check_all(access_pool, capture_command)
        print(render_access_summary(reports))

    return plan_rc


def _try_make_jira_client() -> JIRAClient | None:
    """Construct JIRAClient or print a one-line note and return None.

    HO-ticket lookup is best-effort: missing creds shouldn't break power-drain
    actions or the cwctl fallback. We print a single visible reason so the
    user knows why JIRA matching was skipped.
    """
    try:
        return JIRAClient()
    except JIRAError as exc:
        print(f"\nnote: JIRA lookup disabled — {exc}", file=sys.stderr)
        return None


def _maybe_resolve_drive_inspect(
    actions: list[PlannedAction],
    targets_by_bmn: dict[str, BMNTarget],
    jira_client: JIRAClient | None,
) -> tuple[list[PlannedAction], list[DriveInspectResolution]]:
    """Run the DO ticket dedup pass over any DRIVE_INSPECT actions.

    Pre-flight returns the input list unchanged when there are no
    DRIVE_INSPECT actions to resolve (cheap exit) or when JIRA can't be
    constructed. In the latter case we downgrade every DRIVE_INSPECT to
    NOOP defensively — creating a duplicate DO ticket is more painful
    than skipping for one cycle, so the operator gets a clear note
    pointing at the JIRA outage. Per-BMN search errors are funneled
    through the same defensive path.
    """
    if not any(a.kind is ActionKind.DRIVE_INSPECT for a in actions):
        return actions, []

    if jira_client is None:
        # Mark every DRIVE_INSPECT as "skipped due to JIRA unavailable"
        # using the resolver itself with a stub that reports the error.
        return resolve_drive_inspect(
            actions,
            targets_by_bmn,
            search_fn=lambda _ids: (None, "JIRA credentials missing"),
        )

    def _search(identifiers: tuple[str, ...]) -> tuple[str | None, str | None]:
        jql = build_do_search_jql(identifiers)
        if jql is None:
            return None, None
        try:
            issues = jira_client.search(jql)
        except JIRAError as exc:
            return None, str(exc)
        return (issues[0].key if issues else None, None)

    return resolve_drive_inspect(actions, targets_by_bmn, search_fn=_search)


def _render_drive_inspect_resolutions(resolutions: list[DriveInspectResolution]) -> str:
    """Short info block listing BMNs whose DO ticket already existed (or
    couldn't be checked). Empty when nothing was skipped."""
    if not resolutions:
        return ""
    lines: list[str] = [
        f"=== Existing DO tickets / unchecked ({len(resolutions)}) ===",
        "",
        "BMNs eligible for a drive-inspect DO ticket where one already",
        "exists (or the DO project couldn't be queried). These BMNs are",
        "downgraded to NOOP — no new ticket will be created.",
        "",
    ]
    for r in resolutions:
        if r.existing_ticket:
            lines.append(f"  - {r.bmn}  →  {r.existing_ticket}")
        else:
            lines.append(f"  - {r.bmn}  →  (skipped: {r.error})")
    return "\n".join(lines)


def _utc_now_iso() -> str:
    """ISO-8601 UTC timestamp with seconds precision, suffixed `Z`.

    Used as the `observed_at` field on CW0912 state files. Kept here
    (not in cw0912_state) so the state module stays pure-functional
    and the test fixtures supply their own clock.
    """
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _maybe_collect_cw0912_candidates(
    targets: list[BMNTarget],
    jira_client: JIRAClient | None,
) -> list[CW0912Candidate]:
    """Build per-BMN CW0912 stage decisions, including DO dedup for stage 2.

    Skipped (returns []) when JIRA is unavailable AND any candidate
    would need DO dedup — without that lookup we'd risk filing a
    duplicate tray-reseat ticket, which is worse than waiting one cycle.
    When JIRA is unavailable but no stage-2 candidates exist, we still
    return the in-progress / third-occurrence diagnoses so the operator
    sees them.
    """

    def _do_search(identifiers: tuple[str, ...]) -> tuple[str | None, str | None]:
        if jira_client is None:
            return None, "JIRA credentials missing"
        from frops.cw0912_remediation import build_tray_reseat_search_jql

        jql = build_tray_reseat_search_jql(identifiers)
        if jql is None:
            return None, None
        try:
            issues = jira_client.search(jql)
        except JIRAError as exc:
            return None, str(exc)
        return (issues[0].key if issues else None, None)

    return collect_cw0912_candidates(
        targets,
        state_reader=read_cw0912_state,
        do_search=_do_search,
    )


def _maybe_run_ibp_reseat_pipeline(
    actions: list[PlannedAction],
    targets: list[BMNTarget],
    jira_client: JIRAClient | None,
) -> list[IBPReseatCandidate]:
    """Return all IBP candidates (actionable + skipped), or [] when skipped.

    Like the XID-109 pipeline, this only runs when:
      - JIRA is available (the HO search requires it), AND
      - at least one target is currently in a non-power-drain action
        class — power-drain / drive-inspect / xid-109 results are higher
        precedence and the resolver wouldn't replace them anyway.

    Per-stage failures (HO search, description fetch, JIRA error on the
    DO query) bubble through the injected closures so the resolver can
    surface them in the skip block.
    """
    if jira_client is None:
        return []

    eligible_targets = [
        t
        for t in targets
        if not any(
            a.bmn == t.bmn
            and a.kind
            in (
                ActionKind.POWER_DRAIN,
                ActionKind.DRIVE_INSPECT,
                ActionKind.XID_109_RETURN_TO_READY,
            )
            for a in actions
        )
    ]
    if not eligible_targets:
        return []

    cwnc_states = _fetch_cwnc_states([t.bmn for t in eligible_targets])
    if not cwnc_states:
        return []

    def _ho_search(identifiers: tuple[str, ...]) -> str | None:
        try:
            jql = build_search_jql(JIRA_PROJECT, identifiers, JIRA_OPEN_STATUSES)
            issues = jira_client.search(jql)
        except (JIRAError, ValueError) as exc:
            print(
                f"warning: IBP-reseat HO search failed for {identifiers}: {exc}",
                file=sys.stderr,
            )
            return None
        return issues[0].key if issues else None

    def _fetch_desc(issue_key: str) -> str:
        try:
            return jira_client.fetch_description(issue_key)
        except JIRAError as exc:
            print(
                f"warning: IBP-reseat description fetch failed for {issue_key}: {exc}",
                file=sys.stderr,
            )
            return ""

    def _fetch_phase(bmn: str) -> str:
        return fetch_phase_reason(bmn, capture_command)

    def _do_search(identifiers: tuple[str, ...]) -> tuple[str | None, str | None]:
        jql = build_ibp_do_search_jql(identifiers)
        if jql is None:
            return None, None
        try:
            issues = jira_client.search(jql)
        except JIRAError as exc:
            return None, str(exc)
        return (issues[0].key if issues else None, None)

    return collect_ibp_reseat_candidates(
        eligible_targets,
        cwnc_states,
        ho_search=_ho_search,
        fetch_description=_fetch_desc,
        fetch_phase=_fetch_phase,
        do_search=_do_search,
    )


def _maybe_run_xid109_pipeline(
    actions: list[PlannedAction],
    targets: list[BMNTarget],
    jira_client: JIRAClient | None,
) -> tuple[list[XID109Candidate], list[XID109Candidate]]:
    """Return (actionable, waiting) XID-109 candidates, or ([], []) if skipped.

    The pipeline only runs when (a) JIRA is available and (b) at least one
    target is currently in a non-power-drain action class. Power-drain
    actions are never eligible — they take precedence per the spec.
    Failures along the way (bmns wide error, JIRA error, jsonpath error)
    surface a single stderr note and the pipeline reports nothing rather
    than aborting the larger plan.
    """
    if jira_client is None:
        return [], []
    eligible_targets = [
        t
        for t in targets
        if not any(a.bmn == t.bmn and a.kind is ActionKind.POWER_DRAIN for a in actions)
    ]
    if not eligible_targets:
        return [], []

    cwnc_states = _fetch_cwnc_states([t.bmn for t in eligible_targets])
    if not cwnc_states:
        return [], []

    def _search(identifiers: tuple[str, ...]) -> str | None:
        try:
            jql = build_search_jql(JIRA_PROJECT, identifiers, JIRA_OPEN_STATUSES)
            issues = jira_client.search(jql)
        except (JIRAError, ValueError) as exc:
            print(
                f"warning: XID-109 JIRA search failed for {identifiers}: {exc}",
                file=sys.stderr,
            )
            return None
        return issues[0].key if issues else None

    def _fetch_desc(issue_key: str) -> str:
        try:
            return jira_client.fetch_description(issue_key)
        except JIRAError as exc:
            print(
                f"warning: XID-109 description fetch failed for {issue_key}: {exc}",
                file=sys.stderr,
            )
            return ""

    def _fetch_phase(bmn: str) -> str:
        return fetch_phase_reason(bmn, capture_command)

    candidates = collect_xid109_candidates(
        eligible_targets,
        cwnc_states,
        jira_search=_search,
        fetch_description=_fetch_desc,
        fetch_phase=_fetch_phase,
    )
    return split_by_actionable(candidates)


def _fetch_cwnc_states(bmn_names: list[str]) -> dict[str, str]:
    """One `bmns -o wide` call for many BMNs → {name: CWNC-STATE}.

    bmns CLI accepts space-separated names. We pass all at once to avoid
    N round-trips. On non-zero exit we surface the stderr note and return
    {} so the XID-109 pipeline degrades quietly.
    """
    if not bmn_names:
        return {}
    cmd = "bmns -o wide " + " ".join(bmn_names)
    out, rc = capture_command(cmd)
    if rc != 0:
        print(
            f"warning: XID-109 bmns -o wide failed (exit {rc}):\n{out.strip()[:300]}",
            file=sys.stderr,
        )
        return {}
    return parse_cwnc_states(out)


def _apply_xid109_overrides(
    actions: list[PlannedAction],
    xid109_actionable: list[XID109Candidate],
) -> list[PlannedAction]:
    """Replace each matching action with an XID_109_RETURN_TO_READY entry.

    Power-drain rows are left alone (the pipeline already excluded them).
    HO-ticket and NOOP entries get the new kind + return-to-ready command
    + a note that points at the matching JIRA ticket.
    """
    actionable_by_bmn = {c.bmn: c for c in xid109_actionable}
    rewritten: list[PlannedAction] = []
    for action in actions:
        cand = actionable_by_bmn.get(action.bmn)
        if cand is None or action.kind is ActionKind.POWER_DRAIN:
            rewritten.append(action)
            continue
        rewritten.append(
            PlannedAction(
                bmn=cand.bmn,
                cw_node=cand.cw_node,
                sku=cand.sku,
                kind=ActionKind.XID_109_RETURN_TO_READY,
                triggering_codes=action.triggering_codes,
                command=return_to_ready_command(cand.bmn),
                notes=(
                    f"CWNC-STATE=triage; HO ticket {cand.jira_issue} mentions XID 109; "
                    f"PhaseState reason={cand.phase_reason} → both controllers in triage"
                ),
                jira_issue=None,  # the XID-109 cwctl is the action; no JIRA write
            )
        )
    return rewritten


def _resolve_with_jira(
    actions: list[PlannedAction],
    client: JIRAClient,
    targets_by_bmn: dict[str, BMNTarget],
) -> list[PlannedAction]:
    """Build the search closure and run the resolver. Soft-fail on JIRAError."""

    def _search(identifiers: tuple[str, ...]) -> str | None:
        try:
            jql = build_search_jql(JIRA_PROJECT, identifiers, JIRA_OPEN_STATUSES)
            issues = client.search(jql)
        except (JIRAError, ValueError) as exc:
            # Per-BMN failure: log once and fall back. Don't poison the whole
            # batch — power-drain actions in the same plan should still run.
            print(
                f"warning: JIRA search failed for {identifiers}: {exc}",
                file=sys.stderr,
            )
            return None
        # First match wins. JIRA returns issues in default order (recently
        # updated); for HO ticket dedup that's the right pick.
        return issues[0].key if issues else None

    return resolve_ho_tickets(actions, _search, targets_by_bmn)


def _resolve_cw0912_rma_with_jira(
    actions: list[PlannedAction],
    client: JIRAClient | None,
    targets_by_bmn: dict[str, BMNTarget],
) -> list[PlannedAction]:
    """Look up open HO tickets for CW0912_RMA_ESCALATE actions.

    Mirrors `_resolve_with_jira` but for the RMA escalate kind. On JIRA
    outage (missing client or JIRAError), every RMA action retains its
    cwctl return-to-triage fallback so something useful still runs.
    """
    if client is None:
        return actions  # cwctl fallback path stays in place

    def _search(identifiers: tuple[str, ...]) -> str | None:
        try:
            jql = build_search_jql(JIRA_PROJECT, identifiers, JIRA_OPEN_STATUSES)
            issues = client.search(jql)
        except (JIRAError, ValueError) as exc:
            print(
                f"warning: JIRA HO lookup failed for RMA escalation {identifiers}: {exc}",
                file=sys.stderr,
            )
            return None
        return issues[0].key if issues else None

    return resolve_cw0912_rma_tickets(actions, _search, targets_by_bmn)


def _build_jira_runner(
    client: JIRAClient,
    targets_by_bmn: dict[str, BMNTarget],
) -> Callable[[PlannedAction], int]:
    """Return a runner that dispatches per action.kind to the right JIRA call.

    HO_TICKET           → append a status block to Description (existing).
    CW0912_RMA_ESCALATE → add the RMA-template comment, interpolating
                          the BMN's serial from `targets_by_bmn`.
    """

    def _run(action: PlannedAction) -> int:
        assert action.jira_issue is not None  # guarded by execute_plan dispatch
        if action.kind is ActionKind.CW0912_RMA_ESCALATE:
            target = targets_by_bmn.get(action.bmn)
            serial = target.serial if target else ""
            body = render_rma_template(action.cw_node, action.bmn, serial)
            try:
                client.add_comment(action.jira_issue, body)
            except JIRAError as exc:
                print(
                    f"error: failed to add RMA comment on {action.jira_issue} for {action.bmn}: {exc}",
                    file=sys.stderr,
                )
                return 1
            print(f"added RMA comment on {action.jira_issue} for {action.bmn}")
            return 0
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        block = render_jira_block(action, timestamp)
        try:
            client.append_to_description(action.jira_issue, block)
        except JIRAError as exc:
            print(
                f"error: failed to update {action.jira_issue} for {action.bmn}: {exc}",
                file=sys.stderr,
            )
            return 1
        print(f"updated {action.jira_issue} for {action.bmn}")
        return 0

    return _run


def _prompt_group_selection(
    actionable: list[PlannedAction],
    access_targets: list[BMNTarget],
) -> tuple[set[ActionKind], bool]:
    """Letter-shortcut prompt: which groups should execute?

    Returns `(selected_kinds, run_access_check)`. Empty input, EOF, or
    Ctrl-C means abort — both return values are empty/False.

    Letter map (only the groups present in the plan are offered):
        a       → everything available
        p       → power-drain actions
        h       → ho-ticket actions
        n       → NOOP access check
        p,h,n   → comma-separated combinations

    Anything else (including empty/EOF) aborts. `n` (lowercase) is distinct
    from a missing/empty answer, so users can pick noop-only without it
    being interpreted as "no".
    """
    # Each option = (letter, kind-or-None, display-label, count). The letter
    # appears in brackets inside the label so the prompt reads like
    # `[p]ower-drain (3)` instead of `[p]power-drain (3)`.
    options: list[tuple[str, ActionKind | None, str, int]] = []
    counts_by_kind: dict[ActionKind, int] = {}
    for a in actionable:
        counts_by_kind[a.kind] = counts_by_kind.get(a.kind, 0) + 1
    if ActionKind.POWER_DRAIN in counts_by_kind:
        options.append(
            ("p", ActionKind.POWER_DRAIN, "[p]ower-drain", counts_by_kind[ActionKind.POWER_DRAIN])
        )
    if ActionKind.HO_TICKET in counts_by_kind:
        options.append(
            ("h", ActionKind.HO_TICKET, "[h]o-ticket", counts_by_kind[ActionKind.HO_TICKET])
        )
    if ActionKind.XID_109_RETURN_TO_READY in counts_by_kind:
        options.append(
            (
                "x",
                ActionKind.XID_109_RETURN_TO_READY,
                "[x]id-109-return",
                counts_by_kind[ActionKind.XID_109_RETURN_TO_READY],
            )
        )
    if ActionKind.DRIVE_INSPECT in counts_by_kind:
        options.append(
            (
                "d",
                ActionKind.DRIVE_INSPECT,
                "[d]rives-ticket",
                counts_by_kind[ActionKind.DRIVE_INSPECT],
            )
        )
    if ActionKind.IBP_RESEAT in counts_by_kind:
        options.append(
            (
                "i",
                ActionKind.IBP_RESEAT,
                "[i]bp-reseat",
                counts_by_kind[ActionKind.IBP_RESEAT],
            )
        )
    if ActionKind.CW0912_TRAY_RESEAT in counts_by_kind:
        options.append(
            (
                "t",
                ActionKind.CW0912_TRAY_RESEAT,
                "[t]ray-reseat",
                counts_by_kind[ActionKind.CW0912_TRAY_RESEAT],
            )
        )
    if ActionKind.CW0912_RMA_ESCALATE in counts_by_kind:
        options.append(
            (
                "r",
                ActionKind.CW0912_RMA_ESCALATE,
                "[r]ma-escalate",
                counts_by_kind[ActionKind.CW0912_RMA_ESCALATE],
            )
        )
    if access_targets:
        # ActionKind.NOOP would be misleading — NOOP actions have no command.
        # The `n` letter triggers the diagnostic access check pass instead.
        options.append(("n", None, "[n]oop-access", len(access_targets)))

    if not options:
        return set(), False

    parts = ["[a]ll"] + [f"{label} ({count})" for _, _, label, count in options]
    parts.append("Enter to abort")
    prompt = "\nRun? " + "  /  ".join(parts) + ": "

    try:
        ans = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()  # newline so the next print doesn't sit on the prompt line
        return set(), False

    if not ans:
        return set(), False

    if ans == "a":
        kinds = {kind for _, kind, _, _ in options if kind is not None}
        return kinds, any(letter == "n" for letter, _, _, _ in options)

    selected_kinds: set[ActionKind] = set()
    run_access = False
    letter_to_kind = {letter: kind for letter, kind, _, _ in options}
    for piece in (p.strip() for p in ans.split(",")):
        if piece not in letter_to_kind:
            continue  # silently ignore unknown letters
        kind = letter_to_kind[piece]
        if kind is None:
            run_access = True
        else:
            selected_kinds.add(kind)
    return selected_kinds, run_access


def _build_targets(
    items: list[dict[str, Any]], default_sku: str
) -> tuple[list[BMNTarget], list[BMNTarget]]:
    """Build BMNTargets from raw kubectl JSON `items`.

    Returns `(targets, missing_cwnode)`. Both lists contain BMNTargets;
    items end up in `missing_cwnode` when `status.reportedNodeInfo.nodeName`
    is empty — those can't be classified by AWX (no node to query), so we
    skip awxstat and leave `awx_reports=()`. They still get full workflow /
    state / TS / SKU labels so the diagnostic surface (dedicated section
    + access check) has everything it needs.
    """
    targets: list[BMNTarget] = []
    missing_cwnode: list[BMNTarget] = []

    for item in items:
        metadata = item.get("metadata") or {}
        status = item.get("status") or {}
        bmn_name = metadata.get("name") or ""
        reported_node = (status.get("reportedNodeInfo") or {}).get("nodeName") or ""

        if not bmn_name:
            continue

        labels = metadata.get("labels") or {}
        sku = labels.get("ds.coreweave.com/sku.cw-sku", default_sku)
        serial = labels.get("ds.coreweave.com/status.asset.serial", "")
        workflow = labels.get("flcc.coreweave.com/workflow", "")
        workflow_step = labels.get("flcc.coreweave.com/workflow-step", "")
        state = labels.get("flcc.coreweave.com/state", "")
        # The `physical-topology.region` label is too coarse for cwctl's
        # ticket `-r` flag (e.g. it's `RNO2` when the ticket system expects
        # `RNO2A`). Use the zone label, which carries the granular value
        # cwctl's region argument actually wants.
        region = labels.get("ds.coreweave.com/physical-topology.zone", "")

        if not reported_node:
            missing_cwnode.append(
                BMNTarget(
                    bmn=bmn_name,
                    cw_node="",
                    sku=sku,
                    awx_reports=(),
                    serial=serial,
                    workflow=workflow,
                    workflow_step=workflow_step,
                    state=state,
                    region=region,
                )
            )
            continue

        reports = _collect_awx_reports(bmn_name)
        targets.append(
            BMNTarget(
                bmn=bmn_name,
                cw_node=reported_node,
                sku=sku,
                awx_reports=tuple(reports),
                serial=serial,
                workflow=workflow,
                workflow_step=workflow_step,
                state=state,
                region=region,
            )
        )

    return targets, missing_cwnode


def _collect_awx_reports(bmn: str) -> list[AWXReport]:
    """Run awxstat -l mgmt|bmc against a BMN and parse the output.

    Failures (awxstat unavailable, non-zero exit) are logged to stderr and
    the limit type is skipped. Phase A continues with whatever it got.
    """
    reports: list[AWXReport] = []
    for limit_type in ("mgmt", "bmc"):
        text, rc = capture_command(f"awxstat -l {limit_type} {bmn}")
        if rc != 0:
            print(
                f"warning: awxstat -l {limit_type} {bmn} failed (exit {rc}); skipping this limit",
                file=sys.stderr,
            )
            continue
        reports.append(parse_awxstat(text))
    return reports


def handle_analyze(args: argparse.Namespace) -> int:
    steps = ANALYZE_COMMANDS[args.target]
    print(f"\n### Analyzing {args.target}: {args.name} ###\n")

    worst_rc = 0
    for label, template in steps:
        cmd = template.format(name=args.name)
        if args.dry_run:
            print(format_section(label, cmd, "(dry-run, not executed)", 0))
            continue
        # Stream the command's output directly so terminal colors emitted by
        # kubectl/kubecolor/yq are preserved end-to-end. Capturing would force
        # the tools into non-TTY mode and strip ANSI codes.
        print(f"### {label} ###")
        print(f"# {cmd}")
        rc = run_command(cmd)
        if rc != 0:
            print(f"[exit {rc}]")
        print()
        worst_rc = max(worst_rc, rc)
    return worst_rc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="frops",
        description="FROps workflow helper CLI tool.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Print the commands that would run without executing them.",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    view_parser = subparsers.add_parser("view", help="View types of failures.")
    view_parser.add_argument(
        "fail_type",
        choices=VIEW_TYPES,
        metavar="TYPE",
        help=(
            f"View type. Failure types: {', '.join(FAIL_TYPES)}. "
            "Use 'sku' for SKU-based filtering (requires a SKU value)."
        ),
    )
    view_parser.add_argument(
        "sku_value",
        nargs="?",
        default=None,
        metavar="SKU",
        help="SKU value, required when TYPE is 'sku' (e.g. GPU-GH200-01).",
    )
    view_parser.add_argument(
        "-u",
        metavar="USER",
        dest="user_filter",
        default=None,
        help=(
            "Filter results by ownership label. "
            "Pass 'unassigned' or a specific username, e.g. -u jdoe"
        ),
    )
    view_parser.add_argument(
        "--action",
        action="store_true",
        help=(
            "After displaying results, inspect AWX jobs per BMN, "
            "classify by CW codes, and print an action plan."
        ),
    )
    view_parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help=(
            "With --action: skip the confirmation prompt and execute the "
            "planned cwctl commands. Without --action this flag is rejected."
        ),
    )
    view_parser.set_defaults(func=handle_view)

    analyze_parser = subparsers.add_parser("analyze", help="Analyze a specific resource.")
    analyze_parser.add_argument(
        "target",
        choices=list(ANALYZE_COMMANDS.keys()),
        metavar="TARGET",
        help=f"Resource type to analyze. One of: {', '.join(ANALYZE_COMMANDS.keys())}",
    )
    analyze_parser.add_argument(
        "name",
        metavar="NAME",
        help="Name of the resource to analyze, e.g. ss929610x4724071",
    )
    analyze_parser.set_defaults(func=handle_analyze)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
