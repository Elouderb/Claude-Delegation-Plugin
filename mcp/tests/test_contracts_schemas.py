"""Tests for the committed JSON Schemas — drift + coverage (card AC2).

Proves the committed ``contracts/schemas/*.json`` are byte-identical to a fresh
regeneration (the CI drift gate), that every model has exactly one committed
schema (no missing, no stale), and that ``check_all`` actually detects drift.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import contracts  # noqa: E402
from contracts import generate_schemas as gen  # noqa: E402


class TestSchemaDrift(unittest.TestCase):
    def test_committed_schemas_in_sync(self):
        # The authoritative check the CLI --check runs.
        self.assertEqual(gen.check_all(), [])

    def test_regeneration_is_byte_identical(self):
        for model in contracts.ALL_MODELS:
            with self.subTest(model=model.__name__):
                committed = (gen.SCHEMA_DIR / gen.schema_filename(model)).read_text(
                    encoding="utf-8"
                )
                self.assertEqual(committed, gen.schema_json(model))

    def test_write_all_to_tmp_matches_committed(self):
        with tempfile.TemporaryDirectory(prefix="agent_os_schemas_") as tmp:
            written = gen.write_all(Path(tmp))
            self.assertEqual(len(written), len(contracts.ALL_MODELS))
            for model in contracts.ALL_MODELS:
                fresh = (Path(tmp) / gen.schema_filename(model)).read_text(
                    encoding="utf-8"
                )
                committed = (gen.SCHEMA_DIR / gen.schema_filename(model)).read_text(
                    encoding="utf-8"
                )
                self.assertEqual(fresh, committed, model.__name__)

    def test_every_model_has_a_committed_schema_and_no_stale(self):
        committed = {p.name for p in gen.SCHEMA_DIR.glob("*.json")}
        expected = {gen.schema_filename(m) for m in contracts.ALL_MODELS}
        self.assertEqual(committed, expected)

    def test_schema_json_is_deterministic(self):
        for model in contracts.ALL_MODELS:
            self.assertEqual(gen.schema_json(model), gen.schema_json(model))

    def test_check_all_detects_drift(self):
        # Copy the real schemas into a tmp dir, then corrupt one and add a stale
        # file; check_all must report both.
        with tempfile.TemporaryDirectory(prefix="agent_os_schemadrift_") as tmp:
            target = Path(tmp)
            gen.write_all(target)
            victim = gen.schema_filename(contracts.ALL_MODELS[0])
            (target / victim).write_text("{}\n", encoding="utf-8")
            (target / "StaleModel.json").write_text("{}\n", encoding="utf-8")
            problems = gen.check_all(target)
            self.assertTrue(any("out of date" in p and victim in p for p in problems), problems)
            self.assertTrue(any("stale" in p and "StaleModel.json" in p for p in problems), problems)

    def test_check_all_detects_missing(self):
        with tempfile.TemporaryDirectory(prefix="agent_os_schemamiss_") as tmp:
            target = Path(tmp)
            gen.write_all(target)
            missing = gen.schema_filename(contracts.ALL_MODELS[0])
            (target / missing).unlink()
            problems = gen.check_all(target)
            self.assertTrue(any("missing" in p and missing in p for p in problems), problems)

    def test_check_all_tolerates_crlf_checkout(self):
        # A Windows autocrlf=true checkout can leave CRLF on disk; --check must
        # normalize newlines and NOT report false drift.
        with tempfile.TemporaryDirectory(prefix="agent_os_schemacrlf_") as tmp:
            target = Path(tmp)
            gen.write_all(target)
            for path in target.glob("*.json"):
                lf = path.read_text(encoding="utf-8")
                path.write_bytes(lf.replace("\n", "\r\n").encode("utf-8"))
            self.assertEqual(gen.check_all(target), [])

    def test_tolerant_posture_no_additional_properties_false(self):
        # The committed schemas must reflect the tolerant-reader posture: no
        # model closes its object with additionalProperties:false.
        for model in contracts.ALL_MODELS:
            with self.subTest(model=model.__name__):
                schema = model.model_json_schema()
                self.assertNotIn("additionalProperties", schema)


if __name__ == "__main__":
    unittest.main()
