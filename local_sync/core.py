#!/usr/bin/env python3
"""local-sync — peer-to-peer profile sync over the local network.

No third-party dependencies. One process per machine:

  * UDP beacon (default :45870) so both instances discover each other
  * HTTP API  (default :45872) for pairing and file transfer
  * pairing with a shared 6-digit code the user compares on both screens
  * HMAC-authenticated requests afterwards
  * per-file three-way merge against the last synced base

Scope: user-authored skills, memories, SOUL.md, hooks, desktop-plugins,
cron definitions. Never config.yaml, .env, state.db, caches or logs.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
from collections import deque
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

VERSION = "0.1.0"
SERVICE = "local-sync"
PROTO = 1
BEACON_PORT = 45870
API_PORT = 45872

DEFAULT_ROOTS = ["skills", "memories", "SOUL.md", "hooks", "desktop-plugins", "cron"]
EXCLUDE_DIRS = {"__pycache__", ".locks", "output", "node_modules", ".git", "cache", "logs"}
EXCLUDE_SUFFIXES = (".lock", ".db", ".db-wal", ".db-shm", ".pyc", ".tmp", ".log")
EXCLUDE_NAMES = {"executions.db", ".usage.json", ".usage.json.lock"}
AUTH_WINDOW = 300


LOG_SINK = None  # set by the daemon: (level, message, hint) -> None

HINTS = (
    ("not visible", "включи поиск на обеих машинах и проверь, что демон пира запущен"),
    ("не видно", "включи поиск на обеих машинах и проверь, что демон пира запущен"),
    ("401", "пара устарела после переустановки — отключи устройство и спарь заново"),
    ("unauthorized", "пара устарела после переустановки — отключи устройство и спарь заново"),
    ("address already in use", "уже запущен второй демон — убей лишний процесс"),
    ("wrong password", "код не совпал: введи тот, что показан на первой машине"),
    ("пароли не совпали", "код не совпал: введи тот, что показан на первой машине"),
    ("too large", "снапшот истории больше лимита — почисти старые сессии"),
)


def hint_for(message: str) -> str:
    lowered = message.lower()
    for needle, hint in HINTS:
        if needle in lowered:
            return hint
    return ""


def log(msg: str, level: str = "info") -> None:
    print(f"[local-sync] {msg}", file=sys.stderr, flush=True)
    if LOG_SINK is not None:
        try:
            LOG_SINK(level, msg, hint_for(msg))
        except Exception:
            pass


def notify(title: str, body: str, enabled: bool = True) -> None:
    """Native toast so a pairing request is visible without watching a terminal."""
    if not enabled:
        return
    try:
        if sys.platform == "darwin":
            script = f'display notification "{body}" with title "{title}"'
            subprocess.run(["osascript", "-e", script], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        elif sys.platform.startswith("linux"):
            subprocess.run(["notify-send", title, body], check=False,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def default_profile_dir(profile: str) -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    candidate = Path.home() / ".hermes" / "profiles" / profile
    if candidate.exists():
        return candidate
    return Path.home() / ".hermes"


def bundled_skill_names(profile_dir: Path) -> set[str]:
    """Names shipped by Hermes itself — those must not be synced (upstream owns them)."""
    manifest = profile_dir / "skills" / ".bundled_manifest"
    names: set[str] = set()
    if manifest.exists():
        for line in manifest.read_text(errors="ignore").splitlines():
            line = line.strip()
            if line and ":" in line:
                names.add(line.split(":", 1)[0])
    return names


def is_memory_path(rel: str) -> bool:
    return rel.startswith("memories/") and rel.endswith(".md")


def union_memory(local: bytes, remote: bytes) -> bytes:
    """Memory files are newline-separated sections joined by a lone §.

    Union keeps both sides' sections, local order first, deduplicated by content.
    """
    def sections(raw: bytes) -> list[str]:
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            return []
        parts = [p.strip() for p in text.replace("\r\n", "\n").split("\n§\n")]
        return [p for p in parts if p]

    seen: set[str] = set()
    merged: list[str] = []
    for section in sections(local) + sections(remote):
        key = sha_bytes(section.encode())
        if key in seen:
            continue
        seen.add(key)
        merged.append(section)
    return ("\n§\n".join(merged) + "\n").encode() if merged else b""


PSK_ITERATIONS = 200_000


def psk_key(password: str, pair_id: str) -> str:
    """Pairing key from a shared password. Never persisted until the pair confirms."""
    return hashlib.pbkdf2_hmac("sha256", password.encode(), pair_id.encode(), PSK_ITERATIONS, 32).hex()


def psk_code(key_hex: str) -> str:
    """Short code derived from the key so both screens can be compared (SAS)."""
    return f"{int(key_hex[:8], 16) % 1_000_000:06d}"


def hmac_tag(key_hex: str, message: str) -> str:
    return hmac.new(bytes.fromhex(key_hex), message.encode(), hashlib.sha256).hexdigest()


def supersede_pending(store: "Store", peer_id: str, keep: str = "") -> int:
    """One device, one request: a newer request replaces the older ones.

    Requests are keyed by ``pair_id``, so without this every retry from the same
    machine (or the same machine behind a new instance id) added another row that
    nobody could tell apart. Only unsettled requests are dropped: an accepted one
    is the record of a pairing that already happened.
    """
    dropped = 0
    for pair_id, item in list(store.state["pending"].items()):
        if pair_id == keep or item.get("peer_id") != peer_id or item.get("accepted"):
            continue
        store.state["pending"].pop(pair_id, None)
        dropped += 1
    return dropped


# --------------------------------------------------------------------------- store


class Store:
    def __init__(self, home: Path):
        self.home = home.expanduser()
        self.home.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.home, 0o700)
        except OSError:
            pass
        self.lock = threading.RLock()
        self.config = self._load("config.json") or self._default_config()
        self.state = self._load("state.json") or {"peers": {}, "pending": {}}
        self.state.setdefault("peers", {})
        self.state.setdefault("pending", {})
        self.save()

    def _load(self, name: str) -> dict | None:
        path = self.home / name
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log(f"cannot read {path}: {exc}")
            return None

    def _default_config(self) -> dict:
        profile = os.environ.get("HERMES_PROFILE") or "algo"
        return {
            "id": uuid.uuid4().hex,
            "name": socket.gethostname().split(".")[0].lower(),
            "profile": profile,
            "profile_dir": str(default_profile_dir(profile)),
            "api_port": API_PORT,
            "beacon_port": BEACON_PORT,
            "broadcast": ["255.255.255.255", "127.255.255.255"],
            "roots": list(DEFAULT_ROOTS),
            "beacon_interval": 2.0,
            "notify": True,
            "auto_sync": True,
            "sync_interval": 300,
            "search": True,        # broadcast beacons (manual mode turns this off)
        }

    def save(self) -> None:
        with self.lock:
            atomic_write(self.home / "config.json", json.dumps(self.config, indent=2).encode())
            atomic_write(self.home / "state.json", json.dumps(self.state, indent=2).encode())
            for path in (self.home / "config.json", self.home / "state.json"):
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass

    # peers -----------------------------------------------------------------
    @property
    def me(self) -> dict:
        c = self.config
        return {"id": c["id"], "name": c["name"], "profile": c["profile"],
                "machine": socket.gethostname().lower(),
                "api_port": c["api_port"], "beacon_port": c["beacon_port"]}

    def peer(self, peer_id: str) -> dict | None:
        return self.state["peers"].get(peer_id)

    def find_peer(self, needle: str) -> tuple[str, dict] | tuple[None, None]:
        for pid, info in self.state["peers"].items():
            if needle in (pid, info.get("name"), f"{info.get('name')}/{info.get('profile')}"):
                return pid, info
        return None, None


# ----------------------------------------------------------------------- manifest


def profile_root(store: Store) -> Path:
    """The Hermes home that owns this profile (``~/.hermes`` or a profile's parent)."""
    pd = Path(store.config["profile_dir"]).expanduser()
    return pd.parent.parent if pd.parent.name == "profiles" else pd


def known_profiles(store: Store) -> dict[str, Path]:
    root = profile_root(store)
    found: dict[str, Path] = {}
    if (root / "profiles").is_dir():
        for child in sorted((root / "profiles").iterdir()):
            if child.is_dir():
                found[child.name] = child
    if (root / "config.yaml").exists() or (root / "skills").exists():
        found.setdefault("default", root)
    return found


def resolve_profile_dir(store: Store, name: str | None) -> Path:
    if not name or name == store.config.get("profile"):
        return Path(store.config["profile_dir"]).expanduser()
    profiles = known_profiles(store)
    if name not in profiles:
        raise AuthError(f"unknown profile: {name}")
    return profiles[name]


def profile_summary(path: Path) -> dict:
    def count_files(sub: str, suffix: str = "") -> int:
        target = path / sub
        if target.is_file():
            return 1
        if not target.is_dir():
            return 0
        return sum(1 for item in target.rglob("*") if item.is_file() and item.name.endswith(suffix))

    sessions = path / "state.db"
    return {
        "skills": count_files("skills", "SKILL.md"),
        "memory_bytes": sum(item.stat().st_size for item in (path / "memories").glob("*.md")) if (path / "memories").is_dir() else 0,
        "hooks": count_files("hooks"),
        "cron": count_files("cron", ".json"),
        "state_db_bytes": sessions.stat().st_size if sessions.exists() else 0,
        "has_soul": (path / "SOUL.md").exists(),
    }


def build_manifest(store: Store) -> dict[str, dict]:
    return build_manifest_at(Path(store.config["profile_dir"]).expanduser(), store.config["roots"])


def build_manifest_at(base: Path, roots: list[str]) -> dict[str, dict]:
    base = Path(base).expanduser()
    bundled = bundled_skill_names(base)
    files: dict[str, dict] = {}

    def include(path: Path, rel: str) -> None:
        st = path.stat()
        files[rel] = {"size": st.st_size, "mtime": round(st.st_mtime, 3), "sha256": sha_file(path)}

    for root in roots:
        target = base / root
        if target.is_file():
            include(target, root)
            continue
        if not target.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(target):
            here = Path(dirpath)
            rel_parts = here.relative_to(base).parts
            if any(part in bundled for part in rel_parts):
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS and not d.startswith(".")]
            for fname in filenames:
                if fname.startswith(".") or fname in EXCLUDE_NAMES:
                    continue
                if ".conflict-" in fname or fname.endswith(EXCLUDE_SUFFIXES):
                    continue
                path = here / fname
                if path.is_symlink() or not path.is_file():
                    continue
                include(path, str(path.relative_to(base)))
    return files


def safe_target(base: Path, rel: str) -> Path:
    target = (base / rel).resolve()
    if not str(target).startswith(str(base.resolve())):
        raise ValueError(f"path escapes profile: {rel}")
    return target


# ------------------------------------------------------------------------- client


class AuthError(Exception):
    pass


class PeerClient:
    def __init__(self, store: Store, peer_id: str, addr: str):
        info = store.state["peers"][peer_id]
        self.store = store
        self.peer_id = peer_id
        self.key = bytes.fromhex(info["key"])
        self.base = f"http://{addr}:{info['api_port']}"

    def _headers(self, method: str, path: str) -> dict:
        ts = str(int(time.time()))
        msg = f"{method}:{path}:{ts}".encode()
        sig = hmac.new(self.key, msg, hashlib.sha256).hexdigest()
        return {"X-Hermes-Sync": f"{self.store.me['id']}:{ts}:{sig}"}

    def _request(self, method: str, path: str, body: bytes | None = None) -> bytes:
        url = self.base + path
        req = urllib.request.Request(url, data=body, method=method, headers=self._headers(method, path))
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise AuthError(f"{method} {path} -> {exc.code}: {detail}") from exc

    def manifest(self, profile: str | None = None) -> dict:
        suffix = f"?profile={urllib.parse.quote(profile)}" if profile else ""
        return json.loads(self._request("GET", "/manifest" + suffix))["files"]

    def fetch(self, rel: str, profile: str | None = None) -> bytes:
        query = "?path=" + urllib.parse.quote(rel) + (f"&profile={urllib.parse.quote(profile)}" if profile else "")
        return self._request("GET", "/file" + query)

    def profiles(self) -> dict:
        return json.loads(self._request("GET", "/profiles"))

    def push(self, rel: str, data: bytes) -> None:
        self._request("PUT", "/file?path=" + urllib.parse.quote(rel), body=data)

    def sessions_snapshot(self) -> bytes:
        return self._request("GET", "/sessions")


def key_works(addr: str, port: int, peer_id: str, key_hex: str, timeout: float = 2.0) -> bool:
    """Does this endpoint actually accept the pair key? (guards against a stray instance)"""
    try:
        ts = str(int(time.time()))
        message = f"GET:/ping:{ts}"
        signature = hmac.new(bytes.fromhex(key_hex), message.encode(), hashlib.sha256).hexdigest()
        request = urllib.request.Request(f"http://{addr}:{port}/ping",
                                         headers={"X-Hermes-Sync": f"{peer_id}:{ts}:{signature}"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def find_addr(daemon: "SyncDaemon", peer_id: str) -> str:
    with daemon.lock:
        info = daemon.peers.get(peer_id)
    if not info:
        raise AuthError(f"peer {peer_id} is not visible on the network right now")
    return info["addr"]


# ---------------------------------------------------------------------------- plan


def make_plan(store: Store, daemon: "SyncDaemon", peer_id: str) -> list[dict]:
    remote = PeerClient(store, peer_id, find_addr(daemon, peer_id)).manifest()
    local = build_manifest(store)
    base = store.state["peers"][peer_id].get("base", {})
    actions: list[dict] = []
    for rel in sorted(set(local) | set(remote)):
        lh = local.get(rel, {}).get("sha256")
        rh = remote.get(rel, {}).get("sha256")
        bh = base.get(rel)
        if lh == rh:
            action = "same"
        elif lh is None:
            action = "pull"
        elif rh is None:
            action = "push"
        elif bh is not None and lh == bh:
            action = "pull"
        elif bh is not None and rh == bh:
            action = "push"
        elif bh is None:
            # first contact: newest wins, no conflict noise on a fresh pair
            action = "pull" if remote[rel]["mtime"] > local[rel]["mtime"] else "push"
        else:
            action = "conflict"
        actions.append({"path": rel, "action": action, "local": lh, "remote": rh, "base": bh})
    return actions


def apply_plan(store: Store, daemon: "SyncDaemon", peer_id: str, actions: list[dict]) -> dict:
    base_dir = Path(store.config["profile_dir"]).expanduser()
    client = PeerClient(store, peer_id, find_addr(daemon, peer_id))
    peer_name = store.state["peers"][peer_id].get("name", "peer")
    base = store.state["peers"][peer_id].setdefault("base", {})
    backup_dir = store.home / "backups" / time.strftime("%Y%m%d-%H%M%S")
    stats = {"pulled": 0, "pushed": 0, "conflicts": 0, "same": 0}
    conflicts: list[str] = []

    for item in actions:
        rel, action = item["path"], item["action"]
        target = safe_target(base_dir, rel)
        if action == "same":
            if item["local"]:
                base[rel] = item["local"]
            stats["same"] += 1
            continue
        if action == "pull":
            data = client.fetch(rel)
            if target.exists():
                backup = backup_dir / rel
                atomic_write(backup, target.read_bytes())
            atomic_write(target, data)
            base[rel] = sha_bytes(data)
            stats["pulled"] += 1
            continue
        if action == "push":
            data = target.read_bytes()
            client.push(rel, data)
            base[rel] = sha_bytes(data)
            stats["pushed"] += 1
            continue
        # conflict
        remote_data = client.fetch(rel)
        local_data = target.read_bytes() if target.exists() else b""
        if is_memory_path(rel):
            merged = union_memory(local_data, remote_data)
            atomic_write(target, merged)
            client.push(rel, merged)
            base[rel] = sha_bytes(merged)
            stats["pulled"] += 1
            continue
        stamp = time.strftime("%Y%m%d-%H%M%S")
        side = target.with_name(f"{target.name}.conflict-{peer_name}-{stamp}")
        atomic_write(side, remote_data)
        # local wins this round; the remote copy is kept as a sidecar. Recording the
        # remote hash as base makes the next sync converge by pushing local.
        base[rel] = item["remote"]
        conflicts.append(f"{rel} -> {Path(rel).with_name(side.name)}")
        stats["conflicts"] += 1

    store.state["peers"][peer_id]["last_sync"] = time.time()
    store.save()
    return {"stats": stats, "conflicts": conflicts}


# ------------------------------------------------------------------------- sessions


def sessions_db_path(store: Store) -> Path:
    return Path(store.config["profile_dir"]).expanduser() / "state.db"


def snapshot_sessions(db_path: Path) -> bytes:
    """Consistent copy of a live WAL database without stopping the agent."""
    if not db_path.exists():
        raise AuthError("state.db not found")
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    try:
        target = sqlite3.connect(tmp.name)
        with target:
            source.backup(target)
        target.close()
        return Path(tmp.name).read_bytes()
    finally:
        source.close()
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return []


def import_sessions(db_path: Path, payload: bytes, max_bytes: int = 200 * 1024 * 1024) -> dict:
    """Merge another machine's sessions into this profile: insert-only, no updates."""
    if len(payload) > max_bytes:
        raise AuthError(f"snapshot too large: {len(payload)} bytes")
    if not db_path.exists():
        raise AuthError("локальной state.db нет — Hermes ещё не создал её в этом профиле")
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as handle:
        handle.write(payload)
        snapshot_path = handle.name
    try:
        src = sqlite3.connect(f"file:{snapshot_path}?mode=ro", uri=True)
        dst = sqlite3.connect(str(db_path), timeout=15)
        dst.execute("PRAGMA busy_timeout=15000")
        src_sessions = _table_columns(src, "sessions")
        dst_sessions = _table_columns(dst, "sessions")
        src_messages = _table_columns(src, "messages")
        dst_messages = _table_columns(dst, "messages")
        if not src_sessions or not src_messages or not dst_sessions or not dst_messages:
            raise AuthError("state.db не похожа на хранилище сессий Hermes")
        s_cols = [c for c in src_sessions if c in dst_sessions]
        m_cols = [c for c in src_messages if c in dst_messages]
        known = {row[0] for row in dst.execute("SELECT id FROM sessions")}
        existing = {(row[0], row[1]) for row in dst.execute(
            "SELECT session_id, coalesce(message_uid, role || ':' || timestamp) FROM messages")}
        new_sessions = [dict(zip(s_cols, row)) for row in
                        src.execute(f"SELECT {', '.join(s_cols)} FROM sessions")
                        if row[s_cols.index("id")] not in known]
        new_messages = []
        for row in src.execute(f"SELECT {', '.join(m_cols)} FROM messages ORDER BY timestamp, id"):
            record = dict(zip(m_cols, row))
            key = (record.get("session_id"),
                   record.get("message_uid") or f"{record.get('role')}:{record.get('timestamp')}")
            if key in existing:
                continue
            existing.add(key)
            record.pop("id", None)          # local autoincrement decides its own ids
            new_messages.append(record)
        with dst:
            if new_sessions:
                placeholders = ", ".join("?" for _ in s_cols)
                dst.executemany(
                    f"INSERT OR IGNORE INTO sessions ({', '.join(s_cols)}) VALUES ({placeholders})",
                    [tuple(item[c] for c in s_cols) for item in new_sessions])
            if new_messages:
                cols = list(new_messages[0])
                placeholders = ", ".join("?" for _ in cols)
                dst.executemany(
                    f"INSERT INTO messages ({', '.join(cols)}) VALUES ({placeholders})",
                    [tuple(item.get(c) for c in cols) for item in new_messages])
        for table in ("messages_fts", "messages_fts_trigram"):
            try:
                dst.execute(f"INSERT INTO {table}({table}) VALUES('rebuild')")
            except sqlite3.Error:
                pass
        dst.commit()
        dst.close()
        src.close()
        return {"sessions": len(new_sessions), "messages": len(new_messages),
                "skipped_messages": sum(1 for _ in ())}
    finally:
        try:
            os.unlink(snapshot_path)
        except OSError:
            pass


# --------------------------------------------------------------------------- server


class ApiHandler(BaseHTTPRequestHandler):
    server_version = f"{SERVICE}/{VERSION}"
    store: Store
    daemon: "SyncDaemon"

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    # helpers ---------------------------------------------------------------
    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _raw(self, code: int, body: bytes, ctype: str = "application/octet-stream") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self, method: str, path: str) -> str | None:
        header = self.headers.get("X-Hermes-Sync", "")
        try:
            peer_id, ts, sig = header.split(":")
            ts_int = int(ts)
        except ValueError:
            return None
        if abs(time.time() - ts_int) > AUTH_WINDOW:
            return None
        info = self.store.peer(peer_id)
        if not info:
            return None
        expected = hmac.new(bytes.fromhex(info["key"]), f"{method}:{path}:{ts}".encode(),
                            hashlib.sha256).hexdigest()
        return peer_id if hmac.compare_digest(expected, sig) else None

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    # routes ----------------------------------------------------------------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/hello":
            me = self.store.me
            return self._json(200, {"service": SERVICE, "version": VERSION, "proto": PROTO, **me})
        if path == "/pair/status":
            pair_id = (parse_qs(parsed.query).get("pair_id") or [""])[0]
            pending = self.store.state["pending"].get(pair_id)
            status = "unknown" if not pending else ("accepted" if pending.get("accepted") else "pending")
            return self._json(200, {"status": status})
        peer_id = self._auth("GET", self.path)
        if not peer_id:
            return self._json(401, {"error": "unauthorized"})
        if path == "/ping":
            return self._json(200, {"ok": True, **self.store.me})
        if path == "/profiles":
            profiles = [{"name": name, **profile_summary(directory)}
                        for name, directory in known_profiles(self.store).items()]
            return self._json(200, {"profiles": profiles, "default": self.store.config.get("profile")})
        if path == "/sessions":
            try:
                payload = snapshot_sessions(sessions_db_path(self.store))
            except AuthError as exc:
                return self._json(404, {"error": str(exc)})
            return self._raw(200, payload, "application/x-sqlite3")
        if path == "/manifest":
            wanted = (parse_qs(parsed.query).get("profile") or [None])[0]
            try:
                directory = resolve_profile_dir(self.store, wanted)
            except AuthError as exc:
                return self._json(404, {"error": str(exc)})
            return self._json(200, {"files": build_manifest_at(directory, self.store.config["roots"]),
                                    "profile": wanted or self.store.me["profile"]})
        if path == "/file":
            rel = (parse_qs(parsed.query).get("path") or [""])[0]
            if not rel:
                return self._json(400, {"error": "path required"})
            wanted = (parse_qs(parsed.query).get("profile") or [None])[0]
            try:
                base_dir = resolve_profile_dir(self.store, wanted)
            except AuthError as exc:
                return self._json(404, {"error": str(exc)})
            try:
                target = safe_target(base_dir, rel)
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            if not target.is_file():
                return self._json(404, {"error": "not found"})
            return self._raw(200, target.read_bytes())
        return self._json(404, {"error": "unknown route"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        body = self._body()
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "bad json"})
        if path == "/pair/begin":
            return self._pair_begin(payload)
        if path == "/pair/complete":
            return self._pair_complete(payload)
        if path == "/pair/psk":
            return self._pair_psk_begin(payload)
        if path == "/pair/psk/confirm":
            return self._pair_psk_confirm(payload)
        if path == "/ctl":
            return self._ctl(payload)
        return self._json(404, {"error": "unknown route"})

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        peer_id = self._auth("PUT", self.path)
        if not peer_id:
            return self._json(401, {"error": "unauthorized"})
        if parsed.path != "/file":
            return self._json(404, {"error": "unknown route"})
        rel = (parse_qs(parsed.query).get("path") or [""])[0]
        if not rel:
            return self._json(400, {"error": "path required"})
        base_dir = Path(self.store.config["profile_dir"]).expanduser()
        try:
            target = safe_target(base_dir, rel)
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        atomic_write(target, self._body())
        return self._json(200, {"ok": True, "sha256": sha_file(target)})

    # local control surface (CLI and UI clients on this machine only) ------
    def _ctl(self, payload: dict) -> None:
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return self._json(403, {"error": "ctl is loopback-only"})
        token = self.headers.get("X-Hermes-Sync-Ctl", "")
        expected = self.store.state.get("ctl_token", "")
        if not expected or not hmac.compare_digest(token, expected):
            return self._json(401, {"error": "bad ctl token"})
        op = payload.get("op")
        args = payload.get("args") or {}
        try:
            if op == "status":
                result = self.daemon.status()
            elif op == "peers":
                result = self.daemon.peer_table()
            elif op == "pair_begin":
                result = self.daemon.pair_begin(self.daemon.resolve(args["peer"], wait=3.0))
            elif op == "pair_accept":
                result = self.daemon.pair_accept(self.daemon.resolve(args["peer"], wait=3.0), args.get("code"))
            elif op == "search":
                result = self.daemon.set_search(bool(args.get("enabled", True)))
            elif op == "deny":
                pair_id = args.get("pair_id", "")
                removed = self.store.state["pending"].pop(pair_id, None) is not None
                if removed:
                    self.store.save()
                result = {"denied": removed}
            elif op == "ping":
                result = self.daemon.ping(args["peer"])
            elif op == "unpair":
                result = self.daemon.unpair(args["peer"])
            elif op == "logs":
                result = self.daemon.logs(int(args.get("limit") or 200))
            elif op == "clear_requests":
                result = self.daemon.clear_requests()
            elif op == "pair_auto":
                result = self.daemon.pair_auto(self.daemon.resolve(args["peer"], wait=3.0))
            elif op == "pair_psk_begin":
                result = self.daemon.pair_psk_begin(self.daemon.resolve(args["peer"], wait=3.0),
                                                    args.get("password", ""))
            elif op == "pair_psk_accept":
                result = self.daemon.pair_psk_accept(self.daemon.resolve(args["peer"], wait=3.0),
                                                     args.get("password", ""))
            elif op == "peer_profiles":
                result = self.daemon.peer_profiles(self.daemon.resolve(args["peer"], wait=3.0))
            elif op == "import_profile":
                result = self.daemon.import_profile(
                    self.daemon.resolve(args["peer"], wait=3.0),
                    args["profile"], args.get("local_name"), args.get("mode") or "merge")
            elif op == "sessions":
                result = self.daemon.copy_sessions(self.daemon.resolve(args["peer"], wait=3.0))
            elif op == "plan":
                peer_id = self.daemon.resolve(args["peer"], wait=3.0)
                result = {"plan": make_plan(self.store, self.daemon, peer_id)}
            elif op == "sync":
                peer_id = self.daemon.resolve(args["peer"], wait=3.0)
                result = apply_plan(self.store, self.daemon, peer_id,
                                    make_plan(self.store, self.daemon, peer_id))
            else:
                return self._json(400, {"error": f"unknown op {op}"})
        except Exception as exc:
            return self._json(200, {"ok": False, "error": str(exc)})
        return self._json(200, {"ok": True, "result": result})

    # pairing ---------------------------------------------------------------
    def _pair_begin(self, payload: dict) -> None:
        peer_id = payload.get("id")
        if not peer_id or peer_id == self.store.me["id"]:
            return self._json(400, {"error": "bad id"})
        pair_id = uuid.uuid4().hex
        code = f"{secrets.randbelow(1_000_000):06d}"
        supersede_pending(self.store, peer_id)
        self.store.state["pending"][pair_id] = {
            "pair_id": pair_id,
            "peer_id": peer_id,
            "peer_name": payload.get("name", "?"),
            "peer_profile": payload.get("profile", "?"),
            "peer_api_port": int(payload.get("api_port") or 0),
            "peer_beacon_port": int(payload.get("beacon_port") or 0),
            "a_nonce": payload.get("nonce") or "",
            "code": code,
            "created": time.time(),
            "accepted": False,
        }
        self.store.save()
        log(f"peer {payload.get('name')} ({payload.get('profile')}) request: code {code} — "
            f"confirm with `local-sync accept {payload.get('name')}`")
        self.daemon.on_pair_requested(self.store.state["pending"][pair_id])
        return self._json(200, {"pair_id": pair_id, "code": code, "status": "pending",
                                "local": self.store.me})


    def _pair_complete(self, payload: dict) -> None:
        pair_id = payload.get("pair_id", "")
        pending = self.store.state["pending"].get(pair_id)
        if not pending:
            return self._json(404, {"error": "unknown pair"})
        a_nonce = pending.get("a_nonce") or pending.get("nonce", "")
        b_nonce = payload.get("b_nonce", "")
        peer_id = pending["peer_id"]
        key = sha_bytes(f"{a_nonce}:{b_nonce}:{self.store.me['id']}:{peer_id}".encode())
        sig = payload.get("sig", "")
        expected = hmac.new(bytes.fromhex(key), f"complete:{pair_id}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return self._json(403, {"error": "bad signature"})
        self.store.state["peers"][peer_id] = {
            "name": payload.get("name", pending["peer_name"]),
            "profile": payload.get("profile", pending["peer_profile"]),
            "api_port": int(payload.get("api_port") or pending["peer_api_port"]),
            "beacon_port": int(payload.get("beacon_port") or pending.get("peer_beacon_port") or 0),
            "addr": self.daemon.peers.get(peer_id, {}).get("addr"),
            "key": key,
            "paired_at": time.time(),
            "base": {},
        }
        pending["accepted"] = True
        self.store.save()
        log(f"paired with {self.store.state['peers'][peer_id]['name']} ({peer_id[:8]})")
        self.daemon.on_paired(peer_id)
        return self._json(200, {"ok": True, "peer": self.store.state["peers"][peer_id]["name"]})


    # password pairing (human-in-the-loop) ----------------------------------
    def _pair_psk_begin(self, payload: dict) -> None:
        peer_id = payload.get("id")
        if not peer_id or peer_id == self.store.me["id"]:
            return self._json(400, {"error": "bad id"})
        pair_id = payload.get("pair_id") or uuid.uuid4().hex
        supersede_pending(self.store, peer_id)
        self.store.state["pending"][pair_id] = {
            "pair_id": pair_id,
            "kind": "psk",
            "peer_id": peer_id,
            "peer_name": payload.get("name", "?"),
            "peer_profile": payload.get("profile", "?"),
            "peer_api_port": int(payload.get("api_port") or 0),
            "peer_beacon_port": int(payload.get("beacon_port") or 0),
            "a_nonce": payload.get("nonce") or "",
            "code": "",
            "created": time.time(),
            "accepted": False,
        }
        self.store.save()
        log(f"pairing request from {payload.get('name')} ({payload.get('profile')}): waiting for the password")
        self.daemon.on_pair_requested(self.store.state["pending"][pair_id])
        return self._json(200, {"status": "pending", "pair_id": pair_id, **self.store.me})

    def _pair_psk_confirm(self, payload: dict) -> None:
        pair_id = payload.get("pair_id", "")
        pending = self.daemon.psk_pending.get(pair_id)
        if not pending:
            return self._json(404, {"error": "unknown or expired pairing"})
        b_nonce = payload.get("b_nonce", "")
        tag = payload.get("tag", "")
        expected = hmac_tag(pending["key"], f"confirm:{pair_id}:{pending['nonce']}:{b_nonce}")
        if not hmac.compare_digest(tag, expected):
            return self._json(403, {"error": "wrong password"})
        peer_id = pending["peer_id"]
        self.store.state["peers"][peer_id] = {
            "name": payload.get("name", pending.get("peer_name") or peer_id[:8]),
            "profile": payload.get("profile", "algo"),
            "api_port": int(payload.get("api_port") or 0),
            "beacon_port": int(payload.get("beacon_port") or 0),
            "addr": self.daemon.peers.get(peer_id, {}).get("addr"),
            "key": pending["key"],
            "paired_at": time.time(),
            "base": {},
        }
        self.store.save()
        self.daemon.psk_pending.pop(pair_id, None)
        log(f"paired with {self.store.state['peers'][peer_id]['name']} (password)")
        self.daemon.on_paired(peer_id)
        ack = hmac_tag(pending["key"], f"ack:{pair_id}:{b_nonce}")
        return self._json(200, {"ok": True, "tag": ack, "peer": self.store.state["peers"][peer_id]["name"]})


# --------------------------------------------------------------------------- daemon


class SyncDaemon:
    def __init__(self, store: Store, bind: str = "0.0.0.0", api_port: int | None = None,
                 beacon_port: int | None = None):
        self.store = store
        self.log_buffer: deque = deque(maxlen=400)
        self.log_file = store.home / "logs" / "local-sync.jsonl"
        global LOG_SINK
        LOG_SINK = self._record_log
        # One daemon per state dir. A second one would beacon the same identity from
        # the same host and make every peer see this machine twice.
        import fcntl
        self._lock_file = open(store.home / "daemon.lock", "a+")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log("another local-sync daemon already runs for this state dir — exiting")
            raise SystemExit(1)
        self.lock = threading.RLock()
        self.peers: dict[str, dict] = {}
        self.events: list[dict] = []
        self._stop = threading.Event()
        self.wake = threading.Event()
        self.beacon_now = threading.Event()
        self.psk_pending: dict[str, dict] = {}
        self.bind = bind
        self.beacon_targets: list[tuple[str, int]] = []
        port = store.config["api_port"] if api_port is None else api_port
        try:
            self.server = ThreadingHTTPServer((bind, port), ApiHandler)
        except OSError:
            # Another instance holds the port; take an ephemeral one and record
            # it in config so local clients still find us.
            log(f"api port {port} is busy, falling back to an ephemeral port")
            self.server = ThreadingHTTPServer((bind, 0), ApiHandler)
        self.server.allow_reuse_address = True
        handler_cls = type("BoundHandler", (ApiHandler,), {"store": store, "daemon": self})
        self.server.RequestHandlerClass = handler_cls
        self.api_port = self.server.server_port
        store.config["api_port"] = self.api_port
        if not store.state.get("ctl_token"):
            store.state["ctl_token"] = secrets.token_hex(24)
            store.save()
        beacon = store.config["beacon_port"] if beacon_port is None else beacon_port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.bind((bind if bind != "0.0.0.0" else "", beacon))
        self.beacon_port = self.sock.getsockname()[1]
        self.send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for target in store.config.get("broadcast") or []:
            self.beacon_targets.append((target, self.beacon_port))
        store.config["beacon_port"] = self.beacon_port
        store.save()

    # lifecycle -------------------------------------------------------------
    def start(self) -> "SyncDaemon":
        threading.Thread(target=self.server.serve_forever, name="hs-http", daemon=True).start()
        threading.Thread(target=self._listen, name="hs-udp", daemon=True).start()
        threading.Thread(target=self._beacon, name="hs-beacon", daemon=True).start()
        if self.store.config.get("auto_sync", True):
            threading.Thread(target=self._autosync, name="hs-autosync", daemon=True).start()
        log(f"up: {self.store.config['name']}/{self.store.config['profile']} "
            f"api={self.api_port} beacon={self.beacon_port} profile_dir={self.store.config['profile_dir']}")
        return self

    def stop(self) -> None:
        self._stop.set()
        self.wake.set()
        self.beacon_now.set()
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass
        for sock in (self.sock, self.send_sock):
            try:
                sock.close()
            except OSError:
                pass

    def _beacon(self) -> None:
        payload = {"service": SERVICE, "proto": PROTO, "version": VERSION, **self.store.me}
        while not self._stop.is_set():
            blob = json.dumps(payload).encode()
            with self.lock:
                targets = list(self.beacon_targets) if self.store.config.get("search", True) else []
            # paired peers keep hearing from us even with discovery switched off
            for info in list(self.store.state["peers"].values()):
                addr, port = info.get("addr"), info.get("beacon_port")
                if addr and port:
                    targets.append((addr, int(port)))
            for host, port in targets:
                try:
                    self.send_sock.sendto(blob, (host, port))
                except OSError:
                    pass
            self.beacon_now.wait(float(self.store.config.get("beacon_interval", 2.0)))
            self.beacon_now.clear()

    def sync_paired(self) -> list[dict]:
        """One pass over every paired peer that is currently visible."""
        results = []
        for peer_id in list(self.store.state["peers"].keys()):
            with self.lock:
                if peer_id not in self.peers:
                    continue
            try:
                actions = make_plan(self.store, self, peer_id)
                if all(item["action"] == "same" for item in actions):
                    continue
                result = apply_plan(self.store, self, peer_id, actions)
                results.append({"peer": peer_id, **result})
                log(f"auto-sync {peer_id[:8]}: {result['stats']}")
                if result["conflicts"]:
                    self.notify_user("local-sync: конфликты", "; ".join(result["conflicts"][:3]))
            except Exception as exc:  # never let one peer kill the loop
                log(f"auto-sync {peer_id[:8]} failed: {exc}")
        return results

    def _autosync(self) -> None:
        while not self._stop.is_set():
            interval = max(5.0, float(self.store.config.get("sync_interval", 300)))
            self.wake.wait(interval)
            self.wake.clear()
            if self._stop.is_set():
                return
            self.sync_paired()

    def _listen(self) -> None:
        self.sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(64 * 1024)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                info = json.loads(data)
            except json.JSONDecodeError:
                continue
            me = self.store.me
            if info.get("service") != SERVICE or info.get("id") == me["id"]:
                continue
            if (not self.store.config.get("same_host")
                    and (info.get("machine") or "").lower() == me["machine"]):
                continue  # another local-sync on THIS host (second state dir / stale daemon)
            record = {"id": info["id"], "name": info.get("name", "?"), "profile": info.get("profile", "?"),
                      "machine": info.get("machine", ""),
                      "api_port": int(info.get("api_port") or 0), "beacon_port": int(info.get("beacon_port") or 0),
                      "addr": addr[0], "last_seen": time.time()}
            # A paired peer whose daemon restarted with a fresh state dir comes back
            # under a new id. Move the pair key over instead of losing the pair.
            with self.lock:
                for pid, known in list(self.store.state["peers"].items()):
                    if pid == record["id"] or (known.get("name"), known.get("profile")) != (record["name"], record["profile"]):
                        continue
                    # Only adopt a new instance if it proves it holds the pair key. A second
                    # daemon on the same machine answers /hello happily but 401s on everything
                    # else, and following it broke sync with a perfectly good pair.
                    if key_works(record["addr"], record["api_port"], known.get("id") or pid, known.get("key", "")):
                        migrated = {**known, "addr": record["addr"], "api_port": record["api_port"],
                                    "beacon_port": record["beacon_port"] or known.get("beacon_port")}
                        self.store.state["peers"][record["id"]] = migrated
                        del self.store.state["peers"][pid]
                        self.store.save()
                        log(f"peer {record['name']} came back as a new instance — pair moved to {record['id'][:8]}")
                    else:
                        log(f"{record['name']} answers on another port without our pair key "
                            f"— похоже, на пире запущено несколько демонов", "warn")
                        record["paired"] = False
                    break
                # one row per machine+profile: a re-installed peer may reappear under a new id
                for known_id, known in list(self.peers.items()):
                    if known_id != record["id"] and (known["name"], known["profile"]) == (record["name"], record["profile"]):
                        del self.peers[known_id]
                fresh = record["id"] not in self.peers
                record["paired"] = record["id"] in self.store.state["peers"]
                self.peers[record["id"]] = record
            if fresh:
                log(f"discovered {record['name']} ({record['profile']}) at {record['addr']}:{record['api_port']}")
                # no toast on discovery on purpose: a busy LAN would spam notifications
                self._emit({"type": "peer_found", **record})

    def _emit(self, event: dict) -> None:
        with self.lock:
            self.events.append({**event, "ts": time.time()})
        self.on_event(event)

    # hooks for the UI / plugin --------------------------------------------
    def on_event(self, event: dict) -> None:  # pragma: no cover - overridden by UI
        pass

    def on_pair_requested(self, pending: dict) -> None:
        if pending.get("kind") == "psk":
            self.notify_user(f"local-sync: {pending['peer_name']} просит подключиться",
                             "открой интерфейс (local-sync ui) и введи пароль")
        else:
            self.notify_user(f"local-sync: {pending['peer_name']} хочет синхронизироваться",
                             f"код {pending['code']} — подтверди: local-sync accept {pending['peer_name']}")
        self._emit({"type": "pair_requested", "peer": pending["peer_name"],
                    "code": pending.get("code", ""), "kind": pending.get("kind", "code")})

    def notify_user(self, title: str, body: str) -> None:
        notify(title, body, enabled=bool(self.store.config.get("notify", True)))

    def on_paired(self, peer_id: str) -> None:
        self._emit({"type": "paired", "peer": peer_id})

    # helpers for the control surface -------------------------------------
    def resolve(self, needle: str, wait: float = 3.0) -> str:
        peer_id = _resolve_peer(self.store, self, needle, time.time() + wait)
        if not peer_id:
            raise AuthError(f"peer {needle!r} is not visible on the LAN")
        return peer_id

    def status(self) -> dict:
        store = self.store
        return {"me": store.me, "profile_dir": store.config["profile_dir"],
                "roots": store.config["roots"], "api_port": self.api_port,
                "search": self.search_enabled(),
                "paired": {pid: {"name": p.get("name"), "profile": p.get("profile"),
                                 "addr": p.get("addr"), "last_sync": p.get("last_sync"),
                                 "base_files": len(p.get("base", {}))}
                           for pid, p in store.state["peers"].items()}}

    def peer_table(self) -> dict:
        with self.lock:
            visible = [{"id": pid, "name": p["name"], "profile": p["profile"], "addr": p["addr"],
                        "machine": p.get("machine", ""), "api_port": p["api_port"],
                        "paired": pid in self.store.state["peers"]}
                       for pid, p in self.peers.items()]
        pending = [{"id": p["peer_id"], "name": p.get("peer_name"), "profile": p.get("peer_profile"),
                    "pair_id": p.get("pair_id"), "kind": p.get("kind", "code"),
                    "code": p.get("code"), "initiated": bool(p.get("initiated"))}
                   for p in self.store.state["pending"].values() if not p.get("accepted")]
        return {"peers": visible, "pending": pending}

    def clear_requests(self) -> dict:
        """Drop every request still waiting for an answer («Очистить» in the UIs).

        Denying one by one is fine for a decision, but a request from a machine
        that never came back is just litter: it holds a row forever and there is
        nothing to decide. Accepted entries stay — they are the record of a
        pairing that happened.
        """
        cleared = 0
        for pair_id, item in list(self.store.state["pending"].items()):
            if item.get("accepted"):
                continue
            self.store.state["pending"].pop(pair_id, None)
            cleared += 1
        if cleared:
            self.store.save()
            log(f"cleared {cleared} pending pairing request(s)")
        return {"cleared": cleared}

    # discovery control (manual mode) --------------------------------------
    # diagnostics ----------------------------------------------------------
    def _record_log(self, level: str, message: str, hint: str) -> None:
        entry = {"ts": time.time(), "level": level, "message": message, "hint": hint}
        self.log_buffer.append(entry)
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            if self.log_file.exists() and self.log_file.stat().st_size > 2_000_000:
                self.log_file.replace(self.log_file.with_name("local-sync.1.jsonl"))
            with open(self.log_file, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def logs(self, limit: int = 200) -> dict:
        return {"entries": list(self.log_buffer)[-max(1, limit):],
                "file": str(self.log_file), "total": len(self.log_buffer)}

    def ping(self, needle: str) -> dict:
        """Reconnect check: is the peer reachable right now, and how fast."""
        peer_id = None
        try:
            peer_id = self.resolve(needle, wait=1.0)
        except AuthError:
            peer_id = None
        record = None
        if peer_id:
            record = self.peers.get(peer_id)
        else:
            for known_id, info in self.store.state["peers"].items():
                if needle in (known_id, known_id[:8], info.get("name"), f"{info.get('name')}/{info.get('profile')}"):
                    peer_id = known_id
                    record = {"id": known_id, **info}
                    break
        if not record or not record.get("addr"):
            raise AuthError(f"устройство {needle} не найдено в сети и нет сохранённого адреса")
        started = time.time()
        stored_key = (self.store.state["peers"].get(peer_id) or {}).get("key")
        auth_ok = None
        if stored_key:
            auth_ok = key_works(record["addr"], record["api_port"], peer_id or "", stored_key, timeout=3)
        request = urllib.request.Request(f"http://{record['addr']}:{record['api_port']}/hello", method="GET")
        with urllib.request.urlopen(request, timeout=3) as response:
            identity = json.loads(response.read())
        elapsed = round((time.time() - started) * 1000)
        repaired = False
        stored = self.store.state["peers"].get(peer_id)
        if stored and (stored.get("addr") != record.get("addr") or stored.get("api_port") != record.get("api_port")):
            stored["addr"] = record.get("addr")
            stored["api_port"] = record.get("api_port")
            stored["beacon_port"] = record.get("beacon_port") or stored.get("beacon_port")
            self.store.save()
            repaired = True
            log(f"reconnected {stored.get('name')} at {record['addr']}:{record['api_port']}", "warn")
        result = {"ok": True, "ms": elapsed, "peer": identity.get("name", record.get("name")),
                  "profile": identity.get("profile"), "addr": record.get("addr"),
                  "api_port": record.get("api_port"), "repaired": repaired}
        if auth_ok is False:
            result["auth"] = False
            result["hint"] = ("устройство отвечает, но не принимает ключ пары — вероятно, на нём "
                              "запущено несколько демонов или пара устарела: «Отключить» и спарь заново")
        elif auth_ok is True:
            result["auth"] = True
        return result

    def unpair(self, needle: str) -> dict:
        peer_id = None
        name = needle
        for known_id, info in self.store.state["peers"].items():
            if needle in (known_id, known_id[:8], info.get("name"), f"{info.get('name')}/{info.get('profile')}"):
                peer_id, name = known_id, info.get("name", needle)
                break
        if not peer_id:
            raise AuthError(f"{needle} не спарен с этой машиной")
        self.store.state["peers"].pop(peer_id, None)
        for pair_id, item in list(self.store.state["pending"].items()):
            if item.get("peer_id") == peer_id:
                self.store.state["pending"].pop(pair_id, None)
        self.store.save()
        log(f"unpaired {name} — спаривание и ключ удалены", "warn")
        return {"removed": True, "peer": name}

    def peer_profiles(self, peer_id: str) -> dict:
        client = PeerClient(self.store, peer_id, find_addr(self, peer_id))
        answer = client.profiles()
        answer["peer"] = self.peers[peer_id]["name"]
        return answer

    def import_profile(self, peer_id: str, remote_profile: str, local_name: str | None = None,
                       mode: str = "merge") -> dict:
        """Copy a profile from the peer.

        ``mode`` decides what happens when the name already exists locally:
        ``merge`` keeps local files and only fills the gaps, ``rebase`` replaces
        local files with the peer's (after a backup), ``rename`` imports into a
        new name instead.
        """
        if mode not in ("merge", "rebase", "rename"):
            raise AuthError(f"неизвестный режим импорта: {mode}")
        client = PeerClient(self.store, peer_id, find_addr(self, peer_id))
        name = (local_name or remote_profile).strip()
        if not name or "/" in name or name.startswith("."):
            raise AuthError("плохое имя профиля")
        root = profile_root(self.store)
        active = Path(self.store.config["profile_dir"]).expanduser().resolve()

        def destination(profile_name: str) -> Path:
            return root if profile_name == "default" else root / "profiles" / profile_name

        dest = destination(name)
        if dest.exists() and mode == "rename":
            name = (local_name or f"{remote_profile}-peer").strip()
            dest = destination(name)
            if dest.exists():
                raise AuthError(f"профиль {name} тоже уже есть — укажи другое имя")
        if name == self.store.config.get("profile") or dest.resolve() == active:
            raise AuthError("это активный профиль — выбери другое имя")

        existing = dest.exists()
        backup_dir = None
        if existing and mode == "rebase":
            backup_dir = (self.store.home / "backups" / time.strftime("%Y%m%d-%H%M%S")
                          / "profiles" / name)
        files = client.manifest(profile=remote_profile)
        created = skipped = replaced = 0
        for rel in files:
            target = safe_target(dest, rel)
            if target.exists():
                if existing and mode == "merge":
                    skipped += 1
                    continue
                if existing and mode == "rebase":
                    atomic_write(backup_dir / rel, target.read_bytes())
                    replaced += 1
            atomic_write(target, client.fetch(rel, profile=remote_profile))
            created += 1
        log(f"imported profile {remote_profile} -> {name} ({mode}): {created} files, {skipped} kept")
        return {"profile": name, "path": str(dest), "mode": mode, "files": created,
                "skipped": skipped, "replaced": replaced,
                "backup": str(backup_dir) if backup_dir else None}

    def copy_sessions(self, peer_id: str) -> dict:
        client = PeerClient(self.store, peer_id, find_addr(self, peer_id))
        payload = client.sessions_snapshot()
        result = import_sessions(sessions_db_path(self.store), payload)
        result["peer"] = self.peers[peer_id]["name"]
        log(f"sessions from {result['peer']}: +{result['sessions']} chats, +{result['messages']} messages")
        return result

    def set_search(self, enabled: bool) -> dict:
        self.store.config["search"] = bool(enabled)
        self.store.save()
        if enabled:
            self.beacon_now.set()
        log(f"discovery {'on' if enabled else 'off'}")
        return {"search": bool(enabled)}

    def search_enabled(self) -> bool:
        return bool(self.store.config.get("search", True))

    # pairing helpers ------------------------------------------------------
    def pair_psk_begin(self, peer_id: str, password: str) -> dict:
        """Initiator side: send the request, keep the derived key in memory only."""
        if len(password) < 4:
            raise AuthError("пароль короче 4 символов")
        addr = find_addr(self, peer_id)
        info = self.peers[peer_id]
        pair_id = uuid.uuid4().hex
        nonce = secrets.token_hex(16)
        payload = {**self.store.me, "pair_id": pair_id, "nonce": nonce}
        req = urllib.request.Request(f"http://{addr}:{info['api_port']}/pair/psk",
                                     data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            json.loads(resp.read())
        key = psk_key(password, pair_id)
        # A second attempt to the same peer replaces the first: the peer's own
        # store keeps only the newest pair_id, so the older nonce can never be
        # confirmed and would just sit here as a dead entry.
        for stale in [pid for pid, item in self.psk_pending.items() if item.get("peer_id") == peer_id]:
            self.psk_pending.pop(stale, None)
        self.psk_pending[pair_id] = {"peer_id": peer_id, "peer_name": info["name"],
                                     "key": key, "nonce": nonce, "created": time.time()}
        return {"pair_id": pair_id, "code": psk_code(key), "peer": info["name"],
                "status": "waiting for the other side to enter the same password"}

    def pair_auto(self, peer_id: str) -> dict:
        """Initiator side: generate the code here, the other machine types it in."""
        code = f"{secrets.randbelow(900_000) + 100_000:06d}"
        result = self.pair_psk_begin(peer_id, code)
        return {**result, "code": code, "sas": result.get("code"), "generated": True,
                "hint": f"введи код {code} на {result.get('peer', 'другой машине')}"}

    def pair_psk_accept(self, peer_id: str, password: str) -> dict:
        """Responder side: confirm with the shared password, then verify the ack."""
        if len(password) < 4:
            raise AuthError("пароль короче 4 символов")
        pending = None
        for pair in self.store.state["pending"].values():
            if pair.get("peer_id") == peer_id and pair.get("kind") == "psk" and not pair.get("accepted"):
                pending = pair
                break
        if not pending:
            raise AuthError("no pending pairing request from that peer")
        addr = (self.peers.get(peer_id) or {}).get("addr")
        if not addr:
            raise AuthError("peer is not visible on the network right now")
        pair_id = pending["pair_id"]
        key = psk_key(password, pair_id)
        nonce_b = secrets.token_hex(16)
        tag = hmac_tag(key, f"confirm:{pair_id}:{pending['a_nonce']}:{nonce_b}")
        payload = {"pair_id": pair_id, "b_nonce": nonce_b, "tag": tag, **self.store.me}
        req = urllib.request.Request(f"http://{addr}:{pending['peer_api_port']}/pair/psk/confirm",
                                     data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                answer = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            if exc.code == 403:
                raise AuthError("пароли не совпали — на другой машине введён другой пароль") from exc
            raise AuthError(f"peer refused the pairing: {detail[:200]}") from exc
        expected = hmac_tag(key, f"ack:{pair_id}:{nonce_b}")
        if not hmac.compare_digest(answer.get("tag", ""), expected):
            raise AuthError("peer could not confirm the key")
        self.store.state["peers"][peer_id] = {
            "name": pending["peer_name"], "profile": pending["peer_profile"],
            "api_port": pending["peer_api_port"], "beacon_port": pending.get("peer_beacon_port") or 0,
            "addr": addr, "key": key, "paired_at": time.time(), "base": {},
        }
        pending["accepted"] = True
        self.store.save()
        log(f"paired with {pending['peer_name']} (password)")
        self._emit({"type": "paired", "peer": peer_id})
        return {"peer": pending["peer_name"], "code": psk_code(key), "ok": True}

    def pair_begin(self, peer_id: str) -> dict:
        addr = find_addr(self, peer_id)
        payload = {**self.store.me, "nonce": secrets.token_hex(16)}
        req = urllib.request.Request(f"http://{addr}:{self.peers[peer_id]['api_port']}/pair/begin",
                                     data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            answer = json.loads(resp.read())
        self.store.state["pending"][answer["pair_id"]] = {
            "pair_id": answer["pair_id"],
            "peer_id": peer_id, "peer_name": self.peers[peer_id]["name"],
            "peer_profile": self.peers[peer_id]["profile"],
            "peer_api_port": self.peers[peer_id]["api_port"],
            "nonce": payload["nonce"], "code": answer["code"],
            "initiated": True, "created": time.time(), "accepted": False,
        }
        supersede_pending(self.store, peer_id, keep=answer["pair_id"])
        self.store.save()
        return {"pair_id": answer["pair_id"], "code": answer["code"], "peer": peer_id}

    def pair_accept(self, peer_id: str, code: str | None = None) -> dict:
        pending = None
        for pair in self.store.state["pending"].values():
            if pair.get("peer_id") == peer_id and not pair.get("accepted") and not pair.get("initiated"):
                pending = pair
                break
        if not pending:
            raise AuthError("no pending request from that peer")
        if code and code != pending["code"]:
            raise AuthError("code mismatch")
        b_nonce = secrets.token_hex(16)
        pair_id = pending["pair_id"]
        key = sha_bytes(f"{pending['a_nonce']}:{b_nonce}:{peer_id}:{self.store.me['id']}".encode())
        sig = hmac.new(bytes.fromhex(key), f"complete:{pair_id}".encode(),
                       hashlib.sha256).hexdigest()
        info = self.peers.get(peer_id) or {"addr": None}
        addr = info.get("addr")
        if not addr:
            raise AuthError("peer is not visible on the network right now")
        payload = {"pair_id": pair_id, "b_nonce": b_nonce, "sig": sig, **self.store.me}
        req = urllib.request.Request(f"http://{addr}:{pending['peer_api_port']}/pair/complete",
                                     data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            json.loads(resp.read())
        now = time.time()
        self.store.state["peers"][peer_id] = {
            "name": pending["peer_name"], "profile": pending["peer_profile"],
            "api_port": pending["peer_api_port"], "key": key, "paired_at": now, "base": {},
            "beacon_port": pending.get("peer_beacon_port") or 0, "addr": addr,
        }
        pending["accepted"] = True
        self.store.save()
        log(f"paired with {pending['peer_name']}")
        self._emit({"type": "paired", "peer": peer_id})
        return {"peer": pending["peer_name"], "ok": True}


def pending_id(pair: dict) -> str:
    return pair.get("pair_id") or ""


# ----------------------------------------------------------------------------- CLI


def cmd_init(args) -> int:
    store = Store(Path(args.home))
    log(f"config: {store.home / 'config.json'}")
    print(json.dumps(store.config, indent=2))
    return 0


def _daemon_from_args(args) -> tuple[Store, SyncDaemon]:
    store = Store(Path(args.home))
    if args.name:
        store.config["name"] = args.name
    if args.profile:
        store.config["profile"] = args.profile
    if args.profile_dir:
        store.config["profile_dir"] = args.profile_dir
    store.save()
    daemon = SyncDaemon(store, bind=args.bind, api_port=args.api_port, beacon_port=args.beacon_port)
    for target in args.beacon_target or []:
        host, _, port = target.rpartition(":")
        daemon.beacon_targets.append((host or target, int(port or daemon.beacon_port)))
    return store, daemon


def cmd_run(args) -> int:
    store, daemon = _daemon_from_args(args)
    daemon.start()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        daemon.stop()
    return 0


def spawn_daemon(store: Store) -> subprocess.Popen | None:
    """Start ``python -m local_sync run`` detached, logging next to the state dir."""
    package_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(package_root), env.get("PYTHONPATH", "")]))
    env["LOCAL_SYNC_HOME"] = str(store.home)
    log_path = store.home / "daemon.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log_file:
        return subprocess.Popen(
            [sys.executable, "-m", "local_sync", "run"],
            cwd=str(package_root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )


def ensure_daemon(store: Store, timeout: float = 20.0) -> dict | None:
    """Return the daemon status, starting the daemon if it is not up yet."""
    answer = ctl(store, "status")
    if answer and answer.get("ok"):
        return answer["result"]
    if not store.config.get("auto_start", True):
        return None
    spawn_daemon(store)
    deadline = time.time() + timeout
    while time.time() < deadline:
        answer = ctl(store, "status")
        if answer and answer.get("ok"):
            return answer["result"]
        time.sleep(0.3)
    return None


def ctl(store: Store, op: str, **kwargs) -> dict | None:
    """Talk to the daemon running on this machine. None if nothing answers.

    Reads the token and port from disk every call: the daemon writes both when
    it starts, which is often *after* this process opened its Store.
    """
    home = store.home
    try:
        config = json.loads((home / "config.json").read_text())
        state = json.loads((home / "state.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    token = state.get("ctl_token")
    if not token:
        return None
    url = f"http://127.0.0.1:{config['api_port']}/ctl"
    body = json.dumps({"op": op, "args": kwargs}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"X-Hermes-Sync-Ctl": token,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
        return None


def _print(payload) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def cmd_peers(args) -> int:
    store = Store(Path(args.home))
    ensure_daemon(store)
    answer = ctl(store, "peers")
    if answer and answer.get("ok"):
        _print(answer["result"])
        return 0
    rows = []
    for pid, info in store.state["peers"].items():
        rows.append({"id": pid[:8], "name": info.get("name"), "profile": info.get("profile"),
                     "paired": True, "addr": info.get("addr"), "api_port": info.get("api_port")})
    for pair in store.state["pending"].values():
        if not pair.get("accepted"):
            rows.append({"id": pair["peer_id"][:8], "name": pair.get("peer_name"),
                         "profile": pair.get("peer_profile"), "pending": True, "code": pair.get("code")})
    _print({"peers": rows, "note": "daemon is not running — showing saved state only"})
    return 0


def cmd_status(args) -> int:
    store = Store(Path(args.home))
    ensure_daemon(store)
    answer = ctl(store, "status")
    if answer and answer.get("ok"):
        _print(answer["result"])
        return 0
    _print({"me": store.me, "profile_dir": store.config["profile_dir"], "roots": store.config["roots"],
            "paired": {i: {"name": p.get("name"), "last_sync": p.get("last_sync"),
                           "base_files": len(p.get("base", {}))}
                       for i, p in store.state["peers"].items()},
            "note": "daemon is not running — showing saved state only"})
    return 0


def cmd_pair(args) -> int:
    store = Store(Path(args.home))
    if getattr(args, "password", None):
        answer = ctl(store, "pair_psk_begin", peer=args.peer, password=args.password)
    else:
        answer = ctl(store, "pair_begin", peer=args.peer)
    if answer is not None:
        if not answer.get("ok"):
            log(answer.get("error") or "pairing failed")
            return 1
        result = answer["result"]
        print(f"code: {result['code']}  (confirm on the other machine: `local-sync accept {args.peer}`)")
        deadline = time.time() + (args.wait or 120)
        while time.time() < deadline:
            table = ctl(store, "peers") or {}
            paired = {p["id"] for p in (table.get("result") or {}).get("peers", []) if p.get("paired")}
            if result["peer"] in paired:
                _print({"paired": True, "peer": result["peer"]})
                return 0
            time.sleep(1.0)
        log("timed out waiting for confirmation")
        return 1
    # no daemon: run a throwaway one
    store, daemon = _daemon_from_args(args)
    daemon.start()
    deadline = time.time() + (args.wait or 15)
    peer_id = _resolve_peer(store, daemon, args.peer, deadline)
    if not peer_id:
        log(f"peer {args.peer!r} is not visible on the LAN")
        daemon.stop()
        return 2
    result = daemon.pair_begin(peer_id)
    print(f"code: {result['code']}")
    deadline = time.time() + (args.wait or 120)
    while time.time() < deadline and peer_id not in store.state["peers"]:
        time.sleep(0.5)
    ok = peer_id in store.state["peers"]
    daemon.stop()
    _print({"paired": ok, "peer": args.peer})
    return 0 if ok else 1


def cmd_accept(args) -> int:
    store = Store(Path(args.home))
    if getattr(args, "password", None):
        answer = ctl(store, "pair_psk_accept", peer=args.peer, password=args.password)
    else:
        answer = ctl(store, "pair_accept", peer=args.peer, code=args.code)
    if answer is not None:
        if not answer.get("ok"):
            log(answer.get("error") or "accept failed")
            return 1
        _print(answer["result"])
        return 0
    store, daemon = _daemon_from_args(args)
    daemon.start()
    peer_id = _resolve_peer(store, daemon, args.peer, time.time() + (args.wait or 10))
    if not peer_id:
        log("peer not visible on the LAN")
        daemon.stop()
        return 2
    try:
        result = daemon.pair_accept(peer_id, args.code)
    except AuthError as exc:
        log(str(exc))
        daemon.stop()
        return 1
    daemon.stop()
    _print(result)
    return 0


def cmd_plan(args) -> int:
    store = Store(Path(args.home))
    answer = ctl(store, "plan", peer=args.peer)
    if answer is not None:
        if not answer.get("ok"):
            log(answer.get("error") or "plan failed")
            return 1
        _print(answer["result"]["plan"])
        return 0
    store, daemon = _daemon_from_args(args)
    daemon.start()
    peer_id = _resolve_peer(store, daemon, args.peer, time.time() + (args.wait or 10))
    if not peer_id:
        log("peer not visible on the LAN")
        daemon.stop()
        return 2
    actions = make_plan(store, daemon, peer_id)
    daemon.stop()
    _print(actions)
    return 0


def cmd_sync(args) -> int:
    store = Store(Path(args.home))
    op = "plan" if args.dry_run else "sync"
    answer = ctl(store, op, peer=args.peer)
    if answer is not None:
        if not answer.get("ok"):
            log(answer.get("error") or "sync failed")
            return 1
        _print(answer["result"]["plan"] if args.dry_run else answer["result"])
        return 0
    store, daemon = _daemon_from_args(args)
    daemon.start()
    peer_id = _resolve_peer(store, daemon, args.peer, time.time() + (args.wait or 10))
    if not peer_id:
        log("peer not visible on the LAN")
        daemon.stop()
        return 2
    actions = make_plan(store, daemon, peer_id)
    result = actions if args.dry_run else apply_plan(store, daemon, peer_id, actions)
    daemon.stop()
    _print(result)
    return 0


def _resolve_peer(store: Store, daemon: SyncDaemon, needle: str, deadline: float) -> str | None:
    """Match a peer by id, short id, name or name/profile among peers seen on the LAN.

    When several instances of the same machine answer (two daemons, same host), the
    paired one wins — following the other instance is a guaranteed 401.
    """
    while time.time() < deadline:
        with daemon.lock:
            fallback = None
            for pid, info in daemon.peers.items():
                names = {pid, pid[:8], str(info.get("name")),
                         f"{info.get('name')}/{info.get('profile')}"}
                if needle not in names:
                    continue
                if pid in store.state["peers"]:
                    return pid
                fallback = fallback or pid
            if fallback:
                return fallback
        time.sleep(0.3)
    return None


def cmd_mcp(args) -> int:
    """MCP stdio server: exposes the sync daemon's operations as agent tools.

    If a daemon already runs on this machine, proxy to it (one discoverer per
    host); otherwise start an embedded one for the lifetime of the server.
    """
    store = Store(Path(args.home))
    daemon: SyncDaemon | None = None
    probe = ctl(store, "status")
    if not (probe and probe.get("ok")):
        store, daemon = _daemon_from_args(args)
        daemon.start()
    tools = [
        {"name": "sync_status", "description": "Local identity, paired peers and sync state.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "peers_list", "description": "Peers visible on the LAN and whether they are paired.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "pair_begin", "description": "Ask a discovered peer to pair; returns the 6-digit code shown on both sides.",
         "inputSchema": {"type": "object", "required": ["peer"], "properties": {"peer": {"type": "string"}}}},
        {"name": "pair_accept", "description": "Confirm an incoming pairing request (optionally checking the code).",
         "inputSchema": {"type": "object", "required": ["peer"], "properties": {
             "peer": {"type": "string"}, "code": {"type": "string"}}}},
        {"name": "sync_plan", "description": "Dry-run: list per-file pull/push/conflict actions against a peer.",
         "inputSchema": {"type": "object", "required": ["peer"], "properties": {"peer": {"type": "string"}}}},
        {"name": "sync_apply", "description": "Apply the plan: pull/push changed files, merge memories, keep conflict sidecars.",
         "inputSchema": {"type": "object", "required": ["peer"], "properties": {"peer": {"type": "string"}}}},
    ]

    REMOTE_OPS = {"sync_status": "status", "peers_list": "peers", "pair_begin": "pair_begin",
                  "pair_accept": "pair_accept", "sync_plan": "plan", "sync_apply": "sync"}

    def call(name: str, args_in: dict) -> dict:
        if daemon is None:  # proxy to the daemon already running on this host
            answer = ctl(store, REMOTE_OPS[name], **args_in)
            if answer is None:
                raise AuthError("daemon went away")
            if not answer.get("ok"):
                raise AuthError(answer.get("error") or "failed")
            return answer["result"]

        def resolve(needle: str, wait: float = 3.0) -> str:
            peer_id = _resolve_peer(store, daemon, needle, time.time() + wait)
            if not peer_id:
                raise AuthError(f"peer {needle!r} is not visible on the LAN")
            return peer_id

        if name == "sync_status":
            return daemon.status()
        if name == "peers_list":
            return daemon.peer_table()
        if name == "pair_begin":
            return daemon.pair_begin(resolve(args_in["peer"]))
        if name == "pair_accept":
            return daemon.pair_accept(resolve(args_in["peer"]), args_in.get("code"))
        if name == "sync_plan":
            return {"plan": make_plan(store, daemon, resolve(args_in["peer"]))}
        if name == "sync_apply":
            peer_id = resolve(args_in["peer"])
            return apply_plan(store, daemon, peer_id, make_plan(store, daemon, peer_id))
        raise AuthError(f"unknown tool {name}")

    def reply(msg_id, result=None, error=None) -> None:
        payload = {"jsonrpc": "2.0", "id": msg_id}
        if error is not None:
            payload["error"] = {"code": -32603, "message": str(error)}
        else:
            payload["result"] = result
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, msg_id = msg.get("method"), msg.get("id")
        if method == "initialize":
            reply(msg_id, {"protocolVersion": "2024-11-05",
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": SERVICE, "version": VERSION}})
        elif method == "tools/list":
            reply(msg_id, {"tools": tools})
        elif method == "tools/call":
            params = msg.get("params") or {}
            try:
                result = call(params.get("name", ""), params.get("arguments") or {})
                reply(msg_id, {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]})
            except Exception as exc:  # report, never kill the server
                reply(msg_id, {"content": [{"type": "text", "text": f"error: {exc}"}], "isError": True})
        elif method == "ping":
            reply(msg_id, {})
        elif msg_id is not None:
            reply(msg_id, None, error=f"unsupported method {method}")
    if daemon is not None:
        daemon.stop()
    return 0


def cmd_ping(args) -> int:
    store = Store(Path(args.home))
    ensure_daemon(store)
    answer = ctl(store, "ping", peer=args.peer)
    _print(answer.get("result") if answer and answer.get("ok") else {"ok": False, "error": (answer or {}).get("error")})
    return 0 if answer and answer.get("ok") else 1


def cmd_unpair(args) -> int:
    store = Store(Path(args.home))
    ensure_daemon(store)
    answer = ctl(store, "unpair", peer=args.peer)
    _print(answer.get("result") if answer and answer.get("ok") else {"ok": False, "error": (answer or {}).get("error")})
    return 0 if answer and answer.get("ok") else 1


def cmd_logs(args) -> int:
    store = Store(Path(args.home))
    ensure_daemon(store)
    answer = ctl(store, "logs", limit=args.tail)
    if not (answer and answer.get("ok")):
        _print({"ok": False, "error": (answer or {}).get("error")})
        return 1
    for entry in answer["result"]["entries"]:
        stamp = time.strftime("%H:%M:%S", time.localtime(entry["ts"]))
        level = entry.get("level", "info")
        line = f"{stamp} {level:<5} {entry['message']}"
        if entry.get("hint"):
            line += f"\n            ↳ {entry['hint']}"
        print(line)
    print(f"\nфайл: {answer['result']['file']}")
    return 0


def cmd_search(args) -> int:
    store = Store(Path(args.home))
    ensure_daemon(store)          # discovery needs the daemon; start it if it is down
    if args.state is None:
        answer = ctl(store, "status")
        if answer and answer.get("ok"):
            _print({"search": answer["result"].get("search")})
            return 0
        _print({"search": store.config.get("search", True), "note": "daemon is not running"})
        return 0
    enabled = args.state == "on"
    answer = ctl(store, "search", enabled=enabled)
    if answer is None:
        store.config["search"] = enabled
        store.save()
        _print({"search": enabled, "note": "saved for the next daemon start"})
        return 0
    if not answer.get("ok"):
        log(answer.get("error") or "failed")
        return 1
    _print(answer["result"])
    return 0


def cmd_ui(args) -> int:
    from .ui import run_ui

    store = Store(Path(args.home))
    if args.name:
        store.config["name"] = args.name
    if args.profile_dir:
        store.config["profile_dir"] = args.profile_dir
    store.save()
    return run_ui(store, port=args.ui_port, open_browser=not args.no_open, wait=args.wait or 20.0)


def build_parser() -> argparse.ArgumentParser:
    common = dict()
    parser = argparse.ArgumentParser(prog="local-sync", description=__doc__)
    parser.add_argument("--home", default=os.environ.get("LOCAL_SYNC_HOME", "~/.local-sync"))
    parser.add_argument("--name")
    parser.add_argument("--profile")
    parser.add_argument("--profile-dir")
    parser.add_argument("--api-port", type=int)
    parser.add_argument("--beacon-port", type=int)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--beacon-target", action="append", help="host:port override (repeatable)")
    parser.add_argument("--wait", type=float, default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create config.json")
    sub.add_parser("run", help="run discovery + API until Ctrl-C")
    sub.add_parser("mcp", help="MCP stdio server exposing sync tools")
    ui = sub.add_parser("ui", help="local web interface: discovery switch, pairing, sync")
    ui.add_argument("--ui-port", type=int, default=45873)
    ui.add_argument("--no-open", action="store_true")
    sub.add_parser("peers", help="show paired and pending peers")
    sub.add_parser("status", help="show local identity and sync state")
    search = sub.add_parser("search", help="turn LAN discovery on/off (manual mode)")
    search.add_argument("state", nargs="?", choices=["on", "off"])
    ping = sub.add_parser("ping", help="check a peer is reachable and how fast")
    ping.add_argument("peer")
    unpair = sub.add_parser("unpair", help="forget a paired device and its key")
    unpair.add_argument("peer")
    logs = sub.add_parser("logs", help="show recent daemon log entries")
    logs.add_argument("--tail", type=int, default=60)
    p = sub.add_parser("pair", help="initiate pairing with a discovered peer")
    p.add_argument("peer")
    p.add_argument("--password", help="use the shared-password handshake instead of a code")
    p = sub.add_parser("accept", help="confirm an incoming pairing request")
    p.add_argument("peer")
    p.add_argument("--code")
    p.add_argument("--password", help="answer the shared-password handshake")
    for name, text in (("plan", "show the diff against a peer"), ("sync", "apply the diff")):
        p = sub.add_parser(name, help=text)
        p.add_argument("peer")
        if name == "sync":
            p.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {"init": cmd_init, "run": cmd_run, "mcp": cmd_mcp, "ui": cmd_ui,
                "peers": cmd_peers, "status": cmd_status, "search": cmd_search,
                "ping": cmd_ping, "unpair": cmd_unpair, "logs": cmd_logs,
                "pair": cmd_pair, "accept": cmd_accept, "plan": cmd_plan, "sync": cmd_sync}
    return handlers[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
