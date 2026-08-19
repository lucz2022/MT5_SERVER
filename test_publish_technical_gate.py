from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import publish_technical_gate as publish_technical_gate_main_module
from publish_technical_gate import (
    BEIJING,
    PUBLISH_PATHS,
    assert_clean,
    gate_requires_analysis,
    launch_analysis,
    notify_publisher_status,
    publication_reasons,
    push_with_rebase,
    sync_with_rebase,
    update_failure_counts,
    within_analysis_window,
    write_skip_state,
)


def publisher_main(args):
    return publish_technical_gate_main_module.main(args)


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

    @patch("publish_technical_gate.run")
    def test_sync_rebases_with_autostash_and_recovers_after_failure(self, mocked_run):
        failed = type("Result", (), {"returncode": 1})()
        succeeded = type("Result", (), {"returncode": 0})()
        mocked_run.side_effect = [failed, succeeded, succeeded]
        sync_with_rebase(Path("repo"))
        self.assertEqual(mocked_run.call_count, 3)
        self.assertEqual(
            mocked_run.call_args_list[0].args[0],
            ["git", "pull", "--rebase", "--autostash", "origin", "main"],
        )
        self.assertEqual(
            mocked_run.call_args_list[1].args[0],
            ["git", "rebase", "--abort"],
        )
        self.assertEqual(
            mocked_run.call_args_list[2].args[0],
            ["git", "pull", "--rebase", "--autostash", "origin", "main"],
        )

    def test_analysis_window_boundaries_follow_beijing_weekday_and_hour(self):
        self.assertFalse(within_analysis_window(datetime(2026, 8, 19, 6, 59, tzinfo=BEIJING)))
        self.assertTrue(within_analysis_window(datetime(2026, 8, 19, 7, 0, tzinfo=BEIJING)))
        self.assertTrue(within_analysis_window(datetime(2026, 8, 19, 23, 30, tzinfo=BEIJING)))
        self.assertFalse(within_analysis_window(datetime(2026, 8, 19, 0, 5, tzinfo=BEIJING)))
        self.assertTrue(within_analysis_window(datetime(2026, 8, 17, 7, 5, tzinfo=BEIJING)))
        self.assertTrue(within_analysis_window(datetime(2026, 8, 21, 23, 5, tzinfo=BEIJING)))
        self.assertFalse(within_analysis_window(datetime(2026, 8, 22, 7, 5, tzinfo=BEIJING)))
        self.assertFalse(within_analysis_window(datetime(2026, 8, 23, 12, 0, tzinfo=BEIJING)))

    def test_skip_state_merges_round_payload_and_previous_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            write_skip_state(
                state_path,
                {
                    "consecutive_failures": {"XAUUSD": 1},
                    "last_completed_h1_utc": "2026-08-18T22:00:00Z",
                },
                {"last_completed_h1_utc": "2026-08-18T15:00:00Z", "publisher_failure_count": 2},
            )
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["publisher_status"], "skipped_out_of_analysis_window")
            self.assertIsNone(state["publisher_error"])
            self.assertEqual(state["last_completed_h1_utc"], "2026-08-18T22:00:00Z")
            self.assertEqual(state["publisher_failure_count"], 2)
            self.assertEqual(state["consecutive_failures"], {"XAUUSD": 1})

    def test_out_of_window_round_runs_producer_but_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            old_gate = {"common_completed_h1_utc": "2026-08-18T15:00:00Z", "symbols": []}
            for relative in PUBLISH_PATHS:
                target = repo / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(old_gate), encoding="utf-8")
            producer = root / "producer.py"
            producer.write_text(
                "import json, sys\n"
                "from pathlib import Path\n"
                "repo = Path(sys.argv[sys.argv.index('--repo-root') + 1])\n"
                "gate = json.loads((repo / 'runtime/technical_gate/technical_triggers.json').read_text())\n"
                "gate['common_completed_h1_utc'] = '2026-08-18T23:00:00Z'\n"
                "(repo / 'runtime/technical_gate/technical_triggers.json').write_text(json.dumps(gate))\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)
            state_path = root / "state.json"

            args = argparse.Namespace(
                repo_root=repo,
                producer=producer,
                output_dir=root / "output",
                state_file=state_path,
                force_publish=False,
                no_pull=True,
                no_push=True,
                no_wework=True,
                no_analysis=True,
            )
            with patch("publish_technical_gate.within_analysis_window", return_value=False):
                exit_code = publisher_main(args)

            self.assertEqual(exit_code, 0)
            commits = subprocess.run(
                ["git", "rev-list", "--count", "HEAD"],
                cwd=repo, text=True, capture_output=True, check=True,
            ).stdout.strip()
            self.assertEqual(commits, "1")
            restored = json.loads(
                (repo / "runtime/technical_gate/technical_triggers.json").read_text(encoding="utf-8")
            )
            self.assertEqual(restored["common_completed_h1_utc"], "2026-08-18T15:00:00Z")
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["publisher_status"], "skipped_out_of_analysis_window")
            self.assertEqual(state["last_completed_h1_utc"], "2026-08-18T23:00:00Z")

    def test_gate_requires_analysis_true_when_an_ok_symbol_flags_it(self):
        self.assertTrue(gate_requires_analysis(gate("2026-08-18T12:00:00Z", setup="SETUP_READY_LONG")))

    def test_gate_requires_analysis_false_when_no_symbol_flags_it(self):
        self.assertFalse(gate_requires_analysis(gate("2026-08-18T12:00:00Z")))

    def test_gate_requires_analysis_ignores_errored_symbols(self):
        errored = gate("2026-08-18T12:00:00Z", status="error")
        self.assertFalse(gate_requires_analysis(errored))

    def test_gate_requires_analysis_handles_empty_gate(self):
        self.assertFalse(gate_requires_analysis({}))

    @patch("publish_technical_gate.subprocess.Popen")
    def test_launch_analysis_spawns_detached_and_does_not_wait(self, mocked_popen):
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            (repo / "automation").mkdir()
            (repo / "automation" / "run_local_analysis.py").write_text("", encoding="utf-8")
            launch_analysis(repo)
            mocked_popen.assert_called_once()
            command = mocked_popen.call_args.args[0]
            self.assertEqual(command[0], sys.executable)
            self.assertIn(str(repo / "automation" / "run_local_analysis.py"), command)
            self.assertIn("--repo-root", command)
            self.assertIn(str(repo), command)
            mocked_popen.return_value.wait.assert_not_called()
            # Without redirection, a detached child with no console loses all its
            # print() output -- it must not go to that dark hole. See docstring.
            kwargs = mocked_popen.call_args.kwargs
            self.assertIsNotNone(kwargs.get("stdout"))
            self.assertEqual(kwargs.get("stderr"), subprocess.STDOUT)
            self.assertTrue((repo / "runtime" / "logs" / "local_analysis.log").is_file())

    @patch("publish_technical_gate.subprocess.Popen")
    def test_launch_analysis_appends_across_multiple_launches(self, mocked_popen):
        """The log must accumulate across hourly launches, not truncate each time."""
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir)
            (repo / "automation").mkdir()
            (repo / "automation" / "run_local_analysis.py").write_text("", encoding="utf-8")
            log_path = repo / "runtime" / "logs" / "local_analysis.log"
            log_path.parent.mkdir(parents=True)
            log_path.write_text("previous run marker\n", encoding="utf-8")
            launch_analysis(repo)
            self.assertIn("previous run marker", log_path.read_text(encoding="utf-8"))

    @patch("publish_technical_gate.subprocess.Popen")
    def test_launch_analysis_warns_and_skips_when_script_missing(self, mocked_popen):
        with tempfile.TemporaryDirectory() as temp_dir:
            launch_analysis(Path(temp_dir))
        mocked_popen.assert_not_called()

    def _prepare_real_publish_repo(self, root: Path, *, analysis_required: bool) -> tuple[Path, Path]:
        repo = root / "repo"
        repo.mkdir()
        old_gate = {"common_completed_h1_utc": "2026-08-18T15:00:00Z", "symbols": []}
        for relative in PUBLISH_PATHS:
            target = repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(old_gate), encoding="utf-8")
        new_gate = {
            "common_completed_h1_utc": "2026-08-18T23:00:00Z",
            "symbols": [
                {
                    "symbol": "GC",
                    "analysis_symbol": "XAUUSD",
                    "status": "ok",
                    "error": None,
                    "location": {
                        "setup_status": "SETUP_READY_LONG",
                        "candidate_direction": "LONG",
                        "analysis_required": analysis_required,
                        "conflict": False,
                        "best_side_alert_level": 2,
                    },
                }
            ],
        }
        producer = root / "producer.py"
        producer.write_text(
            "import json, sys\n"
            "from pathlib import Path\n"
            "repo = Path(sys.argv[sys.argv.index('--repo-root') + 1])\n"
            f"gate = {new_gate!r}\n"
            "(repo / 'runtime/technical_gate/technical_triggers.json').write_text(json.dumps(gate))\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)
        return repo, producer

    def test_real_publish_launches_analysis_when_gate_requires_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo, producer = self._prepare_real_publish_repo(root, analysis_required=True)
            args = argparse.Namespace(
                repo_root=repo,
                producer=producer,
                output_dir=root / "output",
                state_file=root / "state.json",
                force_publish=False,
                no_pull=True,
                no_push=True,
                no_wework=True,
                no_analysis=False,
            )
            with patch("publish_technical_gate.within_analysis_window", return_value=True), \
                 patch("publish_technical_gate.launch_analysis") as mocked_launch:
                exit_code = publisher_main(args)
            self.assertEqual(exit_code, 0)
            mocked_launch.assert_called_once_with(repo)

    def test_real_publish_skips_analysis_when_gate_does_not_require_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo, producer = self._prepare_real_publish_repo(root, analysis_required=False)
            args = argparse.Namespace(
                repo_root=repo,
                producer=producer,
                output_dir=root / "output",
                state_file=root / "state.json",
                force_publish=False,
                no_pull=True,
                no_push=True,
                no_wework=True,
                no_analysis=False,
            )
            with patch("publish_technical_gate.within_analysis_window", return_value=True), \
                 patch("publish_technical_gate.launch_analysis") as mocked_launch:
                exit_code = publisher_main(args)
            self.assertEqual(exit_code, 0)
            mocked_launch.assert_not_called()

    def test_real_publish_skips_analysis_when_no_analysis_flag_set(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo, producer = self._prepare_real_publish_repo(root, analysis_required=True)
            args = argparse.Namespace(
                repo_root=repo,
                producer=producer,
                output_dir=root / "output",
                state_file=root / "state.json",
                force_publish=False,
                no_pull=True,
                no_push=True,
                no_wework=True,
                no_analysis=True,
            )
            with patch("publish_technical_gate.within_analysis_window", return_value=True), \
                 patch("publish_technical_gate.launch_analysis") as mocked_launch:
                exit_code = publisher_main(args)
            self.assertEqual(exit_code, 0)
            mocked_launch.assert_not_called()

    @patch.dict(
        "os.environ",
        {"WEWORK_WEBHOOK_URL": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test"},
        clear=True,
    )
    @patch("publish_technical_gate.urllib.request.urlopen")
    def test_publisher_notification_uses_producer_webhook_name(self, mocked_urlopen):
        response = mocked_urlopen.return_value.__enter__.return_value
        response.read.return_value = b'{"errcode":0,"errmsg":"ok"}'
        notify_publisher_status("test")
        request = mocked_urlopen.call_args.args[0]
        self.assertIn("qyapi.weixin.qq.com", request.full_url)


if __name__ == "__main__":
    unittest.main()
