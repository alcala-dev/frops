"""Builds the shell command frops uses to invoke `awxstat`.

When `AWX_USERNAME` and `AWX_PASSWORD` are already exported in the
environment, we route through a small zsh shim that short-circuits
awxstat's credential sourcing. Without the shim, awxstat's
unconditional `source_passkeys` call fails any time Doppler returns
HTTP 403 (stale CLI token) or the 1Password desktop app can't be
reached (e.g. from a non-TTY subprocess like `frops view sku
--action`) — *even though the env vars in place would have worked for
the actual API call*.

When the env vars are absent we fall through to the real `awxstat` so
first-run users get its standard credential-setup guidance instead of
a silent bypass.

The shim lives next to this module under `scripts/awxstat_envbypass.zsh`
so it ships with the wheel; the path is resolved at runtime via
`importlib.resources`.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from importlib import resources
from pathlib import Path

AWX_USERNAME_ENV: str = "AWX_USERNAME"
AWX_PASSWORD_ENV: str = "AWX_PASSWORD"


def shim_path() -> Path:
    """Absolute path to the bundled awxstat envbypass shim.

    Resolved via `importlib.resources` so the same call works in
    editable installs (`pip install -e .`) and built wheels.
    """
    resource = resources.files("frops").joinpath("scripts/awxstat_envbypass.zsh")
    # `files()` returns a Traversable; `Path(...)` only works for
    # MultiplexedPath / PosixPath cases, which is what hatch wheels
    # produce. `as_file` handles edge cases (e.g. zipped installs)
    # but our package never ships zipped, so a direct cast is fine.
    return Path(str(resource))


def awxstat_command(
    limit_type: str,
    bmn: str,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """Render the shell command frops uses to invoke `awxstat` for one BMN.

    Routes through the bundled shim when both `AWX_USERNAME` and
    `AWX_PASSWORD` are present in `env` (defaults to `os.environ`);
    falls back to the real `awxstat` otherwise. The shim itself does
    the same env check internally — this Python-side branch is for
    discoverability and to skip the shim's `command -v awxstat` lookup
    when we can.

    `limit_type` and `bmn` are interpolated unquoted to match the
    existing call style — both come from kubectl output and are
    safely alphanumeric.
    """
    environ = env if env is not None else os.environ
    if environ.get(AWX_USERNAME_ENV) and environ.get(AWX_PASSWORD_ENV):
        return f"{shlex.quote(str(shim_path()))} -l {limit_type} {bmn}"
    return f"awxstat -l {limit_type} {bmn}"
