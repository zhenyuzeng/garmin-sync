"""
Garmin 同步通知：macOS 通知中心 + Bark (iPhone) 双通道。

- macOS 通知：系统自带 osascript，launchd 用户域可正常弹出
- Bark：HTTP 推送到 iPhone；未配置 BARK_API 时静默跳过；
  直连优先、失败重试后回退系统代理（见 BARK_ATTEMPTS_PER_PATH）
"""

import logging
import os
import subprocess
import time
import urllib.parse
import urllib.request
from typing import Optional

LOG = logging.getLogger("garmin-sync")


def _bark_api() -> str:
    """Bark API 地址（去尾部斜杠）。调用时读取，确保 .env 在 import 之后加载也能生效。"""
    return os.getenv("BARK_API", "").rstrip("/")


def _macos_escape(text: str) -> str:
    """转义 AppleScript 字符串中的反斜杠和引号。"""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _send_macos(title: str, message: str) -> None:
    """发送 macOS 通知中心通知；失败仅记日志，不抛出。"""
    script = (
        f'display notification "{_macos_escape(message)}" '
        f'with title "{_macos_escape(title)}" sound name "default"'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            LOG.warning(
                "macOS 通知失败 (exit %d): %s",
                result.returncode,
                result.stderr.decode(errors="replace").strip(),
            )
    except Exception as e:
        LOG.warning("macOS 通知失败: %s", e)


def _bark_opener() -> urllib.request.OpenerDirector:
    """直连 opener（空代理）。

    macOS 上 urllib 默认读取系统代理（Shadowrocket 等），代理节点不稳定时
    推送会整体超时失败（历史 4/4 推送失败均因此）。Bark 为国内服务，
    直连即可，不应依赖代理。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _proxy_opener() -> urllib.request.OpenerDirector:
    """系统代理 opener（urllib 默认行为：读取 macOS 系统代理）。直连全部失败时兜底。"""
    return urllib.request.build_opener()


# Bark 推送重试参数：每条路径（直连/系统代理）最多尝试次数、重试间隔秒数。
# 单路径单次尝试会丢通知（logs/stderr.log 证据）：代理节点挂掉时只有直连能送达，
# 直连抖动时只有代理能送达（2026-08-13 23:13 garmin.com 经代理上传成功，
# 而直连 Bark 握手超时，3 条活动的成功通知丢失）。
BARK_ATTEMPTS_PER_PATH = 2
BARK_RETRY_DELAY = 3  # 秒


def _send_bark(title: str, message: str, sleep_fn=time.sleep) -> None:
    """发送 Bark 手机推送；未配置 BARK_API 时静默跳过。

    直连优先（代理节点不稳定），失败后重试；直连多次失败退到系统代理兜底。
    所有路径都失败仅记日志，不抛出——通知失败不能拖垮同步主流程。
    """
    api = _bark_api()
    if not api:
        return
    # safe=""：标题/内容中的 / 也必须编码，否则会破坏 /key/title/body 路径结构
    url = f"{api}/{urllib.parse.quote(title, safe='')}/{urllib.parse.quote(message, safe='')}"
    for label, make_opener in (("直连", _bark_opener), ("系统代理", _proxy_opener)):
        opener = make_opener()
        for attempt in range(1, BARK_ATTEMPTS_PER_PATH + 1):
            try:
                with opener.open(url, timeout=5):
                    return
            except Exception as e:
                LOG.warning(
                    "Bark 推送失败（%s 第 %d/%d 次）: %s",
                    label, attempt, BARK_ATTEMPTS_PER_PATH, e,
                )
                if attempt < BARK_ATTEMPTS_PER_PATH:
                    sleep_fn(BARK_RETRY_DELAY)
    LOG.warning("Bark 推送放弃：直连与系统代理均不可达")


def _join_items(items: list[str]) -> str:
    """把列表完整拼成通知内容（不截断，用户要求看到全部被同步的记录）。"""
    return "、".join(items)


def decide_notification(
    failed_items: list[str], synced_names: list[str]
) -> Optional[str]:
    """决定应发送的通知类型：失败优先，其次成功，全跳过返回 None。"""
    if failed_items:
        return "failure"
    if synced_names:
        return "success"
    return None


def notify_success(activity_names: list[str]) -> None:
    """成功同步了 ≥1 条此前未同步的活动。完整列出所有活动名。"""
    if not activity_names:
        return
    title = "🏃 Garmin 同步成功"
    message = f"已同步 {len(activity_names)} 条活动：{_join_items(activity_names)}"
    _send_macos(title, message)
    _send_bark(title, message)


def notify_failure(failed_items: list[str]) -> None:
    """有活动同步失败。failed_items 为活动标识（名称+ID）列表。完整列出。"""
    if not failed_items:
        return
    title = "⚠️ Garmin 同步失败"
    message = (
        f"{len(failed_items)} 条活动同步失败：{_join_items(failed_items)}。"
        "详见 logs/stderr.log"
    )
    _send_macos(title, message)
    _send_bark(title, message)


def notify_login_failure(detail: str) -> None:
    """登录 Garmin 失败。"""
    title = "⚠️ Garmin 登录失败"
    message = f"请检查账号密码或网络：{detail[:100]}"
    _send_macos(title, message)
    _send_bark(title, message)


def notify_run_failure(detail: str) -> None:
    """登录成功之后的运行期异常：拉取失败、配置缺失、同步中断等。"""
    title = "⚠️ Garmin 同步中断"
    message = f"同步过程异常：{detail[:100]}。详见 logs/stderr.log"
    _send_macos(title, message)
    _send_bark(title, message)
