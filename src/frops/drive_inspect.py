"""DO ticket resolver for `ActionKind.DRIVE_INSPECT`.

Workflow this module supports:

  1. `classify()` (in `frops.action`) tags BMNs reporting `CW0810` on
     `GPU-GH200-01` as `DRIVE_INSPECT` with a `cwctl ticket dct-action`
     command pre-rendered against the node's serial / cw-node / region.
  2. Before that command is offered at the prompt, this resolver checks
     the JIRA `DO` project for an existing ticket about drive
     inspect/install for the same node (open OR closed status). If one
     exists, the action is downgraded to `NOOP` so a duplicate ticket
     isn't created. If JIRA is unavailable, the resolver downgrades
     defensively (creating a possibly-duplicate ticket is more painful
     than skipping for one cycle).

JQL shape:

  project = DO
  AND (summary ~ "<bmn>" OR description ~ "<bmn>" OR …)
  AND (summary ~ "drive" OR description ~ "drive"
       OR summary ~ "install" OR description ~ "install"
       OR summary ~ "inspect" OR description ~ "inspect")

No status filter — closed tickets count too. The keyword AND clause keeps
unrelated DO tickets for the same node from masking a genuine missing-
drive situation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from frops.action import ActionKind, BMNTarget, PlannedAction

DO_PROJECT: str = "DO"

# Drive-related keywords ORed into the JQL. JIRA's `~` operator does
# language-aware stemming, so `drive` matches `drive`/`drives`/`driving`
# etc. We add `install`/`inspect` explicitly because they're verbs the
# operator naturally uses in DO ticket titles.
DRIVE_KEYWORDS: tuple[str, ...] = ("drive", "install", "inspect")


@dataclass(frozen=True)
class DriveInspectResolution:
    """Per-BMN outcome from the DO ticket lookup. Returned alongside the
    rewritten action list so the CLI can render an informational note
    explaining why an eligible BMN was skipped."""

    bmn: str
    existing_ticket: str | None  # DO ticket key when found
    error: str | None  # Reason the lookup was skipped (JIRA down, etc.)


# `identifiers` -> (issue_key_or_None, error_message_or_None). The CLI
# binds this to a JIRAClient.search closure; tests pass a dict-backed fake.
# The error channel lets the resolver distinguish "no match" from
# "couldn't query" — the former triggers ticket creation, the latter is
# treated like an existing match (skip creation) to avoid duplicates.
SearchFn = Callable[[tuple[str, ...]], tuple[str | None, str | None]]


def build_search_jql(identifiers: tuple[str, ...]) -> str | None:
    """Render the JQL that finds drive-related DO tickets for a node.

    Returns None when no identifiers are usable (avoids running a query
    that would match the entire DO project). Empty strings in `identifiers`
    are dropped.
    """
    cleaned = [i for i in identifiers if i]
    if not cleaned:
        return None
    id_clauses = " OR ".join(
        f'summary ~ "{_escape(i)}" OR description ~ "{_escape(i)}"' for i in cleaned
    )
    keyword_clauses = " OR ".join(
        f'summary ~ "{kw}" OR description ~ "{kw}"' for kw in DRIVE_KEYWORDS
    )
    return f"project = {DO_PROJECT} AND ({id_clauses}) AND ({keyword_clauses})"


def _escape(value: str) -> str:
    """JQL text-value escaping — backslash + double-quote."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def resolve_drive_inspect(
    actions: list[PlannedAction],
    targets_by_bmn: dict[str, BMNTarget],
    search_fn: SearchFn,
) -> tuple[list[PlannedAction], list[DriveInspectResolution]]:
    """For each DRIVE_INSPECT action, query JIRA; downgrade to NOOP on hit.

    Returns `(rewritten_actions, resolutions)`:
      - `rewritten_actions` mirrors the input, with DRIVE_INSPECT entries
        that already have a DO ticket (or where JIRA wasn't reachable)
        flipped to NOOP with an explanatory `notes` string.
      - `resolutions` carries the existing-ticket key / error per BMN
        so the CLI can render a "skipped: DO-12345" informational table.
    """
    resolved: list[PlannedAction] = []
    resolutions: list[DriveInspectResolution] = []
    for action in actions:
        if action.kind is not ActionKind.DRIVE_INSPECT:
            resolved.append(action)
            continue

        target = targets_by_bmn.get(action.bmn)
        identifiers = target.search_identifiers if target else (action.bmn,)
        existing, error = search_fn(identifiers)

        if existing is None and error is None:
            # No existing ticket; nothing more to do — keep the
            # DRIVE_INSPECT command intact so it can run.
            resolved.append(action)
            continue

        # Either a matching ticket was found OR the search couldn't run.
        # Either way, downgrade to NOOP to avoid risking a duplicate.
        if existing is not None:
            note = (
                f"Found existing DO ticket {existing} mentioning drive "
                "inspect/install — skipping new ticket creation."
            )
        else:
            note = (
                "Could not verify DO project for an existing drive ticket "
                f"({error}). Skipping new ticket creation to avoid duplicates."
            )
        resolved.append(
            PlannedAction(
                bmn=action.bmn,
                cw_node=action.cw_node,
                sku=action.sku,
                kind=ActionKind.NOOP,
                triggering_codes=action.triggering_codes,
                command=None,
                notes=note,
            )
        )
        resolutions.append(
            DriveInspectResolution(
                bmn=action.bmn,
                existing_ticket=existing,
                error=error,
            )
        )

    return resolved, resolutions
