"""Per-BMN persistence for the CW0912 remediation state machine.

frops is one-shot, but the CW0912 workflow has stages that span runs:

  - 1st occurrence: power-drain + scheduled node-zap rerun (stage 1, PR #27)
  - 2nd occurrence: file DCT tray-reseat ticket (this module's reason for being)
  - 3rd occurrence: HO ticket RMA escalation (phase 3, future)

To distinguish "this is a fresh CW0912" from "the same CW0912 we already
remediated", we keep a tiny JSON file per BMN under the state dir:

    {
      "bmn": "ss900770x4123",
      "job_id": "320078",
      "observed_at": "2026-06-11T17:00:00Z",
      "stage": "power_drain_scheduled"
    }

`job_id` is the AWX job ID from the BMN's awxstat report at the time we
last took action. On subsequent runs we compare the current AWX job_id
against the stored one — if different (new job ran but CW0912 is back),
we escalate to the next stage. `observed_at` is for human inspection
(and a possible TTL later); it does not gate the state-machine.

Atomic writes: each save writes to `<bmn>.json.tmp` and renames over the
target, so concurrent reads never observe a partial JSON document.

State dir resolution:
  1. `$FROPS_STATE_DIR` env var (used by tests / sandboxes)
  2. `~/.cache/frops/cw0912/` default
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

STATE_DIR_ENV: str = "FROPS_STATE_DIR"
DEFAULT_STATE_DIR: Path = Path.home() / ".cache" / "frops" / "cw0912"

# Documented stage values. The state machine only branches on these
# exact strings — anything else is treated as unknown and the BMN
# falls back to the first-occurrence path.
STAGE_POWER_DRAIN_SCHEDULED: str = "power_drain_scheduled"
STAGE_TRAY_RESEAT_FILED: str = "tray_reseat_filed"
STAGE_RMA_ESCALATED: str = "rma_escalated"


@dataclass(frozen=True)
class CW0912State:
    """One BMN's persisted CW0912 remediation snapshot."""

    bmn: str
    job_id: str
    observed_at: str  # ISO-8601 UTC
    stage: str


def state_dir() -> Path:
    """Return the configured state directory, honoring `FROPS_STATE_DIR`."""
    override = os.environ.get(STATE_DIR_ENV)
    return Path(override) if override else DEFAULT_STATE_DIR


def state_path(bmn: str) -> Path:
    """Resolve the per-BMN state file path under the configured state dir."""
    return state_dir() / f"{bmn}.json"


def read_state(bmn: str) -> CW0912State | None:
    """Load the BMN's state, or None when no file exists / contents are bad.

    Corrupt JSON / missing fields are treated as "no state" rather than
    raising — the caller falls back to the first-occurrence path, which
    is safe (it'll just over-remediate once and rewrite the file).
    """
    path = state_path(bmn)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        # Permission error / I/O issue — treat as missing so we don't
        # block remediation on a bad cache file.
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    try:
        return CW0912State(
            bmn=str(data["bmn"]),
            job_id=str(data["job_id"]),
            observed_at=str(data["observed_at"]),
            stage=str(data["stage"]),
        )
    except (KeyError, TypeError):
        return None


def write_state(state: CW0912State) -> None:
    """Persist `state` to disk atomically (write-then-rename)."""
    target = state_path(state.bmn)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
    os.replace(tmp, target)


def clear_state(bmn: str) -> None:
    """Idempotently delete the BMN's state file."""
    try:
        state_path(bmn).unlink()
    except FileNotFoundError:
        return
