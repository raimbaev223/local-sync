"""local-sync backend for the desktop plugin, mounted at /api/plugins/local-sync/.

A thin proxy: the daemon owns discovery, pairing and merging; this module only
talks to its loopback control endpoint. Kept dependency-light on purpose — it is
imported by the gateway at startup, so nothing here may touch the network or the
filesystem at import time.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import APIRouter

router = APIRouter()


def _home() -> Path:
    return Path(os.environ.get("LOCAL_SYNC_HOME") or (Path.home() / ".local-sync")).expanduser()


def _spawn_daemon() -> None:
    """Start the sync daemon detached, so the pane works without touching a terminal."""
    plugin_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(plugin_root), env.get("PYTHONPATH", "")]))
    env["LOCAL_SYNC_HOME"] = str(_home())
    log_path = _home() / "daemon.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(log_path, "ab") as log_file:
            subprocess.Popen(
                [sys.executable, "-m", "local_sync", "run"],
                cwd=str(plugin_root), env=env, stdin=subprocess.DEVNULL,
                stdout=log_file, stderr=log_file, start_new_session=True,
            )
    except OSError:
        pass


def _ctl(op: str, **args: Any) -> dict:
    answer = _ctl_once(op, **args)
    if answer.get("ok") is False and _looks_like_no_daemon(answer):
        _spawn_daemon()
        deadline = time.time() + 8.0
        while time.time() < deadline:
            time.sleep(0.4)
            answer = _ctl_once(op, **args)
            if not (answer.get("ok") is False and _looks_like_no_daemon(answer)):
                break
    return answer


def _looks_like_no_daemon(answer: dict) -> bool:
    text = str(answer.get("error", ""))
    return "не запущен" in text or "не отвечает" in text


def _ctl_once(op: str, **args: Any) -> dict:
    home = _home()
    try:
        config = json.loads((home / "config.json").read_text())
        state = json.loads((home / "state.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "error": "демон не запущен: нет ~/.local-sync/config.json"}
    token = state.get("ctl_token")
    if not token:
        return {"ok": False, "error": "демон не запущен"}
    body = json.dumps({"op": op, "args": args}).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{config['api_port']}/ctl",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "X-Hermes-Sync-Ctl": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())
    except urllib.error.URLError as exc:
        return {"ok": False, "error": f"демон не отвечает: {exc.reason}"}


@router.get("/status")
def status() -> dict:
    return _ctl("status")


@router.get("/peers")
def peers() -> dict:
    return _ctl("peers")


@router.post("/search")
def search(body: dict) -> dict:
    return _ctl("search", enabled=bool(body.get("enabled")))


@router.post("/pair")
def pair(body: dict) -> dict:
    if body.get("password"):
        return _ctl("pair_psk_begin", peer=body.get("peer", ""), password=body["password"])
    return _ctl("pair_begin", peer=body.get("peer", ""))


@router.post("/accept")
def accept(body: dict) -> dict:
    if body.get("password"):
        return _ctl("pair_psk_accept", peer=body.get("peer", ""), password=body["password"])
    return _ctl("pair_accept", peer=body.get("peer", ""), code=body.get("code", ""))


@router.post("/deny")
def deny(body: dict) -> dict:
    return _ctl("deny", pair_id=body.get("pair_id", ""))


@router.post("/plan")
def plan(body: dict) -> dict:
    return _ctl("plan", peer=body.get("peer", ""))


@router.post("/sync")
def sync(body: dict) -> dict:
    return _ctl("sync", peer=body.get("peer", ""))
