"""Tool schemas handed to the model. Descriptions say WHEN to use a tool."""

from __future__ import annotations

STATUS = {
    "name": "local_sync_status",
    "description": (
        "Show this machine's local-sync identity (name, profile, ports), the peers it is "
        "already paired with, and when each pair last synced. Use this first when the user "
        "asks whether skills/memory are in sync between their machines."
    ),
    "parameters": {"type": "object", "properties": {}},
}

PEERS = {
    "name": "local_sync_peers",
    "description": (
        "List devices running local-sync on the same LAN, whether each is already paired, "
        "and any pending pairing requests with their 6-digit codes. Use when the user says "
        "'find my other machine' or asks why a device is not syncing."
    ),
    "parameters": {"type": "object", "properties": {}},
}

PAIR = {
    "name": "local_sync_pair",
    "description": (
        "Start pairing with a discovered peer. Returns a 6-digit code that must be confirmed "
        "on the other machine with local_sync_accept. Use only after the user asked to connect "
        "two machines; never pair silently."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "peer": {"type": "string", "description": "Peer name from local_sync_peers, e.g. 'ubuntu-pc'"},
            "password": {
                "type": "string",
                "description": "Optional shared password. When set, pairing uses the password handshake: "
                               "the same password is entered on the other machine and both print a matching code.",
            },
        },
        "required": ["peer"],
    },
}

ACCEPT = {
    "name": "local_sync_accept",
    "description": (
        "Confirm an incoming pairing request on THIS machine. The code is shown in the "
        "notification and in local_sync_peers; pass it so the user can verify both screens match."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "peer": {"type": "string", "description": "Name of the machine that asked to pair"},
            "code": {"type": "string", "description": "6-digit code shown on both machines"},
            "password": {"type": "string", "description": "Shared password for the password handshake"},
        },
        "required": ["peer"],
    },
}

PLAN = {
    "name": "local_sync_plan",
    "description": (
        "Dry-run of the sync against a paired peer: per-file pull / push / conflict actions "
        "with hashes. Use before local_sync_apply when the user wants to see what would change."
    ),
    "parameters": {
        "type": "object",
        "properties": {"peer": {"type": "string", "description": "Paired peer name"}},
        "required": ["peer"],
    },
}

APPLY = {
    "name": "local_sync_apply",
    "description": (
        "Apply the sync now: pull changed files, push local ones, union-merge memories, keep "
        "the losing side of a conflict as a *.conflict-<peer>-<ts> file. Use when the user asks "
        "to sync or after a pairing was accepted."
    ),
    "parameters": {
        "type": "object",
        "properties": {"peer": {"type": "string", "description": "Paired peer name"}},
        "required": ["peer"],
    },
}

SESSIONS = {
    "name": "local_sync_sessions",
    "description": (
        "Copy the peer's chat history into this profile: sessions and messages are inserted "
        "only when missing, nothing local is overwritten. Use when the user asks to move or "
        "mirror their conversations from the other machine."
    ),
    "parameters": {
        "type": "object",
        "properties": {"peer": {"type": "string", "description": "Paired peer name"}},
        "required": ["peer"],
    },
}

PROFILES = {
    "name": "local_sync_profiles",
    "description": (
        "List the Hermes profiles that exist on a paired machine, with skill/memory/session sizes. "
        "Use when the user wants to see or bring over a profile from the other machine."
    ),
    "parameters": {
        "type": "object",
        "properties": {"peer": {"type": "string", "description": "Paired peer name"}},
        "required": ["peer"],
    },
}

IMPORT_PROFILE = {
    "name": "local_sync_import_profile",
    "description": (
        "Copy a profile from the paired machine into a new local profile directory (skills, memories, "
        "SOUL.md, hooks, cron, desktop-plugins). Refuses to touch the active profile or overwrite an "
        "existing one unless overwrite=true."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "peer": {"type": "string"},
            "profile": {"type": "string", "description": "Profile name on the peer"},
            "local_name": {"type": "string", "description": "Local directory name (defaults to the same name)"},
            "overwrite": {"type": "boolean", "description": "Overwrite files in an existing profile dir"},
        },
        "required": ["peer", "profile"],
    },
}

TOOLS = [STATUS, PEERS, PAIR, ACCEPT, PLAN, APPLY, SESSIONS, PROFILES, IMPORT_PROFILE]
