"""Run a local tool with stored keys in its environment (ADR-0032, ADR-0036, ADR-0038).

``python -m trader.config.secrets_exec NAME -- CMD [ARGS...]`` is what ``.mcp.json`` launches;
``trader secrets exec`` calls the same ``run``. The launcher stays resident for the life of every
Claude Code session, so this module imports only the key store and the trust-store writer: never
the market-data stack (pandas, numpy) or the database layer. ``tests/test_secrets.py`` checks that.

Nothing is printed to stdout: an MCP server speaks JSON-RPC there.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence

from trader.config.secrets import SecretsError, child_environment, keys_dir, service_name
from trader.data.sources.http import write_os_trust_store

USAGE = "usage: trader secrets exec NAME -- <command> [args...]"


class ExecUsageError(ValueError):
    """The command line names no command to run."""


def resolve_command(command: Sequence[str], environ: Mapping[str, str]) -> list[str]:
    """Drop a leading ``--`` and point ``uv``/``uvx`` at the uv that launched us."""
    resolved = list(command)
    if resolved and resolved[0] == "--":
        resolved = resolved[1:]
    if not resolved:
        raise ExecUsageError(USAGE)
    if resolved[0] in ("uv", "uvx"):
        # Under `uv run` the project venv shadows any global uv; uv exports its own path as UV.
        uv = shutil.which("uv") or environ.get("UV")
        if uv:
            prefix = ["tool", "run"] if resolved[0] == "uvx" else []
            resolved = [uv, *prefix, *resolved[1:]]
    return resolved


def run(name: str, command: Sequence[str]) -> int:
    """Run ``command`` with the keys of secret ``name``; return its exit code.

    Raises ``ExecUsageError`` or ``SecretsError`` before anything is started. For Alpaca, paper
    trading is forced on; a plain service gets its forced safety settings (``child_environment``).
    The OS trust store is passed as SSL_CERT_FILE and REQUESTS_CA_BUNDLE.
    """
    resolved = resolve_command(command, os.environ)
    env = child_environment(service_name(name), os.environ)
    bundle = keys_dir().parent / "os-trust-store.pem"
    write_os_trust_store(bundle)
    env["SSL_CERT_FILE"] = str(bundle)
    env["REQUESTS_CA_BUNDLE"] = str(bundle)
    return subprocess.run(resolved, env=env, check=False).returncode  # inherits stdio


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0].startswith("-"):
        print(USAGE, file=sys.stderr)
        return 2
    try:
        return run(args[0], args[1:])
    except (ExecUsageError, SecretsError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - console entry
    raise SystemExit(main())
