"""The laptop's scheduled runs (2026-11-18): a macOS launchd agent that runs
`uv run jobhunt run --local-only` at 08:00 and 20:00. A run missed while the Mac slept starts when it
wakes (launchd does this for calendar times), `caffeinate -i` keeps it awake for the run, and the
output goes to logs/local-run.log. The agent's file is written at install time from this folder's
path, so no personal path is stored in the repository.

    uv run jobhunt launchd install    # once
    uv run jobhunt launchd token      # once: a long-lived Claude token, kept in the Keychain
    uv run jobhunt launchd status
    uv run jobhunt launchd uninstall

One fixed Claude account (2026-11-18): the job does not use whoever is logged in to the `claude` CLI on
the laptop. It reads a long-lived token (`claude setup-token`) from the macOS Keychain, as GitHub's
runs read theirs from a secret, and passes it as CLAUDE_CODE_OAUTH_TOKEN. Without a stored token it
runs with the login, and Today says so.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from pathlib import Path
from typing import Any

LABEL = "com.jobhunt.local-run"
TIMES = ((8, 0), (20, 0))
LOG = Path("logs") / "local-run.log"
KEYCHAIN_SERVICE = "jobhunt-claude-token"  # the Keychain item holding the token (never a file)
SECURITY = "/usr/bin/security"
MAX_LOG_BYTES = 2_000_000  # the log is cut to its last half at a run when it grows past this


def agent_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def script(root: Path, uv: str) -> str:
    """The shell line the agent runs: trim the log if large, then the run, kept awake."""
    log = root / LOG
    return (f'mkdir -p "{log.parent}"; '
            f'if [ -f "{log}" ] && [ $(wc -c < "{log}") -gt {MAX_LOG_BYTES} ]; then '
            f'tail -c {MAX_LOG_BYTES // 2} "{log}" > "{log}.tmp" && mv "{log}.tmp" "{log}"; fi; '
            f'echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) local run ==="; '
            f'token=$({SECURITY} find-generic-password -s {KEYCHAIN_SERVICE} -w 2>/dev/null); '
            f'if [ -n "$token" ]; then export CLAUDE_CODE_OAUTH_TOKEN="$token"; '
            f'else echo "No token in the Keychain ({KEYCHAIN_SERVICE}): using the claude login"; fi; '
            f'unset token; export JOBHUNT_LAUNCHD=1; '
            f'cd "{root}" && exec /usr/bin/caffeinate -i "{uv}" run jobhunt run --local-only')


def plist(root: Path, uv: str, path_dirs: list[str]) -> dict[str, Any]:
    log = str(root / LOG)
    return {
        "Label": LABEL,
        "ProgramArguments": ["/bin/sh", "-c", script(root, uv)],
        "WorkingDirectory": str(root),
        "StartCalendarInterval": [{"Hour": h, "Minute": m} for h, m in TIMES],
        "StandardOutPath": log, "StandardErrorPath": log,
        # uv, claude (for scoring) and git must be found without a login shell.
        "EnvironmentVariables": {"PATH": ":".join(dict.fromkeys(path_dirs + ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]))},
        "ProcessType": "Standard",
        "RunAtLoad": False,
    }


def tool_dirs() -> list[str]:
    dirs = []
    for tool in ("uv", "claude", "git"):
        found = shutil.which(tool)
        if found:
            dirs.append(str(Path(found).parent))
    return dirs


def install(root: Path, run=subprocess.run, home: Path | None = None) -> Path:
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is not on PATH")
    path = agent_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    (root / LOG).parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(plist(root.resolve(), uv, tool_dirs())))
    domain = f"gui/{os.getuid()}"
    run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)  # an older copy, if any
    done = run(["launchctl", "bootstrap", domain, str(path)], capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {(done.stderr or done.stdout).strip()[:300]}")
    return path


def uninstall(run=subprocess.run, home: Path | None = None) -> bool:
    run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    path = agent_path(home)
    if path.exists():
        path.unlink()
        return True
    return False


def has_token(run=subprocess.run) -> bool:
    found = run([SECURITY, "find-generic-password", "-s", KEYCHAIN_SERVICE], capture_output=True, text=True)
    return found.returncode == 0


def store_token(run=subprocess.run) -> bool:
    """Ask for the token and keep it in the login Keychain (`security` prompts for it, so it never
    appears in a command line or a file). Replaces an earlier one."""
    done = run([SECURITY, "add-generic-password", "-U", "-a", "jobhunt", "-s", KEYCHAIN_SERVICE,
                "-l", "Firstlight: Claude token for the laptop's scheduled runs", "-w"])
    return done.returncode == 0


def status(root: Path, run=subprocess.run, home: Path | None = None) -> str:
    loaded = run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, text=True)
    lines = [f"Agent file: {agent_path(home)} ({'present' if agent_path(home).exists() else 'missing'})",
             "Loaded: " + ("yes" if loaded.returncode == 0 else "no (run: uv run jobhunt launchd install)"),
             "Claude account: " + ("the token in the Keychain" if has_token(run) else
                                   "the claude login (store a token: uv run jobhunt launchd token)")]
    if loaded.returncode == 0:
        for line in loaded.stdout.splitlines():
            if any(k in line for k in ("last exit code", "runs =", "state =")):
                lines.append("  " + line.strip())
    log = root / LOG
    if log.exists():
        tail = log.read_text(errors="replace").splitlines()[-8:]
        lines.append(f"Log ({log}), last lines:")
        lines += ["  " + t for t in tail]
    return "\n".join(lines)
