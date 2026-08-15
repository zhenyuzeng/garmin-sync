"""SyncDB（同步状态层）与进程锁的测试。"""

import json
from pathlib import Path

from garmin_sync import MAX_FAILURES, SyncDB, try_acquire_lock


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


# ── 加载与结构校验 ──────────────────────────────────────────────────

def test_load_missing_file_gives_empty_structure(tmp_path):
    db = SyncDB.load(tmp_path / "sync.json")
    assert db.data == {"synced": {}, "failed": {}, "last_sync": None}


def test_load_roundtrip(tmp_path):
    p = tmp_path / "sync.json"
    _write(p, {"synced": {"1": {"intl_activity_id": 2, "synced_at": "t"}},
               "failed": {}, "last_sync": "t"})
    db = SyncDB.load(p)
    assert db.is_synced(1)
    assert not db.is_synced(999)


def test_load_old_format_without_failed_key(tmp_path):
    # 向后兼容：旧版文件没有 "failed" 键
    p = tmp_path / "sync.json"
    _write(p, {"synced": {"1": {"intl_activity_id": 2, "synced_at": "t"}}, "last_sync": "t"})
    db = SyncDB.load(p)
    assert db.is_synced(1)
    assert db.data["failed"] == {}


def test_load_corrupt_json_backs_up_and_resets(tmp_path):
    p = tmp_path / "sync.json"
    p.write_text("{oops")
    db = SyncDB.load(p)
    assert db.data["synced"] == {}
    backups = list(tmp_path.glob("sync.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == "{oops"
    assert not p.exists()  # 原文件已改名备份


def test_load_valid_json_wrong_structure_backs_up(tmp_path):
    p = tmp_path / "sync.json"
    _write(p, ["not", "a", "dict"])
    db = SyncDB.load(p)
    assert db.data["synced"] == {}
    assert len(list(tmp_path.glob("sync.json.corrupt-*"))) == 1


def test_load_missing_synced_key_backs_up(tmp_path):
    p = tmp_path / "sync.json"
    _write(p, {"something": 1})
    db = SyncDB.load(p)
    assert db.data["synced"] == {}
    assert len(list(tmp_path.glob("sync.json.corrupt-*"))) == 1


# ── 保存（原子写）──────────────────────────────────────────────────

def test_save_then_load_roundtrip(tmp_path):
    p = tmp_path / "sync.json"
    db = SyncDB.load(p)
    db.mark_synced(123, 456)
    db2 = SyncDB.load(p)
    assert db2.is_synced(123)
    assert db2.data["synced"]["123"]["intl_activity_id"] == 456
    assert db2.data["last_sync"] is not None


def test_save_leaves_no_tmp_file(tmp_path):
    p = tmp_path / "sync.json"
    db = SyncDB.load(p)
    db.save()
    assert not (tmp_path / "sync.json.tmp").exists()


# ── 失败计数与永久跳过 ──────────────────────────────────────────────

def test_mark_synced_clears_failure_record(tmp_path):
    db = SyncDB.load(tmp_path / "s.json")
    db.record_failure(1, "boom")
    db.mark_synced(1, 2)
    assert "1" not in db.data["failed"]


def test_record_failure_counts_attempts(tmp_path):
    db = SyncDB.load(tmp_path / "s.json")
    assert db.record_failure(1, "e1") == 1
    assert db.record_failure(1, "e2") == 2
    assert db.data["failed"]["1"]["last_error"] == "e2"
    assert db.data["failed"]["1"]["last_attempt"] is not None


def test_is_permanently_failed_threshold(tmp_path):
    db = SyncDB.load(tmp_path / "s.json")
    for _ in range(MAX_FAILURES - 1):
        db.record_failure(1, "x")
    assert not db.is_permanently_failed(1)
    db.record_failure(1, "x")
    assert db.is_permanently_failed(1)


def test_failure_state_persists_across_load(tmp_path):
    p = tmp_path / "s.json"
    db = SyncDB.load(p)
    db.record_failure(7, "x")
    assert SyncDB.load(p).data["failed"]["7"]["attempts"] == 1


# ── 进程锁 ─────────────────────────────────────────────────────────

def test_lock_excludes_second_holder(tmp_path):
    p = tmp_path / "sync.lock"
    first = try_acquire_lock(p)
    assert first is not None
    second = try_acquire_lock(p)
    assert second is None  # 已被占用
    first.close()          # 释放后可以重新获取
    third = try_acquire_lock(p)
    assert third is not None
    third.close()
