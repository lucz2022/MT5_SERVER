from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from publish_technical_gate import (
    PUBLISH_PATHS,
    assert_clean,
    publication_reasons,
    push_with_rebase,
    update_failure_counts,
)


def gate(h1: str, *, status: str = "ok", setup: str = "NO_SETUP") -> dict:
    return {
        "common_completed_h1_utc": h1,
        "symbols": [
            {
                "symbol": "GC",
                "analysis_symbol": "XAUUSD",
                "status": status,
                "error": None if status == "ok" else "failed",
                "location": {
                    "setup_status": setup,
                    "candidate_direction": "NEUTRAL",
                    "analysis_required": setup.startswith("SETUP_READY"),
                    "conflict": False,
                    "best_side_alert_level": 0,
                }
                if status == "ok"
                else None,
            }
        ],
    }


class TechnicalGatePublisherTests(unittest.TestCase):
    def test_new_completed_h1_is_material(self):
        reasons = publication_reasons(gate("2026-08-18T11:00:00Z"), gate("2026-08-18T12:00:00Z"), [])
        self.assertTrue(any(reason.startswith("completed_h1:") for reason in reasons))

    def test_generated_at_only_change_is_not_material(self):
        old = gate("2026-08-18T12:00:00Z")
        new = gate("2026-08-18T12:00:00Z")
        old["generated_at_utc"] = "2026-08-18T13:00:00Z"
        new["generated_at_utc"] = "2026-08-18T13:05:00Z"
        self.assertEqual(publication_reasons(old, new, []), [])

    def test_setup_transition_is_material(self):
        reasons = publication_reasons(
            gate("2026-08-18T12:00:00Z"),
            gate("2026-08-18T12:00:00Z", setup="SETUP_READY_LONG"),
            [],
        )
        self.assertIn("state_change:XAUUSD", reasons)

    def test_failure_threshold_fires_at_three_and_resets_on_recovery(self):
        failed = gate("2026-08-18T12:00:00Z", status="error")
        counts, threshold = update_failure_counts(failed, {"consecutive_failures": {"XAUUSD": 2}})
        self.assertEqual(counts["XAUUSD"], 3)
        self.assertEqual(threshold, ["XAUUSD"])
        self.assertNotIn("consecutive_failure_count", failed["symbols"][0])
        counts, threshold = update_failure_counts(failed, {"consecutive_failures": counts})
        self.assertEqual(counts["XAUUSD"], 4)
        self.assertEqual(threshold, [])
        recovered = gate("2026-08-18T12:00:00Z")
        counts, threshold = update_failure_counts(recovered, {"consecutive_failures": counts})
        self.assertEqual(counts["XAUUSD"], 0)
        self.assertEqual(threshold, [])

    @patch("publish_technical_gate.git_output", return_value="")
    def test_clean_check_is_scoped_to_publish_paths(self, mocked_git_output):
        assert_clean(Path("repo"))
        mocked_git_output.assert_called_once_with(
            Path("repo"),
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            *PUBLISH_PATHS,
        )

    @patch("publish_technical_gate.run")
    def test_push_does_not_rebase_when_direct_publish_succeeds(self, mocked_run):
        mocked_run.return_value.returncode = 0
        push_with_rebase(Path("repo"))
        mocked_run.assert_called_once_with(
            ["git", "push", "origin", "HEAD:main"],
            cwd=Path("repo"),
            check=False,
        )


if __name__ == "__main__":
    unittest.main()
