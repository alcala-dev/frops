#!/usr/bin/env zsh
#############################################################################
# awxstat_envbypass - thin wrapper around `awxstat` for frops
#
# DESCRIPTION:
#   The upstream `awxstat` script (cw-fleet-tools) always calls
#   `source_passkeys` at startup, which tries Doppler first and then
#   1Password to populate `AWX_USERNAME` / `AWX_PASSWORD`. Both helpers
#   fail under conditions frops cares about:
#     - Doppler CLI token expired (HTTP 403 Forbidden).
#     - 1Password CLI requires biometric auth via the desktop app, which
#       cannot fire from a non-TTY subprocess (i.e. anything frops calls).
#   When both helpers fail, `source_passkeys` returns 1 and `awxstat`
#   exits 1 — even when the AWX env vars are already exported in the
#   caller's shell and would have worked for the actual API call.
#
#   This wrapper short-circuits the helper sourcing without modifying
#   the upstream `awxstat` script:
#     1. Verify `AWX_USERNAME` and `AWX_PASSWORD` are set in env. If
#        not, exec the real `awxstat` so the user sees its standard
#        first-run guidance.
#     2. Source `awxstat` with the trailing `main "$@"` line stripped,
#        so every function defined inside it is available but main
#        does not auto-run during sourcing.
#     3. Redefine `source_passkeys` to `return 0` (no-op).
#     4. Call `main "$@"` ourselves with the caller's arguments.
#
#   The override is process-local and never touches the upstream file.
#
# USAGE: awxstat_envbypass.zsh -l <bmc|mgmt|ip> [--cpx] [-o <yaml|json|md>]
#                              <node> [node ...]
#   (same flags / arguments as `awxstat`)
#############################################################################

set -uo pipefail

# No env-var creds → defer to the real awxstat so the user gets its
# standard credential-setup guidance instead of a silent bypass.
if [[ -z "${AWX_USERNAME:-}" || -z "${AWX_PASSWORD:-}" ]]; then
  exec awxstat "$@"
fi

AWXSTAT_PATH=$(command -v awxstat)
if [[ -z "$AWXSTAT_PATH" || ! -r "$AWXSTAT_PATH" ]]; then
  echo "frops awxstat shim: awxstat not on PATH" >&2
  exit 127
fi

# `sed '$d'` deletes the final line, which (as of cw-fleet-tools'
# zsh rewrite) is `main "$@"`. Sourcing the rest installs all of
# awxstat's functions without auto-running main; we then override
# source_passkeys and invoke main with the caller's args ourselves.
source <(sed -e '$d' "$AWXSTAT_PATH")
source_passkeys() { return 0; }
main "$@"
