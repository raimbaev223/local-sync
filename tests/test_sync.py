#!/usr/bin/env python3
"""End-to-end tests: two daemons on loopback discover, pair and sync."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_sync import core as hs


def wait_for(predicate, timeout=8.0, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class PairTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hs-test-"))
        self.nodes = {}
        for name in ("mac", "pc"):
            profile = self.tmp / f"profile-{name}"
            (profile / "skills" / "user-skill").mkdir(parents=True)
            (profile / "memories").mkdir(parents=True)
            store = hs.Store(self.tmp / name)
            store.config.update({
                "name": name,
                "profile": "algo",
                "profile_dir": str(profile),
                "roots": ["skills", "memories", "SOUL.md"],
                "broadcast": [],
                "beacon_interval": 0.2,
                "same_host": True,      # tests run two peers on one loopback host
            })
            store.save()
            self.nodes[name] = store
        self.a = hs.SyncDaemon(self.nodes["mac"], bind="127.0.0.1", api_port=0, beacon_port=0)
        self.b = hs.SyncDaemon(self.nodes["pc"], bind="127.0.0.1", api_port=0, beacon_port=0)
        self.a.beacon_targets = [("127.0.0.1", self.b.beacon_port)]
        self.b.beacon_targets = [("127.0.0.1", self.a.beacon_port)]
        self.a.start()
        self.b.start()
        self.a_id = self.nodes["mac"].config["id"]
        self.b_id = self.nodes["pc"].config["id"]
        self.assertTrue(wait_for(lambda: self.b_id in self.a.peers and self.a_id in self.b.peers),
                        "peers did not discover each other")

    def tearDown(self):
        self.a.stop()
        self.b.stop()

    # ------------------------------------------------------------------
    def pair(self):
        answer = self.a.pair_begin(self.b_id)
        self.b.pair_accept(self.a_id, answer["code"])
        self.assertTrue(wait_for(lambda: self.b_id in self.nodes["mac"].state["peers"]))
        return answer

    def test_discovery(self):
        self.assertEqual(self.a.peers[self.b_id]["name"], "pc")
        self.assertEqual(self.b.peers[self.a_id]["profile"], "algo")

    def test_pairing_derives_same_key(self):
        self.pair()
        key_a = self.nodes["mac"].state["peers"][self.b_id]["key"]
        key_b = self.nodes["pc"].state["peers"][self.a_id]["key"]
        self.assertEqual(key_a, key_b)
        self.assertEqual(len(bytes.fromhex(key_a)), 32)

    def test_wrong_code_is_rejected(self):
        answer = self.a.pair_begin(self.b_id)
        wrong = "000000" if answer["code"] != "000000" else "111111"
        with self.assertRaises(hs.AuthError):
            self.b.pair_accept(self.a_id, wrong)

    def test_unauthenticated_manifest_is_401(self):
        import urllib.error
        import urllib.request
        url = f"http://127.0.0.1:{self.b.api_port}/manifest"
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(url, timeout=5)
        self.assertEqual(ctx.exception.code, 401)

    def test_tampered_signature_is_401(self):
        self.pair()
        client = hs.PeerClient(self.nodes["mac"], self.b_id, self.a.peers[self.b_id]["addr"])
        client.key = bytes(32)  # wrong key
        with self.assertRaises(hs.AuthError):
            client.manifest()

    def test_path_traversal_is_rejected(self):
        self.pair()
        client = hs.PeerClient(self.nodes["mac"], self.b_id, self.a.peers[self.b_id]["addr"])
        with self.assertRaises(hs.AuthError):
            client.fetch("../../../../etc/passwd")

    # ------------------------------------------------------------------
    def test_pull_then_push(self):
        self.pair()
        skill = Path(self.nodes["mac"].config["profile_dir"]) / "skills/user-skill/SKILL.md"
        skill.write_text("---\nname: user-skill\n---\nhello\n")

        actions = hs.make_plan(self.nodes["mac"], self.a, self.b_id)
        self.assertEqual([a["action"] for a in actions], ["push"])
        hs.apply_plan(self.nodes["mac"], self.a, self.b_id, actions)

        remote_copy = Path(self.nodes["pc"].config["profile_dir"]) / "skills/user-skill/SKILL.md"
        self.assertTrue(wait_for(lambda: remote_copy.exists()))
        self.assertEqual(remote_copy.read_text(), skill.read_text())

        # change it on the other side, pull back
        remote_copy.write_text("---\nname: user-skill\n---\nchanged on pc\n")
        actions = hs.make_plan(self.nodes["pc"], self.b, self.a_id)
        self.assertEqual([a["action"] for a in actions], ["push"])
        hs.apply_plan(self.nodes["pc"], self.b, self.a_id, actions)
        self.assertEqual(skill.read_text(), "---\nname: user-skill\n---\nchanged on pc\n")

    def test_bundled_skills_are_not_synced(self):
        profile = Path(self.nodes["mac"].config["profile_dir"])
        (profile / "skills/.bundled_manifest").write_text("bundled-skill:abc123\n")
        (profile / "skills/bundled-skill").mkdir()
        (profile / "skills/bundled-skill/SKILL.md").write_text("upstream\n")
        manifest = hs.build_manifest(self.nodes["mac"])
        self.assertNotIn("skills/bundled-skill/SKILL.md", manifest)

    def test_conflict_keeps_local_and_writes_sidecar(self):
        self.pair()
        rel = "skills/user-skill/SKILL.md"
        local = Path(self.nodes["mac"].config["profile_dir"]) / rel
        other = Path(self.nodes["pc"].config["profile_dir"]) / rel
        local.write_text("local edit\n")
        actions = hs.make_plan(self.nodes["mac"], self.a, self.b_id)
        hs.apply_plan(self.nodes["mac"], self.a, self.b_id, actions)

        # both sides edit the same file without syncing
        local.write_text("local edit v2\n")
        other.write_text("remote edit v2\n")
        actions = hs.make_plan(self.nodes["mac"], self.a, self.b_id)
        self.assertEqual([a["action"] for a in actions], ["conflict"])
        result = hs.apply_plan(self.nodes["mac"], self.a, self.b_id, actions)

        self.assertEqual(local.read_text(), "local edit v2\n")
        sidecars = list(local.parent.glob("SKILL.md.conflict-pc-*"))
        self.assertEqual(len(sidecars), 1)
        self.assertEqual(sidecars[0].read_text(), "remote edit v2\n")
        self.assertEqual(result["stats"]["conflicts"], 1)
        # local wins: the next sync pushes it and the pair converges
        actions = hs.make_plan(self.nodes["mac"], self.a, self.b_id)
        self.assertEqual([a["action"] for a in actions], ["push"])
        hs.apply_plan(self.nodes["mac"], self.a, self.b_id, actions)
        self.assertEqual(other.read_text(), "local edit v2\n")
        self.assertEqual([a["action"] for a in hs.make_plan(self.nodes["mac"], self.a, self.b_id)], ["same"])

    def test_memories_are_union_merged(self):
        self.pair()
        rel = "memories/USER.md"
        local = Path(self.nodes["mac"].config["profile_dir"]) / rel
        other = Path(self.nodes["pc"].config["profile_dir"]) / rel
        local.write_text("mac fact\n")
        actions = hs.make_plan(self.nodes["mac"], self.a, self.b_id)
        hs.apply_plan(self.nodes["mac"], self.a, self.b_id, actions)

        local.write_text("mac fact\n§\nshared fact A\n")
        other.write_text("mac fact\n§\npc fact B\n")
        actions = hs.make_plan(self.nodes["mac"], self.a, self.b_id)
        self.assertEqual([a["action"] for a in actions], ["conflict"])
        hs.apply_plan(self.nodes["mac"], self.a, self.b_id, actions)

        merged = local.read_text()
        for fragment in ("mac fact", "shared fact A", "pc fact B"):
            self.assertIn(fragment, merged)
        self.assertTrue(wait_for(lambda: "pc fact B" in other.read_text()))
        # no duplicates after a second round
        actions = hs.make_plan(self.nodes["mac"], self.a, self.b_id)
        self.assertEqual([a["action"] for a in actions], ["same"])

    def test_autosync_loop_pushes_without_manual_call(self):
        self.pair()
        hs.apply_plan(self.nodes["mac"], self.a, self.b_id,
                      hs.make_plan(self.nodes["mac"], self.a, self.b_id))
        fresh = Path(self.nodes["mac"].config["profile_dir"]) / "skills/user-skill/skill.md"
        fresh.write_text("written on mac after pairing\n")
        self.a.wake.set()  # same path the timer takes every sync_interval
        target = Path(self.nodes["pc"].config["profile_dir"]) / "skills/user-skill/skill.md"
        self.assertTrue(wait_for(target.exists, timeout=15), "autosync did not push the new file")

    def test_psk_pairing_with_same_password(self):
        begun = self.a.pair_psk_begin(self.b_id, "correct horse battery")
        self.assertEqual(len(begun["code"]), 6)
        accepted = self.b.pair_psk_accept(self.a_id, "correct horse battery")
        self.assertEqual(begun["code"], accepted["code"], "SAS codes must match on both sides")
        key_a = self.nodes["mac"].state["peers"][self.b_id]["key"]
        key_b = self.nodes["pc"].state["peers"][self.a_id]["key"]
        self.assertEqual(key_a, key_b)
        # password never lands in the state file
        blob = (self.nodes["mac"].home / "state.json").read_text()
        self.assertNotIn("correct horse battery", blob)

    def test_numeric_pin_is_enough(self):
        begun = self.a.pair_psk_begin(self.b_id, "4821")
        accepted = self.b.pair_psk_accept(self.a_id, "4821")
        self.assertEqual(begun["code"], accepted["code"])
        self.assertIn(self.b_id, self.nodes["mac"].state["peers"])

    def test_short_pin_is_rejected(self):
        with self.assertRaises(hs.AuthError):
            self.a.pair_psk_begin(self.b_id, "123")

    def test_psk_pairing_with_wrong_password_fails(self):
        self.a.pair_psk_begin(self.b_id, "alpha-alpha")
        with self.assertRaises(hs.AuthError):
            self.b.pair_psk_accept(self.a_id, "beta-beta")
        self.assertNotIn(self.a_id, self.nodes["pc"].state["peers"])

    def test_pair_auto_generates_code_for_the_other_side(self):
        begun = self.a.pair_auto(self.b_id)
        self.assertTrue(begun["generated"])
        self.assertEqual(len(begun["code"]), 6)
        accepted = self.b.pair_psk_accept(self.a_id, begun["code"])
        self.assertEqual(accepted["peer"], "mac")
        self.assertEqual(self.nodes["mac"].state["peers"][self.b_id]["key"],
                         self.nodes["pc"].state["peers"][self.a_id]["key"])

    def test_sessions_copy_is_insert_only(self):
        import sqlite3
        self.pair()
        schema = """
            CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, started_at REAL);
            CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
                                   role TEXT, content TEXT, timestamp REAL, message_uid TEXT);
        """
        for name in ("mac", "pc"):
            db = Path(self.nodes[name].config["profile_dir"]) / "state.db"
            con = sqlite3.connect(db)
            con.executescript(schema)
            con.commit()
            con.close()
        # the peer has a chat and one local has its own
        pc_db = sqlite3.connect(Path(self.nodes["pc"].config["profile_dir"]) / "state.db")
        pc_db.execute("INSERT INTO sessions VALUES ('sess-pc', 'cli', 1.0)")
        pc_db.executemany("INSERT INTO messages (session_id, role, content, timestamp, message_uid) VALUES (?,?,?,?,?)",
                          [("sess-pc", "user", "hi from pc", 1.0, "u1"),
                           ("sess-pc", "assistant", "hello", 1.1, "u2")])
        pc_db.commit(); pc_db.close()
        mac_db_path = Path(self.nodes["mac"].config["profile_dir"]) / "state.db"
        mac_db = sqlite3.connect(mac_db_path)
        mac_db.execute("INSERT INTO sessions VALUES ('sess-mac', 'cli', 2.0)")
        mac_db.commit(); mac_db.close()

        result = self.a.copy_sessions(self.b_id)
        self.assertEqual((result["sessions"], result["messages"]), (1, 2))

        check = sqlite3.connect(mac_db_path)
        ids = {row[0] for row in check.execute("SELECT id FROM sessions")}
        self.assertEqual(ids, {"sess-mac", "sess-pc"})
        self.assertEqual(check.execute("SELECT count(*) FROM messages").fetchone()[0], 2)
        check.close()

        # second pass copies nothing and never duplicates
        self.assertEqual(self.a.copy_sessions(self.b_id)["messages"], 0)

    def test_peer_profiles_and_import(self):
        self.pair()
        skill = Path(self.nodes["pc"].config["profile_dir"]) / "skills/user-skill/SKILL.md"
        skill.write_text("---\nname: user-skill\n---\nfrom pc\n")
        listed = self.a.peer_profiles(self.b_id)
        names = [item["name"] for item in listed["profiles"]]
        self.assertIn("default", names)
        default = next(item for item in listed["profiles"] if item["name"] == "default")
        self.assertGreaterEqual(default["skills"], 1)

        result = self.a.import_profile(self.b_id, "default", "imported")
        self.assertGreaterEqual(result["files"], 1)
        copied = Path(self.nodes["mac"].config["profile_dir"]) / "profiles" / "imported" / "skills/user-skill/SKILL.md"
        self.assertTrue(copied.exists())

        with self.assertRaises(hs.AuthError):
            self.a.import_profile(self.b_id, "default", "imported")
        with self.assertRaises(hs.AuthError):
            self.a.import_profile(self.b_id, "default", "algo")  # active profile is off limits

    def test_search_toggle(self):
        self.assertTrue(self.a.search_enabled())
        self.a.set_search(False)
        self.assertFalse(self.a.store.config["search"])
        reloaded = hs.Store(self.nodes["mac"].home)
        self.assertFalse(reloaded.config["search"])
        self.a.set_search(True)
        self.assertTrue(self.a.search_enabled())

    def test_ui_endpoints_require_token(self):
        import threading, urllib.request, urllib.error
        from local_sync.ui import UiServer

        ui = UiServer(self.nodes["mac"], port=0)
        threading.Thread(target=ui.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{ui.port}"
        try:
            with urllib.request.urlopen(base + "/", timeout=5) as resp:
                html = resp.read().decode()
            self.assertEqual(resp.status, 200)
            self.assertIn("local-sync", html)

            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(base + "/api/status", timeout=5)
            self.assertEqual(ctx.exception.code, 401)

            with urllib.request.urlopen(f"{base}/api/status?t={ui.token}", timeout=5) as resp:
                payload = json.loads(resp.read())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["result"]["me"]["name"], "mac")

            req = urllib.request.Request(f"{base}/api/search?t={ui.token}",
                                         data=json.dumps({"enabled": False}).encode(), method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "X-Local-Sync-Token": ui.token})
            with urllib.request.urlopen(req, timeout=5) as resp:
                payload = json.loads(resp.read())
            self.assertEqual(payload["result"], {"search": False})
        finally:
            ui.stop()

    def test_untouched_files_are_same(self):
        self.pair()
        Path(self.nodes["mac"].config["profile_dir"], "SOUL.md").write_text("soul\n")
        actions = hs.make_plan(self.nodes["mac"], self.a, self.b_id)
        hs.apply_plan(self.nodes["mac"], self.a, self.b_id, actions)
        self.assertEqual([a["action"] for a in hs.make_plan(self.nodes["mac"], self.a, self.b_id)], ["same"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
