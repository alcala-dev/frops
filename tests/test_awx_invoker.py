"""Tests for the awxstat command builder + shim path resolution."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from frops.awx_invoker import (
    AWX_PASSWORD_ENV,
    AWX_USERNAME_ENV,
    awxstat_command,
    shim_path,
)

# --------------------------- shim_path -------------------------------------


def test_shim_path_resolves_to_a_real_executable_in_the_package() -> None:
    path = shim_path()
    assert path.exists(), f"shim should ship with the package: {path}"
    assert path.is_file()
    assert os.access(str(path), os.X_OK), "shim must be executable"
    # Sanity: it's the right file (looks like the wrapper, not some
    # random script that happened to ship under scripts/).
    head = path.read_text().splitlines()[:3]
    assert head[0].startswith("#!"), "shim must start with a shebang"
    assert "zsh" in head[0].lower()


# --------------------------- awxstat_command -------------------------------


def test_command_uses_shim_when_both_creds_in_env() -> None:
    env = {AWX_USERNAME_ENV: "u", AWX_PASSWORD_ENV: "p"}
    cmd = awxstat_command("mgmt", "ss900770x4123", env=env)
    assert "awxstat_envbypass.zsh" in cmd
    assert "-l mgmt ss900770x4123" in cmd


def test_command_falls_through_to_real_awxstat_when_creds_missing() -> None:
    # No env vars at all → real awxstat (so first-run users see its
    # standard credential setup guidance instead of a silent bypass).
    cmd = awxstat_command("bmc", "ss900770x4999", env={})
    assert cmd.startswith("awxstat ")
    assert "awxstat_envbypass.zsh" not in cmd
    assert "-l bmc ss900770x4999" in cmd


def test_command_falls_through_when_only_username_set() -> None:
    # Missing password → can't shortcut auth; defer to real awxstat.
    env = {AWX_USERNAME_ENV: "u"}
    cmd = awxstat_command("mgmt", "bmn-x", env=env)
    assert cmd.startswith("awxstat ")
    assert "awxstat_envbypass.zsh" not in cmd


def test_command_falls_through_when_only_password_set() -> None:
    env = {AWX_PASSWORD_ENV: "p"}
    cmd = awxstat_command("mgmt", "bmn-x", env=env)
    assert cmd.startswith("awxstat ")
    assert "awxstat_envbypass.zsh" not in cmd


def test_command_treats_empty_creds_as_missing() -> None:
    # An env var set to the empty string shouldn't count as "creds
    # present" — match what the shim itself does.
    env = {AWX_USERNAME_ENV: "", AWX_PASSWORD_ENV: ""}
    cmd = awxstat_command("mgmt", "bmn-x", env=env)
    assert cmd.startswith("awxstat ")


def test_command_quotes_shim_path_for_safety_against_spaces() -> None:
    # The shim path itself is package-internal so usually has no spaces,
    # but quoting it is cheap insurance against odd install layouts
    # (e.g. `/Users/Some Name/...` editable installs).
    env = {AWX_USERNAME_ENV: "u", AWX_PASSWORD_ENV: "p"}
    cmd = awxstat_command("mgmt", "bmn-x", env=env)
    # Either quoted (shlex.quote → '...') or unquoted absolute path
    # is fine — the test just confirms the path piece is one token.
    shim = shim_path()
    assert str(shim) in cmd or f"'{shim}'" in cmd


def test_command_defaults_to_os_environ_when_env_param_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When `env=…` is omitted the builder reads `os.environ` directly.
    monkeypatch.setenv(AWX_USERNAME_ENV, "u")
    monkeypatch.setenv(AWX_PASSWORD_ENV, "p")
    cmd = awxstat_command("mgmt", "bmn-x")  # no env=… argument
    assert "awxstat_envbypass.zsh" in cmd

    monkeypatch.delenv(AWX_USERNAME_ENV, raising=False)
    monkeypatch.delenv(AWX_PASSWORD_ENV, raising=False)
    cmd_no_creds = awxstat_command("mgmt", "bmn-x")
    assert "awxstat_envbypass.zsh" not in cmd_no_creds


# --------------------------- shim script content sanity --------------------


def test_shim_script_short_circuits_when_creds_set() -> None:
    # We don't actually exec the shim in unit tests (it depends on
    # `command -v awxstat` etc.) — but we DO want to know if a future
    # edit accidentally drops the env-var check.
    body = Path(shim_path()).read_text()
    assert "AWX_USERNAME" in body
    assert "AWX_PASSWORD" in body
    # The defining trick: strip the trailing `main "$@"` and override
    # source_passkeys before calling main ourselves.
    assert "source_passkeys() { return 0; }" in body
    assert 'main "$@"' in body
    assert "sed -e '$d'" in body


# --------------------------- _collect_awx_reports integration ---------------


def test_collect_awx_reports_routes_through_shim_when_creds_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When the env has both AWX creds, the CLI's awxstat call should
    # go through the bundled shim. We don't actually execute the shim;
    # we fake `capture_command` and assert the command string contains
    # the shim path instead of the literal `awxstat`.
    from frops.cli import _collect_awx_reports

    monkeypatch.setenv(AWX_USERNAME_ENV, "u")
    monkeypatch.setenv(AWX_PASSWORD_ENV, "p")

    seen: list[str] = []

    def _fake_capture(cmd: str, **_kw: object) -> tuple[str, int]:
        seen.append(cmd)
        # Empty awxstat output is fine — parser returns a blank report
        # and the test isn't asserting on report content.
        return "", 0

    monkeypatch.setattr("frops.cli.capture_command", _fake_capture)

    reports = _collect_awx_reports("ss900770x4123")

    # Two limit types (mgmt, bmc) → two commands, both via the shim.
    assert len(seen) == 2
    assert all("awxstat_envbypass.zsh" in c for c in seen)
    assert any("-l mgmt ss900770x4123" in c for c in seen)
    assert any("-l bmc ss900770x4123" in c for c in seen)
    assert len(reports) == 2


def test_collect_awx_reports_uses_plain_awxstat_when_creds_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Default conftest fixture clears AWX env vars, so we don't need to
    # delete them here — just exercise the code path explicitly.
    from frops.cli import _collect_awx_reports

    seen: list[str] = []

    def _fake_capture(cmd: str, **_kw: object) -> tuple[str, int]:
        seen.append(cmd)
        return "", 0

    monkeypatch.setattr("frops.cli.capture_command", _fake_capture)

    _collect_awx_reports("bmn-x")

    assert len(seen) == 2
    assert all(c.startswith("awxstat ") for c in seen), seen
    assert all("awxstat_envbypass.zsh" not in c for c in seen)
