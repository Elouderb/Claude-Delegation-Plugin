"""Tests for the contract models — round-trip + invalid-payload rejection (AC1).

For EVERY model in ``contracts.ALL_MODELS`` (driven by the canonical example in
``contracts.examples.EXAMPLES``):

  * model -> json -> model round-trips to an equal instance (the single
    round-trip site — the examples test no longer duplicates it);
  * an unexpected extra key is *tolerated* on parse (tolerant reader) but
    *rejected* by ``contracts.strict_validate`` (producer-side check);
  * dropping a required field is rejected.

Plus targeted tests: every §10.3 event type (incl. the added ``task.reconciled``)
round-trips through ``EventEnvelope``; the open event-type / capability
registries accept unknown well-formed ``domain.verb`` names and reject malformed
ones; a naive datetime and out-of-range numeric fields are rejected; the
tolerant-reader vs. ``strict_validate`` posture holds; and the §11.2 Finding
payload validates verbatim.
"""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import contracts  # noqa: E402
from contracts import examples  # noqa: E402
from pydantic import ValidationError  # noqa: E402


class TestRoundTripAllModels(unittest.TestCase):
    """Data-driven over every registered model via its example instance."""

    def test_every_model_round_trips(self):
        for name, model in examples.EXAMPLES.items():
            with self.subTest(model=name):
                cls = type(model)
                reparsed = cls.model_validate_json(model.model_dump_json())
                self.assertEqual(reparsed, model)
                # dict round-trip (mode="json") also validates
                reparsed2 = cls.model_validate(model.model_dump(mode="json"))
                self.assertEqual(reparsed2, model)

    def test_every_model_tolerates_but_strict_rejects_extra_key(self):
        # Tolerant reader: an unexpected key parses (and is dropped). Producer-side
        # strict_validate: the same payload is rejected.
        for name, model in examples.EXAMPLES.items():
            with self.subTest(model=name):
                payload = model.model_dump(mode="json")
                payload["__unexpected_field__"] = "x"
                tolerant = type(model).model_validate(payload)
                self.assertNotIn("__unexpected_field__", tolerant.model_dump())
                with self.assertRaises(ValidationError):
                    contracts.strict_validate(type(model), payload)

    def test_every_model_rejects_missing_required_field(self):
        for name, model in examples.EXAMPLES.items():
            cls = type(model)
            required = [
                fname for fname, f in cls.model_fields.items() if f.is_required()
            ]
            if not required:
                continue
            with self.subTest(model=name):
                payload = model.model_dump(mode="json")
                payload.pop(required[0], None)
                with self.assertRaises(ValidationError):
                    cls.model_validate(payload)


class TestEventTaxonomy(unittest.TestCase):
    def _base(self):
        return {
            "event_id": "evt_01J8Z3M4Q5R6S7T8V9W0X1Y2Z3",
            "occurred_at": "2026-08-03T20:00:00Z",
            "machine_id": "mach_01J8Z3M4Q5R6S7T8V9W0X1Y2Z3",
        }

    def test_every_event_type_round_trips(self):
        for et in contracts.EventType:
            with self.subTest(event_type=et.value):
                payload = dict(self._base(), event_type=et.value)
                evt = contracts.EventEnvelope.model_validate(payload)
                self.assertEqual(evt.event_type, et.value)
                self.assertEqual(
                    contracts.EventEnvelope.model_validate_json(
                        evt.model_dump_json()
                    ).event_type,
                    et.value,
                )

    def test_reconciliation_event_type_registered(self):
        # §14.3 reconciliation event — surfaced by the review as a §10.3 gap.
        values = {et.value for et in contracts.EventType}
        self.assertIn("task.reconciled", values)

    def test_unknown_wellformed_event_type_accepted(self):
        # Open registry: a consumer must be able to parse a newer producer's
        # event type it does not yet know, so it can still route / dead-letter it.
        payload = dict(self._base(), event_type="custom.new_kind")
        evt = contracts.EventEnvelope.model_validate(payload)
        self.assertEqual(evt.event_type, "custom.new_kind")

    def test_malformed_event_type_rejected(self):
        for bad in ("NotDotted", "bad type", "UP.per", "trailing.", "task", "a..b"):
            with self.subTest(bad=bad):
                payload = dict(self._base(), event_type=bad)
                with self.assertRaises(ValidationError):
                    contracts.EventEnvelope.model_validate(payload)

    def test_naive_datetime_rejected(self):
        payload = dict(
            self._base(),
            event_type="task.created",
            occurred_at="2026-08-03T20:00:00",  # no tz -> naive -> rejected
        )
        with self.assertRaises(ValidationError):
            contracts.EventEnvelope.model_validate(payload)

    def test_aware_datetime_accepted_and_utc(self):
        evt = contracts.EventEnvelope(
            event_id="evt_x",
            event_type=contracts.EventType.TASK_CREATED,
            occurred_at=datetime(2026, 8, 3, 20, 0, 0, tzinfo=timezone.utc),
            machine_id="mach_x",
        )
        # pydantic emits the Z suffix for UTC (proposal §10.2 example).
        self.assertIn('"occurred_at":"2026-08-03T20:00:00Z"', evt.model_dump_json())


class TestNumericBounds(unittest.TestCase):
    def test_claim_confidence_bounds(self):
        contracts.Claim(statement="ok", confidence=0.0)
        contracts.Claim(statement="ok", confidence=1.0)
        for bad in (-0.1, 1.1):
            with self.assertRaises(ValidationError):
                contracts.Claim(statement="ok", confidence=bad)

    def test_context_token_budget_positive(self):
        contracts.ContextPackage(token_budget=1)
        for bad in (0, -5):
            with self.assertRaises(ValidationError):
                contracts.ContextPackage(token_budget=bad)

    def test_task_record_version_ge_one(self):
        with self.assertRaises(ValidationError):
            contracts.TaskRecord(
                global_id="task_x",
                status=contracts.TaskSyncStatus.CREATED,
                version=0,
                updated_at=datetime(2026, 8, 3, 20, 0, 0, tzinfo=timezone.utc),
                originating_client="mach_x",
            )

    def test_test_report_counts_non_negative(self):
        with self.assertRaises(ValidationError):
            contracts.TestReport(artifact_id="t", summary="s", passed=-1)


class TestProposalExamples(unittest.TestCase):
    def test_finding_matches_proposal_11_2_verbatim(self):
        # The proposal §11.2 payload (its exact keys) must validate as a Finding.
        payload = {
            "artifact_type": "finding",
            "artifact_id": "finding_01J8Z3M4Q5R6S7T8V9W0X1Y2Z3",
            "author_agent": "database-engineer",
            "task_id": "TASK-184",
            "claims": [
                {
                    "statement": "CustomerOrder.CustomerId has no foreign-key constraint.",
                    "confidence": 0.99,
                    "evidence": ["dbgraph://CustomerOrder/CustomerId"],
                }
            ],
            "recommended_actions": [
                "Determine whether the missing constraint is intentional before "
                "modifying the schema."
            ],
        }
        finding = contracts.Finding.model_validate(payload)
        self.assertEqual(finding.claims[0].confidence, 0.99)
        self.assertEqual(finding.artifact_type, "finding")

    def test_memory_node_matches_proposal_4_3(self):
        node = examples.EXAMPLES["MemoryNode"]
        self.assertIsInstance(node, contracts.MemoryNode)
        assert isinstance(node, contracts.MemoryNode)  # narrow for the type checker
        self.assertEqual(node.canonical_name, "Dedicated memory curator")
        self.assertEqual(node.scope, "global")
        self.assertEqual(node.approval_state.value, "accepted")

    def test_schema_version_present_everywhere(self):
        for name, model in examples.EXAMPLES.items():
            with self.subTest(model=name):
                self.assertEqual(model.schema_version, 1)


class TestOpenCapabilityRegistry(unittest.TestCase):
    _TS = datetime(2026, 8, 3, 20, 0, 0, tzinfo=timezone.utc)

    def test_registration_accepts_unknown_capability(self):
        # A later machine's capability (e.g. telegram.send) must not break
        # registration parsing fleet-wide — the heartbeat/registration path.
        reg = contracts.AgentRegistration(
            agent_id="agent_x",
            session_id="sess_x",
            agent_type="implementer",
            machine_id="mach_x",
            capabilities=["repo.read", "telegram.send"],
            started_at=self._TS,
        )
        self.assertEqual(reg.capabilities, ["repo.read", "telegram.send"])

    def test_grant_accepts_unknown_capability(self):
        grant = contracts.CapabilityGrant(
            grant_id="grant_x",
            capability="schedule.create",
            principal_type="agent",
            principal_id="agent_x",
            granted_by="user:x",
            granted_at=self._TS,
        )
        self.assertEqual(grant.capability, "schedule.create")

    def test_malformed_capability_rejected(self):
        for bad in ("NotACap", "no dot", "Repo.Write", "repo."):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    contracts.CapabilityGrant(
                        grant_id="grant_x",
                        capability=bad,
                        principal_type="agent",
                        principal_id="agent_x",
                        granted_by="user:x",
                        granted_at=self._TS,
                    )

    def test_registry_values_are_wellformed(self):
        # Every producer-registry value (EventType, Capability) must itself be a
        # valid open-string value, or a producer using the registry would emit an
        # unparseable field.
        for value in [e.value for e in contracts.EventType] + [
            c.value for c in contracts.Capability
        ]:
            with self.subTest(value=value):
                contracts.EventEnvelope.model_validate(
                    {
                        "event_id": "evt_x",
                        "event_type": value,
                        "occurred_at": "2026-08-03T20:00:00Z",
                        "machine_id": "mach_x",
                    }
                )


class TestTolerantReaderAndStrictValidate(unittest.TestCase):
    def test_tolerant_parse_ignores_unknown_key(self):
        c = contracts.Claim.model_validate(
            {"statement": "s", "confidence": 0.5, "future_field": 123}
        )
        self.assertEqual(c.statement, "s")
        self.assertNotIn("future_field", c.model_dump())

    def test_strict_validate_rejects_unknown_key(self):
        with self.assertRaises(ValidationError):
            contracts.strict_validate(
                contracts.Claim, {"statement": "s", "confidence": 0.5, "x": 1}
            )

    def test_strict_validate_returns_requested_class_instance(self):
        c = contracts.strict_validate(
            contracts.Claim, {"statement": "s", "confidence": 0.5}
        )
        self.assertIs(type(c), contracts.Claim)
        self.assertEqual(c.confidence, 0.5)


class TestTaskRecordContentFields(unittest.TestCase):
    def test_content_fields_round_trip(self):
        rec = contracts.TaskRecord(
            global_id="task_x",
            title="Do the thing",
            description="A longer description.",
            priority="high",
            status=contracts.TaskSyncStatus.BLOCKED,
            status_reason="waiting on review",
            blocked_reason="needs approval",
            updated_at=datetime(2026, 8, 3, 20, 0, 0, tzinfo=timezone.utc),
            originating_client="mach_x",
        )
        back = contracts.TaskRecord.model_validate_json(rec.model_dump_json())
        self.assertEqual(back, rec)
        self.assertEqual(back.title, "Do the thing")
        self.assertEqual(back.blocked_reason, "needs approval")


if __name__ == "__main__":
    unittest.main()
