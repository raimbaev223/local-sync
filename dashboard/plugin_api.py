"""local-sync backend for the desktop plugin, mounted at /api/plugins/local-sync/.

A thin proxy: the daemon owns discovery, pairing and merging; this module only
talks to its loopback control endpoint. Kept dependency-light on purpose — it is
imported by the gateway at startup, so nothing here may touch the network or the
filesystem at import time.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import APIRouter

router = APIRouter()


def _home() -> Path:
    return Path(os.environ.get("LOCAL_SYNC_HOME") or (Path.home() / ".local-sync")).expanduser()


def _ctl(op: str, **args: Any) -> dict:
    home = _home()
    try:
        config = json.loads((home / "config.json").read_text())
        state = json.loads((home / "state.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "error": "local-sync ещё не запускался: нет ~/.local-sync/config.json"}
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
