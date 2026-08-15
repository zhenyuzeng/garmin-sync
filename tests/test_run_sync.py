"""主同步循环（run_sync）与单条同步（sync_activity）的测试。"""

import io
import zipfile
from unittest.mock import Mock

from garmin_sync import (
    MAX_FAILURES,
    SyncDB,
    fetch_activity_by_id,
    run_sync,
    sync_activity,
)


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("act.fit", b"FITDATA")
    return buf.getvalue()


def _activity(aid: int, name: str = "晨跑") -> dict:
    return {
        "activityId": aid,
        "activityName": name,
        "activityType": {"typeKey": "running"},
        "startTimeLocal": "2026-08-01 07:00:00",
    }


def _ok_clients():
    cn = Mock()
    cn.download_activity.return_value = _zip_bytes()
    intl = Mock()
    intl.upload_activity.return_value = {"detailedImportResult": {"uploadId": 42}}
    return cn, intl


def _db(tmp_path) -> SyncDB:
    return SyncDB.load(tmp_path / "sync.json")


# ── fetch_activity_by_id ─────────────────────────────────────────────

def test_single_activity_notification_shows_name_and_time(tmp_path):
    # 详情接口的真实形状：时间嵌在 summaryDTO 里且为 ISO 格式，类型在 activityTypeDTO 里
    cn, intl = _ok_clients()
    cn.get_activity.return_value = {
        "activityId": 7,
        "activityName": "晨跑",
        "summaryDTO": {"startTimeLocal": "2026-08-01T07:00:00.0"},
        "activityTypeDTO": {"typeKey": "running"},
    }
    db = _db(tmp_path)
    activity = fetch_activity_by_id(cn, 7, "CN")
    result = run_sync(cn, intl, [activity], db, sleep_fn=Mock())
    assert result["synced_names"] == ["晨跑 (08-01 07:00)"]


def test_fetch_activity_by_id_falls_back_to_bare_id_on_error():
    cn = Mock()
    cn.get_activity.side_effect = Exception("404")
    assert fetch_activity_by_id(cn, 7, "CN") == {"activityId": 7}


# ── sync_activity ────────────────────────────────────────────────────

def test_sync_activity_success_marks_synced(tmp_path):
    cn, intl = _ok_clients()
    db = _db(tmp_path)
    assert sync_activity(cn, intl, _activity(1), db) is True
    assert db.is_synced(1)


def test_sync_activity_failure_records_attempt(tmp_path):
    cn, intl = _ok_clients()
    cn.download_activity.side_effect = Exception("网络挂了")
    db = _db(tmp_path)
    assert sync_activity(cn, intl, _activity(1), db) is False
    assert db.data["failed"]["1"]["attempts"] == 1


def test_sync_activity_dry_run_touches_nothing(tmp_path):
    cn, intl = _ok_clients()
    db = _db(tmp_path)
    assert sync_activity(cn, intl, _activity(1), db, dry_run=True) is False
    cn.download_activity.assert_not_called()
    assert db.data["failed"] == {}
    assert db.data["synced"] == {}


# ── run_sync ─────────────────────────────────────────────────────────

def test_run_sync_skips_already_synced(tmp_path):
    cn, intl = _ok_clients()
    db = _db(tmp_path)
    db.mark_synced(1, 42)
    result = run_sync(cn, intl, [_activity(1)], db, sleep_fn=Mock())
    assert result["skipped"] == 1
    cn.download_activity.assert_not_called()


def test_run_sync_skips_permanently_failed(tmp_path):
    cn, intl = _ok_clients()
    db = _db(tmp_path)
    for _ in range(MAX_FAILURES):
        db.record_failure(1, "x")
    result = run_sync(cn, intl, [_activity(1)], db, sleep_fn=Mock())
    assert result["permanent_skipped"] == 1
    assert result["failed_items"] == []
    cn.download_activity.assert_not_called()


def test_run_sync_force_ids_bypasses_permanent_skip(tmp_path):
    cn, intl = _ok_clients()
    db = _db(tmp_path)
    for _ in range(MAX_FAILURES):
        db.record_failure(1, "x")
    result = run_sync(cn, intl, [_activity(1)], db, force_ids={1}, sleep_fn=Mock())
    assert result["synced"] == 1
    cn.download_activity.assert_called_once()


def test_run_sync_counts_success_and_names(tmp_path):
    cn, intl = _ok_clients()
    db = _db(tmp_path)
    result = run_sync(
        cn, intl, [_activity(1, "晨跑"), _activity(2, "骑行")], db, sleep_fn=Mock()
    )
    assert result["synced"] == 2
    assert result["synced_names"] == ["晨跑 (08-01 07:00)", "骑行 (08-01 07:00)"]


def test_run_sync_synced_name_falls_back_when_time_missing(tmp_path):
    cn, intl = _ok_clients()
    db = _db(tmp_path)
    # 详情拉取失败时（如活动已删除），活动只有 ID，没有名称和时间
    result = run_sync(cn, intl, [{"activityId": 3}], db, sleep_fn=Mock())
    assert result["synced_names"] == ["活动 3"]


def test_run_sync_failed_item_plain_below_limit(tmp_path):
    cn, intl = _ok_clients()
    cn.download_activity.side_effect = Exception("x")
    db = _db(tmp_path)
    result = run_sync(cn, intl, [_activity(9, "夜跑")], db, sleep_fn=Mock())
    assert result["failed"] == 1
    assert result["failed_items"] == ["夜跑 (9)"]


def test_run_sync_failed_item_notes_retry_limit_reached(tmp_path):
    cn, intl = _ok_clients()
    db = _db(tmp_path)
    for _ in range(MAX_FAILURES - 1):
        db.record_failure(9, "x")
    cn.download_activity.side_effect = Exception("x")
    result = run_sync(cn, intl, [_activity(9, "夜跑")], db, sleep_fn=Mock())
    assert result["failed"] == 1
    assert "不再自动重试" in result["failed_items"][0]


def test_run_sync_dry_run_does_not_sleep(tmp_path):
    cn, intl = _ok_clients()
    db = _db(tmp_path)
    sleep = Mock()
    result = run_sync(cn, intl, [_activity(1), _activity(2)], db, dry_run=True, sleep_fn=sleep)
    sleep.assert_not_called()
    assert result["synced"] == 0
    assert result["failed"] == 0


def test_run_sync_sleeps_between_real_syncs(tmp_path):
    cn, intl = _ok_clients()
    db = _db(tmp_path)
    sleep = Mock()
    run_sync(cn, intl, [_activity(1), _activity(2)], db, sleep_fn=sleep)
    assert sleep.call_count == 2
