import logging
import subprocess
import urllib.request
from unittest.mock import MagicMock, patch

from garmin_sync import notify


def test_macos_escape_quotes():
    assert notify._macos_escape('say "hi"') == 'say \\"hi\\"'


def test_join_items_full_list():
    # 用户要求完整列出所有被同步的记录，不截断
    items = ["a", "b", "c", "d", "e"]
    assert notify._join_items(items) == "a、b、c、d、e"


def test_join_items_single():
    assert notify._join_items(["a"]) == "a"


def test_decide_failure_wins_over_success():
    assert notify.decide_notification(["bad (1)"], ["good (2)"]) == "failure"


def test_decide_success():
    assert notify.decide_notification([], ["good (2)"]) == "success"


def test_decide_silent_when_all_skipped():
    assert notify.decide_notification([], []) is None


def test_notify_success_sends_both_channels():
    with (
        patch.object(notify, "_send_macos") as macos,
        patch.object(notify, "_send_bark") as bark,
    ):
        notify.notify_success(["Easy run"])
        macos.assert_called_once()
        bark.assert_called_once()


def test_notify_success_lists_all_activities():
    with (
        patch.object(notify, "_send_macos") as macos,
        patch.object(notify, "_send_bark") as bark,
    ):
        notify.notify_success(["Run A", "Run B", "Run C", "Run D"])
        message = macos.call_args.args[1]
        assert "Run A" in message and "Run D" in message
        assert "4 条活动" in message
        bark.assert_called_once()


def test_notify_success_empty_noop():
    with (
        patch.object(notify, "_send_macos") as macos,
        patch.object(notify, "_send_bark") as bark,
    ):
        notify.notify_success([])
        macos.assert_not_called()
        bark.assert_not_called()


def test_notify_failure_sends_both_channels():
    with (
        patch.object(notify, "_send_macos") as macos,
        patch.object(notify, "_send_bark") as bark,
    ):
        notify.notify_failure(["Easy run (123)"])
        macos.assert_called_once()
        bark.assert_called_once()


def test_notify_login_failure_sends_both_channels():
    with (
        patch.object(notify, "_send_macos") as macos,
        patch.object(notify, "_send_bark") as bark,
    ):
        notify.notify_login_failure("boom")
        macos.assert_called_once()
        bark.assert_called_once()


def test_send_bark_skips_without_api(monkeypatch):
    monkeypatch.delenv("BARK_API", raising=False)
    with patch.object(notify, "_bark_opener") as opener:
        notify._send_bark("t", "m")
        opener.assert_not_called()


def test_send_bark_reads_env_at_call_time(monkeypatch):
    # 回归测试：BARK_API 在 notify import 之后才写入环境（.env 晚于 import 加载）也必须生效
    monkeypatch.delenv("BARK_API", raising=False)
    notify._send_bark("t", "m")  # 无 key 时静默跳过，不抛错
    monkeypatch.setenv("BARK_API", "https://api.day.app/key")
    with patch.object(notify, "_bark_opener", return_value=MagicMock()) as opener:
        notify._send_bark("标题", "内容")
        opener.return_value.open.assert_called_once()


def test_send_bark_url_encoding(monkeypatch):
    monkeypatch.setenv("BARK_API", "https://api.day.app/key")
    with patch.object(notify, "_bark_opener", return_value=MagicMock()) as opener:
        notify._send_bark("标题", "内容")
        opener.return_value.open.assert_called_once_with(
            "https://api.day.app/key/%E6%A0%87%E9%A2%98/%E5%86%85%E5%AE%B9",
            timeout=5,
        )


def test_send_bark_escapes_slash_in_title(monkeypatch):
    # 回归测试：活动名含 / 时，若用 quote 默认 safe='/'，会破坏 Bark 的 /key/title/body 路径结构
    monkeypatch.setenv("BARK_API", "https://api.day.app/key")
    with patch.object(notify, "_bark_opener", return_value=MagicMock()) as opener:
        notify._send_bark("晨跑/恢复", "内容")
        url = opener.return_value.open.call_args.args[0]
        title_part = url.split("/key/")[1].split("/")[0]
        assert "/" not in title_part
        assert "%2F" in url


def test_send_bark_retries_direct_path_then_succeeds(monkeypatch):
    # 直连抖动一次后恢复（如 2026-08-13 23:13 握手超时）：应重试并最终送达，不碰系统代理
    monkeypatch.setenv("BARK_API", "https://api.day.app/key")
    direct = MagicMock()
    direct.open.side_effect = [OSError("握手超时"), MagicMock()]
    sleep = MagicMock()
    with (
        patch.object(notify, "_bark_opener", return_value=direct),
        patch.object(notify, "_proxy_opener") as proxy,
    ):
        notify._send_bark("t", "m", sleep_fn=sleep)
    assert direct.open.call_count == 2
    sleep.assert_called_once_with(notify.BARK_RETRY_DELAY)
    proxy.assert_not_called()


def test_send_bark_falls_back_to_system_proxy(monkeypatch):
    # 直连路径全部失败但代理活着（2026-08-13 23:13 的真实场景：
    # garmin.com 经代理上传成功、直连 Bark 超时）→ 走系统代理兜底送达
    monkeypatch.setenv("BARK_API", "https://api.day.app/key")
    direct = MagicMock()
    direct.open.side_effect = OSError("直连超时")
    proxy = MagicMock()
    with (
        patch.object(notify, "_bark_opener", return_value=direct),
        patch.object(notify, "_proxy_opener", return_value=proxy),
    ):
        notify._send_bark("t", "m", sleep_fn=MagicMock())
    assert direct.open.call_count == notify.BARK_ATTEMPTS_PER_PATH
    proxy.open.assert_called_once()


def test_send_bark_all_paths_exhausted_stays_silent(monkeypatch, caplog):
    # 整段断网：直连+代理全部失败也不抛异常（通知失败不能拖垮同步主流程），但要留日志
    monkeypatch.setenv("BARK_API", "https://api.day.app/key")
    direct = MagicMock()
    direct.open.side_effect = OSError("DNS 解析失败")
    proxy = MagicMock()
    proxy.open.side_effect = OSError("代理不可达")
    with (
        patch.object(notify, "_bark_opener", return_value=direct),
        patch.object(notify, "_proxy_opener", return_value=proxy),
        caplog.at_level(logging.WARNING, logger="garmin-sync"),
    ):
        notify._send_bark("t", "m", sleep_fn=MagicMock())
    assert direct.open.call_count == notify.BARK_ATTEMPTS_PER_PATH
    assert proxy.open.call_count == notify.BARK_ATTEMPTS_PER_PATH
    assert any("Bark" in r.message for r in caplog.records)


def test_send_bark_success_first_try_no_retry(monkeypatch):
    # 一次成功：不重试、不 sleep、不碰系统代理
    monkeypatch.setenv("BARK_API", "https://api.day.app/key")
    direct = MagicMock()
    sleep = MagicMock()
    with (
        patch.object(notify, "_bark_opener", return_value=direct),
        patch.object(notify, "_proxy_opener") as proxy,
    ):
        notify._send_bark("t", "m", sleep_fn=sleep)
    direct.open.assert_called_once()
    sleep.assert_not_called()
    proxy.assert_not_called()


def test_bark_opener_bypasses_system_proxy():
    # 回归测试：Bark 必须直连，不能走 macOS 系统代理。
    # macOS 上 urllib 默认读取系统代理（Shadowrocket 等），代理节点不稳定时
    # 推送会全部超时（历史 4/4 推送失败均因此，见 logs/stderr.log）。
    opener = notify._bark_opener()
    # build_opener(ProxyHandler({})) 的语义：结果里要么没有 ProxyHandler
    # （空 proxies 时会被 add_handler 静默丢弃），要么其 proxies 为空——
    # 两种情况都代表直连，绝不携带系统代理配置。
    assert not any(
        isinstance(h, urllib.request.ProxyHandler) and h.proxies
        for h in opener.handlers
    )


def test_send_macos_logs_warning_on_nonzero_returncode(caplog):
    fake = subprocess.CompletedProcess(args=["osascript"], returncode=1, stderr=b"syntax error")
    with patch("subprocess.run", return_value=fake):
        with caplog.at_level(logging.WARNING, logger="garmin-sync"):
            notify._send_macos("t", "m")
    assert any("macOS" in r.message for r in caplog.records)


def test_send_macos_silent_on_success(caplog):
    fake = subprocess.CompletedProcess(args=["osascript"], returncode=0, stderr=b"")
    with patch("subprocess.run", return_value=fake):
        with caplog.at_level(logging.WARNING, logger="garmin-sync"):
            notify._send_macos("t", "m")
    assert caplog.records == []


def test_notify_run_failure_sends_both_channels():
    with (
        patch.object(notify, "_send_macos") as macos,
        patch.object(notify, "_send_bark") as bark,
    ):
        notify.notify_run_failure("拉取活动列表失败：timeout")
        macos.assert_called_once()
        bark.assert_called_once()


def test_notify_run_failure_truncates_detail():
    with (
        patch.object(notify, "_send_macos") as macos,
        patch.object(notify, "_send_bark"),
    ):
        notify.notify_run_failure("x" * 500)
        message = macos.call_args.args[1]
        assert len(message) < 200
