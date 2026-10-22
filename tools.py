"""Tool handlers. Every handler returns a JSON string and never raises."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .local_sync import core

_SETTINGS: dict[str, Any] = {}
DAEMON_START_TIMEOUT = 20.0


def configure(settings: dict[str, Any]) -> None:
    """Called from register() with the values of config_schema."""
    _SETTINGS.clear()
    _SETTINGS.update({k: v for k, v in settings.items() if v is not None})


def _home() -> Path:
    return Path(os.environ.get("LOCAL_SYNC_HOME") or (Path.home() / ".local-sync")).expanduser()


def _store() -> core.Store:
    """Open the state dir and mirror UI settings into the daemon config."""
    store = core.Store(_home())
    changed = False
    for key in ("notify", "sync_interval", "roots", "auto_start"):
        value = _SETTINGS.get(key)
        if value is not None and store.config.get(key) != value:
            store.config[key] = value
            changed = True
    if changed:
        store.save()
    return store


def _daemon(store: core.Store) -> dict | None:
    """Return the daemon's status, starting it in the background when allowed."""
    return core.ensure_daemon(store, timeout=DAEMON_START_TIMEOUT)


def _call(op: str, **kwargs: Any) -> dict:
    store = _store()
    if _daemon(store) is None:
        raise RuntimeError(
            "local-sync daemon is not running and did not start — see "
            f"{store.home / 'daemon.log'}"
        )
    answer = core.ctl(store, op, **kwargs)
    if answer is None:
        raise RuntimeError("local-sync daemon stopped responding")
    if not answer.get("ok"):
        raise RuntimeError(answer.get("error") or "local-sync call failed")
    return answer["result"]


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _run(fn, args: dict) -> str:
    try:
        return _json(fn(args or {}))
    except Exception as exc:  # a tool call must never blow up the turn
        return _json({"ok": False, "error": str(exc)})


# --------------------------------------------------------------------------- tools


def status(args: dict) -> str:
    return _run(lambda _: _call("status"), args)


def peers(args: dict) -> str:
    return _run(lambda _: _call("peers"), args)


def pair(args: dict) -> str:
    def go(a: dict) -> dict:
        op = "pair_psk_begin" if a.get("password") else "pair_begin"
        kwargs = {"peer": a["peer"]}
        if a.get("password"):
            kwargs["password"] = a["password"]
        result = _call(op, **kwargs)
        if a.get("password"):
            result["next"] = (
                "On the other machine enter the SAME password: local_sync_accept(peer=..., password=...) "
                f"or `local-sync ui`. Codes must match: {result.get('code')}"
            )
        else:
            result["next"] = (
                f"Confirm the code on the other machine: local_sync_accept(peer=..., code={result['code']}) "
                "or `local-sync accept <this-machine> --code " + result["code"] + "`."
            )
        return result

    return _run(go, args)


def accept(args: dict) -> str:
    def go(a: dict) -> dict:
        if a.get("password"):
            return _call("pair_psk_accept", peer=a["peer"], password=a["password"])
        return _call("pair_accept", peer=a["peer"], code=a.get("code"))

    return _run(go, args)


def plan(args: dict) -> str:
    def go(a: dict) -> dict:
        result = _call("plan", peer=a["peer"])
        counts: dict[str, int] = {}
        for item in result.get("plan", []):
            counts[item["action"]] = counts.get(item["action"], 0) + 1
        result["summary"] = counts
        return result

    return _run(go, args)


def apply(args: dict) -> str:
    return _run(lambda a: _call("sync", peer=a["peer"]), args)


HANDLERS = {
    "local_sync_status": status,
    "local_sync_peers": peers,
    "local_sync_pair": pair,
    "local_sync_accept": accept,
    "local_sync_plan": plan,
    "local_sync_apply": apply,
}
