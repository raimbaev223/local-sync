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
        if a.get("password"):           # shared password both sides agree on
            result = _call("pair_psk_begin", peer=a["peer"], password=a["password"])
            result["next"] = ("Enter the same password on the other machine: "
                              "local_sync_accept(peer=..., password=...)")
        else:                           # code generated here, typed in there
            result = _call("pair_auto", peer=a["peer"])
            result["next"] = (f"Read this code to the other machine and confirm there: "
                              f"local_sync_accept(peer=..., code={result['code']})")
        return result

    return _run(go, args)


def accept(args: dict) -> str:
    def go(a: dict) -> dict:
        secret = a.get("password") or a.get("code")
        if not secret:
            raise RuntimeError("нужен код с другой машины")
        return _call("pair_psk_accept", peer=a["peer"], password=secret)

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


def sessions(args: dict) -> str:
    """Pull the peer's chat history into this profile (insert-only)."""
    return _run(lambda a: _call("sessions", peer=a["peer"]), args)


def profiles(args: dict) -> str:
    return _run(lambda a: _call("peer_profiles", peer=a["peer"]), args)


def import_profile(args: dict) -> str:
    def go(a: dict) -> dict:
        return _call("import_profile", peer=a["peer"], profile=a["profile"],
                     local_name=a.get("local_name"), overwrite=bool(a.get("overwrite")))

    return _run(go, args)


HANDLERS = {
    "local_sync_status": status,
    "local_sync_peers": peers,
    "local_sync_pair": pair,
    "local_sync_accept": accept,
    "local_sync_plan": plan,
    "local_sync_apply": apply,
    "local_sync_sessions": sessions,
    "local_sync_profiles": profiles,
    "local_sync_import_profile": import_profile,
}
