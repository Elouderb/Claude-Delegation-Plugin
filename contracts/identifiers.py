"""Prefixed ULID identifiers for Agent OS contracts (Phase 0, ruling 2).

Every durable Agent OS entity is named by a *prefixed ULID*: a short type
prefix, an underscore, then a 26-character `ULID
<https://github.com/ulid/spec>`_.  Examples::

    evt_01J8Z3M4Q5R6S7T8V9W0X1Y2Z3
    mem_01J8Z3M4Q5R6S7T8V9W0X1Y2Z3

Why ULIDs (not UUIDv4): a ULID's leading 48 bits are the creation time in
milliseconds, so ids sort *lexicographically in creation order* — handy for
event logs, sync cursors, and human-scannable ids — while the trailing 80 bits
of randomness keep them collision-safe across machines without coordination.

We vendor a ~40-line generator rather than take a dependency (ruling 2).  It is
deliberately minimal:

  * **Not** process-monotonic — two ids minted in the same millisecond are
    ordered only by their random tail.  The spec's monotonic-increment option is
    not needed for Phase 0 and is explicitly out of scope.
  * Crockford base32 (`0-9A-HJKMNP-TV-Z`, excluding I/L/O/U) so ids are
    case-insensitive-safe and unambiguous.

The alphabet, ULID width, and prefix registry are the stable public surface;
`CONTRACTS.md` documents the format and prefix table.
"""

from __future__ import annotations

import os
import time
from typing import Tuple

# Crockford base32 alphabet (RFC-free; the ULID spec's canonical alphabet).
# 32 symbols, excludes I, L, O, U to avoid visual/transcription ambiguity.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CROCKFORD_INDEX = {ch: i for i, ch in enumerate(_CROCKFORD)}

# A ULID is 128 bits: 48-bit millisecond timestamp + 80-bit randomness, encoded
# as 26 Crockford-base32 characters (26 * 5 = 130 bits; the top 2 bits are 0).
ULID_LENGTH = 26
_TIMESTAMP_BITS = 48
_RANDOM_BYTES = 10  # 80 bits
_MAX_TIMESTAMP = (1 << _TIMESTAMP_BITS) - 1


# -- prefix registry (ruling 2) ------------------------------------------------
# The canonical type prefixes are the module constants below; they are the
# single source of truth.  ``KNOWN_PREFIXES`` is *derived* from them (rather than
# maintained as a second parallel list) so a new prefix is added in exactly one
# place.  The docs / tests introspect ``KNOWN_PREFIXES``.
EVENT = "evt"
TASK = "task"
MACHINE = "mach"
REPOSITORY = "repo"
PROJECT = "proj"
AGENT = "agent"
SESSION = "sess"
MEMORY = "mem"
PROPOSAL = "prop"
GRANT = "grant"  # CapabilityGrant.grant_id — a durable, registered grant record.

# The set of valid prefix stems (without the trailing underscore), derived from
# the constants above so there is one authoritative registry.
KNOWN_PREFIXES = frozenset(
    {EVENT, TASK, MACHINE, REPOSITORY, PROJECT, AGENT, SESSION, MEMORY, PROPOSAL, GRANT}
)


def _encode_crockford(value: int, length: int) -> str:
    """Big-endian, left-zero-padded Crockford base32 encoding of ``value``.

    ``value`` must be non-negative and fit in ``length`` base32 digits.  The
    result is fixed-width so ids of the same width sort lexicographically by
    numeric value.
    """
    if value < 0:
        raise ValueError("cannot encode a negative value")
    chars = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        chars.append(_CROCKFORD[rem])
    if value:
        raise ValueError("value too large for the requested width")
    return "".join(reversed(chars))


def generate_ulid(timestamp_ms: int | None = None) -> str:
    """Return a fresh 26-character ULID string (no prefix).

    ``timestamp_ms`` defaults to the current wall-clock time in milliseconds;
    passing it explicitly is intended for tests that need deterministic ordering.
    """
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)
    if not 0 <= timestamp_ms <= _MAX_TIMESTAMP:
        raise ValueError(f"timestamp_ms out of 48-bit range: {timestamp_ms}")
    randomness = int.from_bytes(os.urandom(_RANDOM_BYTES), "big")
    value = (timestamp_ms << (_RANDOM_BYTES * 8)) | randomness
    return _encode_crockford(value, ULID_LENGTH)


def is_valid_ulid(value: str) -> bool:
    """True iff ``value`` is a syntactically valid 26-char Crockford ULID.

    A ULID encodes 128 bits in 26 Crockford-base32 characters.  ``26 * 5 = 130``
    bits of code space, so the most-significant character carries only the top
    3 bits (0-7): the largest valid ULID is ``7ZZZ…`` and the first character
    must be ``0``-``7``.  We reject strings whose leading character exceeds ``7``
    (i.e. encode more than 128 bits) so we accept exactly what canonical
    cross-language ULID libraries (python-ulid, JS ``ulid``) accept.
    """
    if not isinstance(value, str) or len(value) != ULID_LENGTH:
        return False
    if value[0] not in "01234567":
        return False
    return all(ch in _CROCKFORD_INDEX for ch in value)


def new_id(prefix: str) -> str:
    """Mint a new prefixed id, e.g. ``new_id(EVENT) -> 'evt_01J...'``.

    ``prefix`` must be one of the registered stems in :data:`KNOWN_PREFIXES`;
    an unknown prefix is a programming error and raises.
    """
    if prefix not in KNOWN_PREFIXES:
        raise ValueError(
            f"unknown id prefix {prefix!r}; known prefixes: "
            f"{sorted(KNOWN_PREFIXES)}"
        )
    return f"{prefix}_{generate_ulid()}"


def split_id(value: str) -> Tuple[str, str]:
    """Split a prefixed id into ``(prefix, ulid)``.

    Raises ``ValueError`` if the string is not ``<prefix>_<ulid>`` with a known
    prefix and a valid ULID body.
    """
    prefix, sep, body = value.partition("_")
    if not sep or prefix not in KNOWN_PREFIXES or not is_valid_ulid(body):
        raise ValueError(f"not a valid prefixed id: {value!r}")
    return prefix, body


def is_valid_id(value: str, prefix: str | None = None) -> bool:
    """True iff ``value`` is a valid prefixed id (optionally of ``prefix``)."""
    try:
        found, _body = split_id(value)
    except (ValueError, AttributeError):
        return False
    return prefix is None or found == prefix


__all__ = [
    "ULID_LENGTH",
    "EVENT",
    "TASK",
    "MACHINE",
    "REPOSITORY",
    "PROJECT",
    "AGENT",
    "SESSION",
    "MEMORY",
    "PROPOSAL",
    "GRANT",
    "KNOWN_PREFIXES",
    "generate_ulid",
    "is_valid_ulid",
    "new_id",
    "split_id",
    "is_valid_id",
]
