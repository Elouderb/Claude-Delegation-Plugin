"""Tests for contracts.examples — every model has a valid example (AC1/§16).

The examples module is the "emit example payloads" success criterion.  These
tests prove it covers EVERY exported model (coverage is introspected from
``contracts.ALL_MODELS`` so a new model without an example fails loudly), that
each example is valid and round-trips, and that ``python -m contracts.examples``
runs and prints validated JSON.
"""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

import contracts  # noqa: E402
from contracts import examples  # noqa: E402


class TestExampleCoverage(unittest.TestCase):
    # Coverage is asserted once, via the introspecting ``check_coverage()`` path
    # (the models test no longer duplicates the set-equality check). Round-trip of
    # every example is likewise asserted once, in test_contracts_models.

    def test_check_coverage_passes(self):
        examples.check_coverage()  # must not raise

    def test_each_example_is_correct_type(self):
        for name, model in examples.EXAMPLES.items():
            with self.subTest(model=name):
                self.assertEqual(type(model).__name__, name)
                self.assertIsInstance(model, contracts.AgentOSModel)

    def test_coverage_fails_loudly_on_missing_example(self):
        # Introspection guarantee: drop one example and prove check_coverage
        # reports the gap rather than silently passing.
        reduced = dict(examples.EXAMPLES)
        dropped = reduced.pop(next(iter(reduced)))
        with patch.object(examples, "EXAMPLES", reduced):
            with self.assertRaises(AssertionError) as ctx:
                examples.check_coverage()
        self.assertIn(type(dropped).__name__, str(ctx.exception))

    def test_coverage_fails_loudly_on_unregistered_example(self):
        extra = dict(examples.EXAMPLES)
        extra["NotAModel"] = examples.EXAMPLES[next(iter(examples.EXAMPLES))]
        with patch.object(examples, "EXAMPLES", extra):
            with self.assertRaises(AssertionError):
                examples.check_coverage()


class TestExampleModuleRuns(unittest.TestCase):
    def test_python_m_contracts_examples(self):
        result = subprocess.run(
            [sys.executable, "-m", "contracts.examples"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        # emits a section per model
        self.assertIn("# EventEnvelope", result.stdout)
        self.assertIn("# Finding", result.stdout)
        # every registered model name appears as a header
        for cls in contracts.ALL_MODELS:
            self.assertIn(f"# {cls.__name__}", result.stdout)


if __name__ == "__main__":
    unittest.main()
