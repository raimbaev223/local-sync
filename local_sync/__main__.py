"""``python -m local_sync <command>`` — same entry point as the ``local-sync`` CLI."""

from .core import main

raise SystemExit(main())
