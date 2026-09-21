"""Enable/disable the Disaster Sentinel coordinator heartbeat cron.

Primary runner: user crontab every 15 minutes → ``sentinel-cli run-heartbeat``.
The OpenClaw agentTurn job is kept disabled (LLM path is fragile / duplicates work).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

HEARTBEAT_CRON_ID = "55327caa-e310-4506-8f82-10c4ee751a98"
HEARTBEAT_CRON_NAME = "Disaster Sentinel coordinator heartbeat"
SENTINEL_ROOT = os.environ.get(
    "SENTINEL_ROOT",
    "/mnt/secondary/home/vijaymuruganprimary/.openclaw/workspace",
)
OPENCLAW_HOME = Path(
    os.environ.get("OPENCLAW_HOME")
    or os.environ.get("OPENCLAW_STATE_DIR")
    or Path.home() / ".openclaw"
)
# Prefer the workspace sidecar path used by this deploy.
JOBS_JSON_CANDIDATES = [
    Path("/mnt/secondary/home/vijaymuruganprimary/.openclaw/cron/jobs.json"),
    OPENCLAW_HOME / "cron" / "jobs.json",
]
HEARTBEAT_PAYLOAD_MESSAGE = (
    "Run Disaster Sentinel heartbeat with no freestyle tools.\n\n"
    f"Exec exactly once (host=gateway, ask=off, security=full, workdir={SENTINEL_ROOT}):\n\n"
    f"{SENTINEL_ROOT}/bin/sentinel-cli run-heartbeat --format json\n\n"
    "Reply with ONLY this short text (no report body):\n"
    "ok missions_ran=<n> skipped=<n> errored=<n> folded=<ids or none>\n"
    "Use numbers from the JSON. Slack delivery is handled by the CLI — do not paste stdout."
)

CRONTAB_MARKER = "Disaster Sentinel heartbeat"
CRONTAB_LOG = "/tmp/sentinel-heartbeat-cron.log"
CRONTAB_LINE = (
    f"*/15 * * * * cd {SENTINEL_ROOT} && "
    f"{SENTINEL_ROOT}/bin/sentinel-cli run-heartbeat --format json "
    f">> {CRONTAB_LOG} 2>&1"
)


def _jobs_json_path() -> Path:
    for path in JOBS_JSON_CANDIDATES:
        if path.is_file():
            return path
    return JOBS_JSON_CANDIDATES[0]


def _load_jobs_file() -> Dict[str, Any]:
    path = _jobs_json_path()
    if not path.is_file():
        return {"version": 1, "jobs": []}
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _save_jobs_file(data: Dict[str, Any]) -> None:
    path = _jobs_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def _find_job_in_store(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for job in data.get("jobs") or []:
        if not isinstance(job, dict):
            continue
        if job.get("id") == HEARTBEAT_CRON_ID or job.get("name") == HEARTBEAT_CRON_NAME:
            return job
    return None


def _set_job_enabled(enabled: bool) -> Dict[str, Any]:
    data = _load_jobs_file()
    job = _find_job_in_store(data)
    if not job:
        return {
            "ok": False,
            "error": "heartbeat_job_missing",
            "jobs_path": str(_jobs_json_path()),
        }
    previous = bool(job.get("enabled"))
    job["enabled"] = bool(enabled)
    if "payload" in job and isinstance(job["payload"], dict):
        job["payload"]["message"] = HEARTBEAT_PAYLOAD_MESSAGE
    _save_jobs_file(data)
    return {
        "ok": True,
        "job_id": job.get("id") or HEARTBEAT_CRON_ID,
        "enabled": bool(enabled),
        "previous_enabled": previous,
        "jobs_path": str(_jobs_json_path()),
        "via": "jobs_json",
    }


def _run_openclaw(args: List[str], *, timeout: int = 60) -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            ["openclaw", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc), "args": args}
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    payload: Any = None
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = None
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "payload": payload,
        "args": args,
    }


def _read_crontab() -> List[str]:
    try:
        proc = subprocess.run(
            ["crontab", "-l"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    return (proc.stdout or "").splitlines()


def _write_crontab(lines: List[str]) -> Dict[str, Any]:
    body = "\n".join(lines).rstrip() + "\n"
    try:
        proc = subprocess.run(
            ["crontab", "-"],
            input=body,
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stderr": (proc.stderr or "").strip(),
    }


def _crontab_has_heartbeat(lines: Optional[List[str]] = None) -> bool:
    rows = lines if lines is not None else _read_crontab()
    return any("sentinel-cli run-heartbeat" in line and not line.strip().startswith("#") for line in rows)


def ensure_os_crontab() -> Dict[str, Any]:
    """Install the */15 user crontab line for sentinel-cli run-heartbeat."""
    lines = _read_crontab()
    kept = [
        line
        for line in lines
        if "sentinel-cli run-heartbeat" not in line and CRONTAB_MARKER not in line
    ]
    already = _crontab_has_heartbeat(lines)
    kept.append(f"# {CRONTAB_MARKER} (every 15 min)")
    kept.append(CRONTAB_LINE)
    written = _write_crontab(kept)
    return {
        "ok": bool(written.get("ok")),
        "action": "already_present" if already else "installed",
        "via": "user_crontab",
        "line": CRONTAB_LINE,
        "log": CRONTAB_LOG,
        "error": written.get("stderr") or written.get("error"),
    }


def disable_os_crontab() -> Dict[str, Any]:
    """Remove the sentinel heartbeat line from the user crontab."""
    lines = _read_crontab()
    if not lines:
        return {"ok": True, "action": "missing", "via": "user_crontab", "enabled": False}
    kept = [
        line
        for line in lines
        if "sentinel-cli run-heartbeat" not in line and CRONTAB_MARKER not in line
    ]
    # Drop trailing empty lines left by marker-only removals
    while kept and not kept[-1].strip():
        kept.pop()
    if kept == lines:
        return {"ok": True, "action": "already_disabled", "via": "user_crontab", "enabled": False}
    written = _write_crontab(kept)
    return {
        "ok": bool(written.get("ok")),
        "action": "disabled",
        "via": "user_crontab",
        "enabled": False,
        "error": written.get("stderr") or written.get("error"),
    }


def _disable_openclaw_agent_job() -> Dict[str, Any]:
    """Keep the OpenClaw agentTurn job off so it does not double-fire with OS cron."""
    existing = find_heartbeat_job()
    if not existing:
        return {"ok": True, "action": "missing", "enabled": False}
    job_id = existing.get("id") or HEARTBEAT_CRON_ID
    if existing.get("enabled") is False:
        return {"ok": True, "action": "already_disabled", "job_id": job_id, "enabled": False}
    cli = _run_openclaw(["cron", "disable", str(job_id)])
    patched = _set_job_enabled(False)
    return {
        "ok": True,
        "action": "disabled",
        "job_id": job_id,
        "enabled": False,
        "via": "openclaw_cli" if cli.get("ok") else "jobs_json",
        "cli_error": None if cli.get("ok") else (cli.get("stderr") or cli.get("error")),
        "jobs_path": patched.get("jobs_path"),
    }


def find_heartbeat_job() -> Optional[Dict[str, Any]]:
    data = _load_jobs_file()
    return _find_job_in_store(data)


def ensure_heartbeat_cron() -> Dict[str, Any]:
    """Idempotently enable the global coordinator heartbeat (OS crontab)."""
    os_cron = ensure_os_crontab()
    openclaw = _disable_openclaw_agent_job()
    return {
        "ok": bool(os_cron.get("ok")),
        "action": os_cron.get("action"),
        "enabled": True,
        "via": "user_crontab",
        "os_crontab": os_cron,
        "openclaw_agent_job": openclaw,
        "schedule": "*/15 * * * *",
        "command": CRONTAB_LINE,
    }


def disable_heartbeat_cron() -> Dict[str, Any]:
    os_cron = disable_os_crontab()
    openclaw = _disable_openclaw_agent_job()
    return {
        "ok": bool(os_cron.get("ok")),
        "action": os_cron.get("action"),
        "enabled": False,
        "via": "user_crontab",
        "os_crontab": os_cron,
        "openclaw_agent_job": openclaw,
    }


def disable_heartbeat_cron_if_idle(active_mission_count: int) -> Dict[str, Any]:
    if active_mission_count > 0:
        # Re-assert OS crontab while missions are active.
        ensured = ensure_heartbeat_cron()
        ensured["action"] = "keep_enabled"
        ensured["active_mission_count"] = active_mission_count
        return ensured
    result = disable_heartbeat_cron()
    result["active_mission_count"] = 0
    return result
