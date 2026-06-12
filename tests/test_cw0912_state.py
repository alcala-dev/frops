"""Tests for the CW0912 per-BMN state file IO."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from frops.cw0912_state import (
    STAGE_POWER_DRAIN_SCHEDULED,
    STAGE_TRAY_RESEAT_FILED,
    STATE_DIR_ENV,
    CW0912State,
    clear_state,
    read_state,
    state_dir,
    state_path,
    write_state,
)


@pytest.fixture()
def state_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path))
    return tmp_path


# --------------------------- state_dir / state_path --------------------------


def test_state_dir_honors_env_override(state_tmp: Path) -> None:
    assert state_dir() == state_tmp


def test_state_path_uses_bmn_filename(state_tmp: Path) -> None:
    assert state_path("ss900770x4123") == state_tmp / "ss900770x4123.json"


# --------------------------- write_state / read_state ------------------------


def test_write_then_read_round_trips(state_tmp: Path) -> None:
    state = CW0912State(
        bmn="ss900770x4123",
        job_id="320078",
        observed_at="2026-06-11T17:00:00Z",
        stage=STAGE_POWER_DRAIN_SCHEDULED,
    )
    write_state(state)
    loaded = read_state("ss900770x4123")
    assert loaded == state


def test_write_creates_parent_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Point the env var at a directory that does NOT yet exist; write
    # should mkdir -p so callers don't have to bootstrap.
    nested = tmp_path / "a" / "b" / "c"
    assert not nested.exists()
    monkeypatch.setenv(STATE_DIR_ENV, str(nested))
    write_state(
        CW0912State(
            bmn="bmn-x",
            job_id="1",
            observed_at="2026-06-11T17:00:00Z",
            stage=STAGE_POWER_DRAIN_SCHEDULED,
        )
    )
    assert (nested / "bmn-x.json").exists()


def test_write_is_atomic_via_temp_rename(state_tmp: Path) -> None:
    write_state(
        CW0912State(
            bmn="bmn-x",
            job_id="1",
            observed_at="2026-06-11T17:00:00Z",
            stage=STAGE_POWER_DRAIN_SCHEDULED,
        )
    )
    # No `.json.tmp` should remain after a successful write.
    leftover = list(state_tmp.glob("*.json.tmp"))
    assert leftover == []


def test_read_returns_none_when_missing(state_tmp: Path) -> None:
    assert read_state("does-not-exist") is None


def test_read_returns_none_on_corrupt_json(state_tmp: Path) -> None:
    (state_tmp / "corrupt.json").write_text("{not json")
    assert read_state("corrupt") is None


def test_read_returns_none_on_missing_fields(state_tmp: Path) -> None:
    # Missing `stage` key — fall back to None rather than raising,
    # so a broken state file doesn't block remediation.
    (state_tmp / "partial.json").write_text(json.dumps({"bmn": "partial", "job_id": "1"}))
    assert read_state("partial") is None


# --------------------------- clear_state -----------------------------------


def test_clear_state_removes_existing_file(state_tmp: Path) -> None:
    write_state(
        CW0912State(
            bmn="bmn-x",
            job_id="1",
            observed_at="2026-06-11T17:00:00Z",
            stage=STAGE_TRAY_RESEAT_FILED,
        )
    )
    assert (state_tmp / "bmn-x.json").exists()
    clear_state("bmn-x")
    assert not (state_tmp / "bmn-x.json").exists()


def test_clear_state_is_idempotent(state_tmp: Path) -> None:
    # No file → no error.
    clear_state("never-existed")
