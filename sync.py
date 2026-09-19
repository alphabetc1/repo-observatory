"""Refresh a complete public snapshot; never publish a partial GitHub scan."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import logging
import sys
from pathlib import Path

from cacheboard import GitHub, atomic_json, months_ago, normalize, now, stamp, to_entry
from cacheboard import REPO
from settings import CONFIG
from analysis import load_analyzer, analyze_entry

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("cache-observatory")


def build_snapshot(records: dict, editorial: dict, started: dt.datetime, stats: dict) -> dict:
    cutoff = months_ago(started, 6)
    entries = []
    for item in records.values():
        if dt.datetime.fromisoformat(item["created_at"].replace("Z", "+00:00")) < cutoff:
            continue
        if entry := to_entry(item, editorial):
            entries.append(entry)
    entries.sort(key=lambda e: (e["priority"], -int(dt.datetime.fromisoformat(e["updated_at"].replace("Z", "+00:00")).timestamp())))
    visible = {entry["number"] for entry in entries}
    for entry in entries:
        entry["related"] = [number for number in entry["related"] if number in visible]
    return {"schema_version": 1, "repository": REPO, "synced_at": stamp(started), "completed_at": stamp(), "next_sync_at": stamp(started.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=3 - started.hour % 3)), "window_start": stamp(cutoff), "entries": entries, "stats": stats, "methodology": {"scope": f"{REPO}: open issues and PRs created within six calendar months; modules are matched by configured rules.", "priority": "P0 正确性与隔离；P1 可用性与资源释放；P2 性能与容量；P3 能力、设计与观测。人工待办注释优先，其余依据报告症状自动分类，均不代表合并就绪。", "confidence": "issue 置信度衡量报告提供的环境、复现步骤和观测对照是否充分，不是故障概率，不表示我们已经运行复现。", "updates": "每 3 小时读取 GitHub 更新流，包含关闭、合并、重开和修改；页面每分钟检查新快照。同步失败保留最近完整数据并显式提示。", "date_filter": "时间范围按 GitHub 创建时间计算，一周为 7 天，一个月和六个月为日历月。"}}


def refresh(data_dir: Path, bootstrap: bool = False, use_gh: bool = False) -> dict:
    started = now()
    state_file = data_dir / "state.json"
    if state_file.exists() and json.loads(state_file.read_text()).get('repository', REPO) != REPO:
        raise ValueError('Use a separate data directory for each repository')
    previous = json.loads(state_file.read_text()) if state_file.exists() and not bootstrap else {"records": {}, "cursor": None}
    editorial_file = data_dir / "editorial.json"
    editorial = json.loads(editorial_file.read_text()) if editorial_file.exists() else {}
    records = previous["records"].copy()
    client = GitHub(use_gh=use_gh)
    cutoff = months_ago(started, 6)
    since = None
    if previous["cursor"]:
        # The overlapping window and the scan-start cursor cover concurrent updates.
        cursor = dt.datetime.fromisoformat(previous["cursor"].replace("Z", "+00:00"))
        since = stamp(cursor - dt.timedelta(hours=1))
    scanned = 0
    old_ids = {key for key, item in records.items() if to_entry(item, editorial)}
    for page in client.issues(since=since):
        for raw in page:
            item = normalize(raw)
            key = str(item["number"])
            created = dt.datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
            scanned += 1
            if created >= cutoff and item["state"] == "open":
                records[key] = item
            else:
                records.pop(key, None)
        LOG.info("Read %s items in %s pages", scanned, client.calls)
        if not since and dt.datetime.fromisoformat(page[-1]["created_at"].replace("Z", "+00:00")) < cutoff:
            break
    records = {key: item for key, item in records.items() if dt.datetime.fromisoformat(item["created_at"].replace("Z", "+00:00")) >= cutoff}
    new_ids = {key for key, item in records.items() if to_entry(item, editorial)}
    stats = {"scanned_this_run": scanned, "api_calls": client.calls, "new_this_run": len(new_ids - old_ids), "removed_this_run": len(old_ids - new_ids), "sync_mode": "incremental" if since else "full"}
    snapshot = build_snapshot(records, editorial, started, stats)
    if CONFIG.get('analyzer'):
        try:
            analyzer = load_analyzer(CONFIG['analyzer'])
        except Exception:
            analyzer = None
            LOG.warning('Optional analyzer could not be loaded')
        for entry in snapshot['entries']:
            try:
                if analyzer is None:
                    raise ValueError('Analyzer unavailable')
                entry['analysis'] = analyze_entry(entry, analyzer, CONFIG['default_language'])
            except Exception:
                entry['analysis'] = {'status': 'unavailable'}
                LOG.warning('Optional analysis unavailable for %s', entry['id'])
    atomic_json(data_dir / "snapshot.json", snapshot)
    atomic_json(state_file, {"repository": REPO, "cursor": stamp(started), "records": records})
    atomic_json(data_dir / "status.json", {"ok": True, "last_attempt": stamp(started), "last_success": snapshot["completed_at"], "error": None})
    LOG.info("Published %s open entries; %s", len(snapshot["entries"]), stats)
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--bootstrap", action="store_true")
    parser.add_argument("--use-gh", action="store_true", help="Use the local gh login without copying credentials to the server")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args.data_dir.mkdir(parents=True, exist_ok=True)
    with (args.data_dir / "sync.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.info("Another refresh is active")
            return 0
        try:
            refresh(args.data_dir, args.bootstrap, args.use_gh)
        except Exception as error:
            old_status = args.data_dir / "status.json"
            previous = json.loads(old_status.read_text()) if old_status.exists() else {}
            atomic_json(old_status, {"ok": False, "last_attempt": stamp(), "last_success": previous.get("last_success"), "error": str(error)[:500]})
            LOG.exception("Refresh failed; previous complete snapshot retained")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
