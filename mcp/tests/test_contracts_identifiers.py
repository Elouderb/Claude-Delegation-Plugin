"""Tests for contracts.identifiers — prefixed-ULID properties (card AC4).

Proves: 26-char Crockford base32, lexicographic sortability by timestamp,
uniqueness, and the prefix helpers (new_id / split_id / is_valid_id /
is_valid_ulid).  Pure stdlib + contracts; no optional deps.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from contracts import identifiers as ids  # noqa: E402

_CROCKFORD = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


class TestUlid(unittest.TestCase):
    def test_length_and_alphabet(self):
        u = ids.generate_ulid()
        self.assertEqual(len(u), ids.ULID_LENGTH)
        self.assertEqual(len(u), 26)
        self.assertTrue(set(u) <= _CROCKFORD, f"non-Crockford chars in {u}")
        self.assertNotIn("I", u)  # excluded letters never appear
        self.assertNotIn("L", u)
        self.assertNotIn("O", u)
        self.assertNotIn("U", u)

    def test_is_valid_ulid(self):
        self.assertTrue(ids.is_valid_ulid(ids.generate_ulid()))
        self.assertFalse(ids.is_valid_ulid("too-short"))
        self.assertFalse(ids.is_valid_ulid("I" * 26))  # I is not in the alphabet
        self.assertFalse(ids.is_valid_ulid(123))  # type: ignore[arg-type]

    def test_is_valid_ulid_first_char_bounds(self):
        # A ULID encodes 128 bits in 26 base32 chars; the leading char carries
        # only 3 bits, so it must be 0-7. Reject strings that encode >128 bits
        # (what cross-language ULID libraries reject), even if every char is a
        # legal Crockford symbol.
        self.assertFalse(ids.is_valid_ulid("Z" * 26))  # first char 'Z' -> >128 bits
        self.assertFalse(ids.is_valid_ulid("8" + "0" * 25))  # '8' -> overflow
        self.assertTrue(ids.is_valid_ulid("7" + "Z" * 25))  # 7ZZ... is the max ULID
        # every generated ULID starts 0-7
        for _ in range(200):
            self.assertIn(ids.generate_ulid()[0], "01234567")

    def test_sortable_by_timestamp(self):
        # Larger timestamps must produce lexicographically larger ids regardless
        # of the random tail (the whole point of ULIDs).
        early = ids.generate_ulid(timestamp_ms=1_000)
        late = ids.generate_ulid(timestamp_ms=2_000_000_000_000)
        self.assertLess(early, late)
        # Monotone across a sweep of increasing timestamps.
        prev = ""
        for ms in range(0, 5_000_000, 500_000):
            cur = ids.generate_ulid(timestamp_ms=ms)
            self.assertGreaterEqual(cur, prev)
            prev = cur

    def test_uniqueness(self):
        fixed_ts = 1_700_000_000_000
        seen = {ids.generate_ulid(timestamp_ms=fixed_ts) for _ in range(2000)}
        self.assertEqual(len(seen), 2000)  # 80 bits of randomness -> no collisions

    def test_timestamp_range_guard(self):
        with self.assertRaises(ValueError):
            ids.generate_ulid(timestamp_ms=-1)
        with self.assertRaises(ValueError):
            ids.generate_ulid(timestamp_ms=1 << 48)  # exceeds 48-bit field


class TestPrefixes(unittest.TestCase):
    def test_registry_has_expected_prefixes(self):
        expected = {
            "evt", "task", "mach", "repo", "proj",
            "agent", "sess", "mem", "prop", "grant",
        }
        self.assertEqual(set(ids.KNOWN_PREFIXES), expected)

    def test_grant_prefix_registered(self):
        # CapabilityGrant.grant_id is a registered prefixed ULID: the package must
        # accept its own canonical example (grant_01J...).
        gid = ids.new_id(ids.GRANT)
        self.assertTrue(gid.startswith("grant_"))
        self.assertTrue(ids.is_valid_id(gid))
        self.assertTrue(ids.is_valid_id(gid, prefix=ids.GRANT))
        self.assertTrue(ids.is_valid_id("grant_01J8Z3M4Q5R6S7T8V9W0X1Y2Z3"))

    def test_new_id_shape(self):
        for stem in ids.KNOWN_PREFIXES:
            value = ids.new_id(stem)
            self.assertTrue(value.startswith(stem + "_"))
            prefix, body = ids.split_id(value)
            self.assertEqual(prefix, stem)
            self.assertTrue(ids.is_valid_ulid(body))
            self.assertTrue(ids.is_valid_id(value))
            self.assertTrue(ids.is_valid_id(value, prefix=stem))

    def test_new_id_rejects_unknown_prefix(self):
        with self.assertRaises(ValueError):
            ids.new_id("bogus")

    def test_split_and_validate_bad_inputs(self):
        self.assertFalse(ids.is_valid_id("nodashvalue"))
        self.assertFalse(ids.is_valid_id("bogus_" + "0" * 26))  # unknown prefix
        self.assertFalse(ids.is_valid_id("evt_short"))
        self.assertFalse(ids.is_valid_id("evt_" + "I" * 26))  # bad ulid body
        # a valid evt id is not a valid task id
        evt = ids.new_id(ids.EVENT)
        self.assertFalse(ids.is_valid_id(evt, prefix=ids.TASK))
        with self.assertRaises(ValueError):
            ids.split_id("nope")


if __name__ == "__main__":
    unittest.main()
