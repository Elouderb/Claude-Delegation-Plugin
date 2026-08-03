"""Shared base model and primitives for all Agent OS contracts.

Everything in ``contracts`` is a pydantic v2 model derived from
:class:`AgentOSModel`.  The base fixes two project-wide invariants:

  * ``extra="ignore"`` — the **tolerant-reader** posture.  Contracts are wire
    formats shared across machines and (eventually) language runtimes, evolving
    additively within a major version (proposal §13).  A consumer running an
    older contract build must be able to *parse* a payload that carries a
    newer, not-yet-known optional field rather than hard-failing at the
    boundary — otherwise every additive change forces a lockstep fleet upgrade.
    Unknown keys are therefore ignored on read.  Strictness is not lost, it is
    *relocated* to the producing side: :func:`strict_validate` rejects unknown
    keys and is used by the test-suite and by any producer that wants to prove
    it is not emitting fields outside its declared contract version.
  * ``schema_version: int = 1`` — every model carries a monotonically-managed
    schema version (ruling 9 / proposal §13).  The evolution rule
    (additive-only within a major version) is documented in ``CONTRACTS.md``;
    the field exists from day one so consumers can branch on it later without a
    breaking format change.

The base intentionally imports only pydantic + stdlib — ``contracts`` is the
dependency of everything else and depends on nothing in ``mcp/`` (ruling 1).
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from typing import Annotated, Type, TypeVar

from pydantic import BaseModel, ConfigDict, StringConstraints

# Open, dot-namespaced identifier pattern (``domain.verb``, e.g. ``task.created``
# or ``project_memory.commit``): a lowercase segment, then one or more further
# ``.segment`` groups.  Used for the *open* type registries — event types and
# capabilities — where the value space is deliberately extensible (new domains /
# verbs may appear without a contract change) but must stay well-formed so it can
# be routed and grouped by domain prefix.  See ``CONTRACTS.md`` / ``EVENTS.md``.
NAMESPACED_NAME = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"

#: A constrained-open string: any well-formed dot-namespaced name.  Constrains
#: shape without closing the value set (unlike an ``Enum`` field, which cannot
#: even *parse* an unknown member — the failure mode that poisons an outbox drain
#: or breaks registration fleet-wide).
Dotted = Annotated[str, StringConstraints(pattern=NAMESPACED_NAME)]


def utc_now() -> datetime:
    """Current time as a timezone-aware UTC ``datetime``.

    All contract timestamps are timezone-aware UTC (proposal §10.2 uses ISO-8601
    with a ``Z`` suffix, which pydantic emits for UTC datetimes).  Naive
    datetimes are rejected by :class:`~pydantic.AwareDatetime` fields, so this
    helper is the canonical way to stamp a new instance.
    """
    return datetime.now(timezone.utc)


class AgentOSModel(BaseModel):
    """Base for every Agent OS contract model.

    Subclasses inherit the tolerant-reader ``extra="ignore"`` config and the
    ``schema_version`` field.  The default ``schema_version`` is ``1`` for the
    initial contract generation.
    """

    model_config = ConfigDict(extra="ignore")

    schema_version: int = 1


_T = TypeVar("_T", bound=AgentOSModel)


@lru_cache(maxsize=None)
def _strict_mirror(model_cls: Type[_T]) -> Type[_T]:
    """A cached subclass of ``model_cls`` with ``extra="forbid"``.

    Used only to enforce the producer-side no-unknown-keys check; it is never
    imported into the package namespace, so it is not enrolled in
    ``ALL_MODELS`` / schema generation.
    """
    return type(  # type: ignore[return-value]
        f"Strict{model_cls.__name__}",
        (model_cls,),
        {"model_config": ConfigDict(extra="forbid")},
    )


def strict_validate(model_cls: Type[_T], data: object) -> _T:
    """Validate ``data`` against ``model_cls`` **rejecting unknown top-level keys**.

    This is the authoring/producer-side counterpart to the tolerant reader: a
    producer (or a test) uses it to prove a payload contains only fields declared
    by ``model_cls`` for its schema version, raising ``pydantic.ValidationError``
    on any extra key.  On success it returns a normal ``model_cls`` instance
    (the strict check is performed via an internal ``extra="forbid"`` mirror; the
    returned object is an ordinary tolerant instance of the requested class).

    Scope: the check is applied at the top level of ``model_cls``.  Nested
    contract objects are validated by their own (tolerant) contracts; a producer
    that wants a strict guarantee on a nested payload strict-validates that
    payload against its own model at its own authoring boundary.
    """
    _strict_mirror(model_cls).model_validate(data)
    return model_cls.model_validate(data)


__all__ = [
    "AgentOSModel",
    "utc_now",
    "NAMESPACED_NAME",
    "Dotted",
    "strict_validate",
]
