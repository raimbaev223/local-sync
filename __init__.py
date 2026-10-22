"""local-sync — peer-to-peer sync of a Hermes profile over the local network.

Registers six tools. The heavy lifting lives in ``local_sync.core`` and runs in
one daemon per machine; the tools proxy to it over a loopback control endpoint
and start that daemon on first use if it is not running yet.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import schemas, tools

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Called once by Hermes. Anything raised here disables the plugin."""
    settings = {
        "sync_interval": ctx.get_config("sync_interval", default=300),
        "notify": ctx.get_config("notify", default=True),
        "auto_start": ctx.get_config("auto_start", default=True),
        "roots": ctx.get_config("roots", default=None),
    }
    tools.configure({k: v for k, v in settings.items() if v is not None})

    for schema in schemas.TOOLS:
        ctx.register_tool(
            name=schema["name"],
            toolset="local_sync",
            schema=schema,
            handler=tools.HANDLERS[schema["name"]],
            description=schema["description"],
        )

    skills_dir = Path(__file__).resolve().parent / "skills"
    if skills_dir.is_dir():
        for child in sorted(skills_dir.iterdir()):
            skill_file = child / "SKILL.md"
            if skill_file.exists():
                ctx.register_skill(child.name, skill_file, description=f"local-sync: {child.name}")
