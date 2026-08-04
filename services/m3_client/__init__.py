"""Stand-in M3 read-only client (Phase 1d, card 93baf05b).

A small terminal client that plays Machine 3's role — reading machine,
repository, active-session, and recent-event state out of the Agent OS ``/v1``
API — until the real M3 VM and its communication agent exist (Phase 6). See
:mod:`services.m3_client.__main__` for the CLI entry point
(``python -m services.m3_client status``).

**Stdlib only, end to end.** Every module in this package imports nothing
beyond the Python standard library (``urllib``, ``json``, ``argparse``, ...) —
not even this repo's own ``contracts``/``python-dotenv`` — because this client
is meant to eventually run standalone on a bare-Python M3 VM that has none of
this repo's other packages or dependencies installed. See
``services/README.md`` for the full rationale and the ``--client-id`` /
sync-cursor design.
"""

from __future__ import annotations

__all__: list = []
