---
name: local-sync
description: "Sync a Hermes profile between machines over the LAN: pair, plan, apply."
version: 1.0.0
author: Erkin
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [sync, lan, profiles, skills, memory]
---

# local-sync

Two machines, one profile (`algo` on both), one pile of context. This plugin
moves the portable half between them over the local network and tells you what
it is about to change before it changes it.

## When to use

- The user says the other machine "doesn't have my skills / memory / cron jobs".
- The user asks to sync, pair, or check sync state between a laptop and a desktop.
- A pairing notification appeared and the user wants it accepted.

Not for: `config.yaml`, `.env`, `auth.json`, `state.db`. Those are deliberately
out of scope — machine-specific or secret.

## Tools

| Tool | What it does |
|---|---|
| `local_sync_status` | identity, paired peers, last sync, files in base |
| `local_sync_peers` | devices seen on the LAN, paired flag, pending codes |
| `local_sync_pair` | start pairing → returns a 6-digit code |
| `local_sync_accept` | confirm an incoming request with that code |
| `local_sync_plan` | dry-run: pull / push / conflict per file |
| `local_sync_apply` | apply now |

Start with `local_sync_status`; if the peer is not listed, `local_sync_peers`.

Pairing is two-sided on purpose and always needs the human: either a 6-digit code
shown on both screens, or a shared password typed on both machines
(`local_sync_pair(peer=..., password=...)` then `local_sync_accept(peer=...,
password=...)`). Both flows print the same 6-digit code as a check — report it and
ask the user to compare the two screens. Never accept on the user's behalf.

For a click-through version of the same actions (discovery switch, pending
requests, plan, sync) point the user at `local-sync ui` — a loopback-only page
with a token in the URL. Prefer the UI when the user wants manual control over
what pairs and what syncs.

## What a plan means

- `pull` — the peer's copy changed, take it.
- `push` — local changed, send it.
- `conflict` — both changed since the last sync. Memory files are union-merged
  by `§` sections automatically. Any other file keeps the local version and
  saves the peer's as `<name>.conflict-<peer>-<ts>`; report that path to the user.
- `same` — nothing to do.

## Rules that matter in practice

- One daemon per machine. The tools start it on first use (`auto_start`) and
  then proxy to it; do not start a second instance, or the peer sees this host
  twice.
- Show the plan before applying when the user did not explicitly ask for an
  immediate sync.
- If pairing times out: broadcast may be blocked on this network. After the
  first pairing the daemon keeps unicasting to the stored address, so a working
  pair recovers by itself; a first contact on a locked-down network needs
  `local-sync --beacon-target <ip>:45870 run` on one side.
- Conflicts are not errors: the local copy wins that round, the loser is kept
  next to it, and the next sync converges.
