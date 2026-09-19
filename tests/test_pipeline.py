"""Regression tests using recorded public GitHub responses and pure triage rules."""
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cacheboard import atomic_json, classify_modules, confidence, months_ago, normalize, priority, to_entry
from sync import build_snapshot

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(number):
    return normalize(json.loads((FIXTURES / f"{number}.json").read_text()))


class PipelineTests(unittest.TestCase):
    def test_real_open_issue_and_pr_have_details(self):
        for number in (38485, 38494):
            entry = to_entry(fixture(number), {})
            self.assertIsNotNone(entry)
            self.assertTrue(entry["body"])
            self.assertTrue(entry["summary"])
            self.assertIn(entry["priority"], ("P0", "P1", "P2", "P3"))
        self.assertIsNotNone(to_entry(fixture(38485), {})["confidence"])
        self.assertIsNone(to_entry(fixture(38494), {})["confidence"])

    def test_real_closed_issue_and_merged_pr_are_removed(self):
        for number in (38477, 38195):
            self.assertEqual(fixture(number)["state"], "closed")
            self.assertIsNone(to_entry(fixture(number), {}))
        raw = json.loads((FIXTURES / "38195.json").read_text())
        self.assertTrue(raw["pull_request"]["merged_at"])

    def test_snapshot_reflects_additions_and_closures(self):
        moment = dt.datetime(2026, 9, 10, 12, tzinfo=dt.timezone.utc)
        records = {str(n):fixture(n) for n in (38485, 38494, 38195, 38477)}
        snapshot = build_snapshot(records, {}, moment, {})
        self.assertEqual({e["number"] for e in snapshot["entries"]}, {38485, 38494})
        records.pop("38494")
        self.assertEqual([e["number"] for e in build_snapshot(records, {}, moment, {})["entries"]], [38485])

    def test_six_month_window_expires_items(self):
        moment = dt.datetime(2027, 4, 1, tzinfo=dt.timezone.utc)
        self.assertEqual(build_snapshot({"38485":fixture(38485)}, {}, moment, {})["entries"], [])

    def test_calendar_month_boundaries(self):
        moment = dt.datetime(2026, 3, 31, 8, tzinfo=dt.timezone.utc)
        self.assertEqual(months_ago(moment, 1).day, 28)
        self.assertEqual(months_ago(moment, 6).isoformat(), "2025-09-30T08:00:00+00:00")
        leap = moment.replace(year=2024)
        self.assertEqual(months_ago(leap, 1).day, 29)

    def test_module_boundaries_and_multi_module_support(self):
        self.assertEqual(classify_modules("[HiSparse] Connect HiCache logical KV pool", ""), ["hicache", "hisparse"])
        self.assertEqual(classify_modules("UnifiedRadixCache host restore", ""), ["unified"])
        self.assertEqual(classify_modules("Br l3 memcache", ""), ["hicache"])
        self.assertEqual(classify_modules("New unrelated model", "## Related\nHiCache HiCache HiSparse HiSparse"), [])
        self.assertEqual(classify_modules("[Kernel] RadixTopK speedup", ""), [])

    def test_rfc_reference_is_not_an_rfc(self):
        item = fixture(38494)
        item["title"] = "[HiCache] NIXL registration (RFC #32841, stage 1)"
        self.assertEqual(to_entry(item, {})["kind"], "pr")
        item["title"] = "[RFC] HiCache async completion"
        self.assertEqual(to_entry(item, {})["kind"], "rfc")
        self.assertEqual(to_entry(item, {})["source_kind"], "pr")

    def test_template_does_not_manufacture_confidence(self):
        result = confidence("## Checklist\n- [x] The bug persists in the latest version.\n- [x] Please provide environment.\nSomething seems broken.")
        self.assertEqual(result["level"], "low")
        self.assertEqual(confidence("Not yet reproduced. ```python\nprint('example that is not an execution result')\n``` CUDA 13 RuntimeError")['level'], 'low')

    def test_tooling_is_not_a_production_p0(self):
        self.assertEqual(priority("[Simulator] Enable multi-turn session", "The simulator previously corrupted its own cache state.", "pr")[0], "P3")

    def test_editorial_keeps_source_and_does_not_hide_current_body(self):
        item = fixture(38494)
        note = {"priority":"P0", "summary":"Reviewed summary", "reviewed_at":"2026-09-10"}
        entry = to_entry(item, {"38494":note})
        self.assertEqual(entry["summary"], note["summary"])
        self.assertIn("pending", entry["body"])
        self.assertEqual(entry["editorial"], note)

    def test_atomic_publish_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            atomic_json(path, {"entries":[38485]})
            self.assertEqual(json.loads(path.read_text()), {"entries":[38485]})
            atomic_json(path, {"entries":[38494]})
            self.assertEqual(json.loads(path.read_text()), {"entries":[38494]})
            self.assertFalse(path.with_name(path.name + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
