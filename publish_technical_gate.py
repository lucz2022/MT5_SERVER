from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_REPO = ROOT / "mt5-gpt-data"
DEFAULT_PRODUCER = ROOT / "xauusd_technical_analysis_chart_v2_1.py"
DEFAULT_OUTPUT = ROOT / "ibkr_technical_runtime"
DEFAULT_STATE = DEFAULT_OUTPUT / "gate_publish_state.json"
BEIJING = timezone(timedelta(hours=8))
ANALYSIS_WINDOW_START_BEIJING_HOUR = 7
PUBLISH_PATHS = (
    "runtime/technical_gate/technical_triggers.json",
    "latest/technical_gate.json",
    "latest/technical_klines.json",
)


def within_analysis_window(now: datetime | None = None) -> bool:
    """The analysis workflow consumes the Gate on Beijing weekdays 07:20-23:20."""
    moment = (now or datetime.now(UTC)).astimezone(BEIJING)
    return moment.weekday() < 5 and moment.hour >= ANALYSIS_WINDOW_START_BEIJING_HOUR


def run(command: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    print("[RUN]", subprocess.list2cmdline(command), flush=True)
    return subprocess.run(command, cwd=cwd, text=True, encoding="utf-8", check=check)


def git_output(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, encoding="utf-8", capture_output=True, check=True
    )
    return result.stdout.rstrip("\r\n")


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def gate_items(gate: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_items = gate.get("symbols") or gate.get("triggers") or []
    return {
        str(item.get("analysis_symbol") or item.get("symbol")): item
        for item in raw_items
        if isinstance(item, dict) and (item.get("analysis_symbol") or item.get("symbol"))
    }


def material_signature(item: dict[str, Any] | None) -> tuple[Any, ...]:
    if not isinstance(item, dict):
        return (None,)
    location = item.get("location") if isinstance(item.get("location"), dict) else {}
    return (
        item.get("status"),
        bool(item.get("error")),
        location.get("setup_status"),
        location.get("candidate_direction"),
        location.get("analysis_required"),
        location.get("conflict"),
        location.get("best_side_alert_level"),
    )


def update_failure_counts(
    gate: dict[str, Any], previous_state: dict[str, Any]
) -> tuple[dict[str, int], list[str]]:
    prior = previous_state.get("consecutive_failures", {})
    if not isinstance(prior, dict):
        prior = {}
    counts: dict[str, int] = {}
    newly_at_threshold: list[str] = []
    for symbol, item in gate_items(gate).items():
        failed = str(item.get("status") or "").casefold() != "ok" or bool(item.get("error"))
        count = int(prior.get(symbol) or 0) + 1 if failed else 0
        counts[symbol] = count
        if failed and count == 3:
            newly_at_threshold.append(symbol)
    return counts, newly_at_threshold


def publication_reasons(
    old_gate: dict[str, Any],
    new_gate: dict[str, Any],
    failure_threshold_symbols: list[str],
    *,
    force: bool = False,
) -> list[str]:
    reasons: list[str] = []
    if force:
        reasons.append("forced")
    old_h1 = old_gate.get("common_completed_h1_utc")
    new_h1 = new_gate.get("common_completed_h1_utc")
    if old_h1 != new_h1:
        reasons.append(f"completed_h1:{old_h1}->{new_h1}")
    old_items = gate_items(old_gate)
    new_items = gate_items(new_gate)
    for symbol in sorted(old_items.keys() | new_items.keys()):
        if material_signature(old_items.get(symbol)) != material_signature(new_items.get(symbol)):
            reasons.append(f"state_change:{symbol}")
    for symbol in sorted(failure_threshold_symbols):
        reasons.append(f"failure_threshold:{symbol}")
    return reasons


def assert_clean(repo: Path) -> None:
    status = git_output(
        repo,
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        *PUBLISH_PATHS,
    )
    if status:
        raise RuntimeError(
            "Technical Gate publish paths have pre-existing changes; refusing scheduled publish:\n"
            + status
        )


def changed_paths(repo: Path) -> set[str]:
    status = git_output(repo, "status", "--porcelain", "--untracked-files=all")
    paths: set[str] = set()
    for line in status.splitlines():
        if not line:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.add(path.replace("\\", "/"))
    return paths


def sync_with_rebase(repo: Path, *, attempts: int = 3) -> None:
    """Rebase local commits onto origin/main while tolerating unrelated dirty files."""
    for attempt in range(1, attempts + 1):
        pulled = run(
            ["git", "pull", "--rebase", "--autostash", "origin", "main"],
            cwd=repo,
            check=False,
        )
        if pulled.returncode == 0:
            return
        run(["git", "rebase", "--abort"], cwd=repo, check=False)
        print(f"[RETRY] sync rebase {attempt}/{attempts}", flush=True)
    raise RuntimeError("could not sync Technical Gate workspace onto origin/main")


def push_with_rebase(repo: Path, *, attempts: int = 3) -> None:
    """Publish safely when the independent feed updates main during IBKR collection."""
    for attempt in range(1, attempts + 1):
        pushed = run(["git", "push", "origin", "HEAD:main"], cwd=repo, check=False)
        if pushed.returncode == 0:
            return
        print(f"[RETRY] push race {attempt}/{attempts}", flush=True)
        if attempt == attempts:
            break
        pulled = run(["git", "pull", "--rebase", "origin", "main"], cwd=repo, check=False)
        if pulled.returncode != 0:
            run(["git", "rebase", "--abort"], cwd=repo, check=False)
            raise RuntimeError("could not rebase Technical Gate commit onto origin/main")
    raise RuntimeError("could not push Technical Gate after feed-update retries")


def notify_publisher_status(message: str, *, enabled: bool = True) -> None:
    if not enabled:
        return
    webhook = (
        os.getenv("WEWORK_WEBHOOK_URL", "").strip()
        or os.getenv("WE_COM_WEBHOOK_URL", "").strip()
    )
    if not webhook:
        print(
            "[WEWORK] WEWORK_WEBHOOK_URL/WE_COM_WEBHOOK_URL not set; "
            "publisher status notification skipped"
        )
        return
    payload = json.dumps(
        {"msgtype": "markdown", "markdown": {"content": message}},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        webhook,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
        if int(result.get("errcode", -1)) != 0:
            raise RuntimeError(str(result))
        print("[WEWORK] publisher status notification sent")
    except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
        print(f"[WEWORK] publisher status notification failed: {exc}", file=sys.stderr)


def write_success_state(
    path: Path,
    payload: dict[str, Any],
    previous_state: dict[str, Any],
    *,
    notify: bool,
) -> None:
    recovered = previous_state.get("publisher_status") == "error"
    payload["publisher_status"] = "ok"
    payload["publisher_error"] = None
    payload["publisher_failure_count"] = 0
    write_json(path, payload)
    if recovered:
        notify_publisher_status(
            "✅ Technical Gate 发布器已恢复\n> 取数、提交与推送链路恢复正常。",
            enabled=notify,
        )


def write_skip_state(
    path: Path,
    payload: dict[str, Any],
    previous_state: dict[str, Any],
) -> None:
    """Record an out-of-window round: the producer ran locally, nothing was published."""
    merged = dict(previous_state) if isinstance(previous_state, dict) else {}
    merged.update(payload)
    merged.update(
        {
            "updated_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "publisher_status": "skipped_out_of_analysis_window",
            "publisher_error": None,
        }
    )
    write_json(path, merged)


def record_publisher_failure(path: Path, error: Exception, *, notify: bool) -> None:
    state = load_json(path, {})
    if not isinstance(state, dict):
        state = {}
    state.update(
        {
            "updated_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "publisher_status": "error",
            "publisher_error": str(error),
            "publisher_failure_count": int(state.get("publisher_failure_count") or 0) + 1,
        }
    )
    write_json(path, state)
    notify_publisher_status(
        "⚠️ Technical Gate 发布失败\n"
        f"> `{type(error).__name__}: {error}`\n"
        "> 本轮未强推；请检查计划任务与共享工作区。",
        enabled=notify,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate and conditionally publish Technical Gate")
    parser.add_argument("--repo-root", type=Path, default=DEFAULT_REPO)
    parser.add_argument("--producer", type=Path, default=DEFAULT_PRODUCER)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--force-publish", action="store_true",
                        help="publish even without material change or outside the analysis window")
    parser.add_argument("--no-pull", action="store_true")
    parser.add_argument("--no-push", action="store_true")
    parser.add_argument("--no-wework", action="store_true")
    return parser


def main(args: argparse.Namespace | None = None) -> int:
    args = args or build_parser().parse_args()
    repo = args.repo_root.resolve()
    producer = args.producer.resolve()
    state_path = args.state_file.resolve()
    if not (repo / ".git").exists():
        raise SystemExit(f"[FATAL] not a Git repository: {repo}")
    if not producer.is_file():
        raise SystemExit(f"[FATAL] producer missing: {producer}")

    assert_clean(repo)
    if not args.no_pull:
        sync_with_rebase(repo)
    assert_clean(repo)
    baseline_changes = changed_paths(repo)

    authoritative = repo / PUBLISH_PATHS[0]
    old_gate = load_json(authoritative, {})
    previous_state = load_json(state_path, {})
    command = [
        sys.executable,
        "-X",
        "utf8",
        "-u",
        str(producer),
        "--repo-root",
        str(repo),
        "--output-dir",
        str(args.output_dir.resolve()),
    ]
    if args.no_wework:
        command.append("--no-wework")
    producer_result = run(command, cwd=ROOT, check=False)
    if not authoritative.is_file():
        raise RuntimeError("producer did not write the authoritative Technical Gate")

    new_gate = load_json(authoritative, {})
    if not isinstance(new_gate, dict):
        raise RuntimeError("producer wrote an invalid Technical Gate")
    counts, threshold_symbols = update_failure_counts(new_gate, previous_state)
    write_json(authoritative, new_gate)
    write_json(repo / PUBLISH_PATHS[1], new_gate)

    changed = changed_paths(repo)
    unexpected = (changed - baseline_changes) - set(PUBLISH_PATHS)
    if unexpected:
        raise RuntimeError(f"producer changed unexpected repository paths: {sorted(unexpected)}")
    publish_changes = changed & set(PUBLISH_PATHS)
    reasons = publication_reasons(
        old_gate, new_gate, threshold_symbols, force=args.force_publish
    )
    state_payload = {
        "updated_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "consecutive_failures": counts,
        "last_gate_status": new_gate.get("status"),
        "last_completed_h1_utc": new_gate.get("common_completed_h1_utc"),
    }

    if not reasons:
        if publish_changes:
            run(["git", "restore", "--", *PUBLISH_PATHS], cwd=repo)
        write_success_state(
            state_path, state_payload, previous_state, notify=not args.no_wework
        )
        print("[SKIP] no material Gate change")
        return 0 if producer_result.returncode == 0 else producer_result.returncode

    if not args.force_publish and not within_analysis_window():
        if publish_changes:
            run(["git", "restore", "--", *PUBLISH_PATHS], cwd=repo)
        write_skip_state(state_path, state_payload, previous_state)
        print(
            "[SKIP] outside analysis window (Beijing overnight/weekend); "
            "Gate produced locally and alerts delivered, nothing published"
        )
        return 0 if producer_result.returncode == 0 else producer_result.returncode

    print("[PUBLISH] " + ", ".join(reasons))
    if not publish_changes:
        write_success_state(
            state_path, state_payload, previous_state, notify=not args.no_wework
        )
        print("[SKIP] material reason exists but tracked payload is unchanged")
        return 0 if producer_result.returncode == 0 else producer_result.returncode
    run(["git", "add", "--", *PUBLISH_PATHS], cwd=repo)
    h1_label = str(new_gate.get("common_completed_h1_utc") or "failure-state")
    run(
        [
            "git",
            "commit",
            "--only",
            "-m",
            f"Update technical gate {h1_label}",
            "--",
            *PUBLISH_PATHS,
        ],
        cwd=repo,
    )
    if not args.no_push:
        push_with_rebase(repo)
    write_success_state(
        state_path, state_payload, previous_state, notify=not args.no_wework
    )
    print("[DONE] Technical Gate published")
    return 0 if producer_result.returncode == 0 else producer_result.returncode


if __name__ == "__main__":
    parsed_args = build_parser().parse_args()
    try:
        raise SystemExit(main(parsed_args))
    except Exception as exc:
        record_publisher_failure(
            parsed_args.state_file.resolve(), exc, notify=not parsed_args.no_wework
        )
        print(f"[FATAL] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
