"""
Garmin 国内版 → 国际版 活动数据同步桥接

从 Garmin CN (connect.garmin.cn) 拉取活动数据，
上传到 Garmin International (connect.garmin.com)，
国际版再通过原生连接自动同步到 Strava。

链路：手表 → Garmin CN → [本脚本] → Garmin INTL → Strava

用法：
    uv run garmin-sync              # 单次同步
    uv run garmin-sync --dry-run    # 预览模式，不做实际上传
    uv run garmin-sync --days 30    # 回溯 30 天
    uv run garmin-sync --verbose    # DEBUG 级别的详细日志
"""

import fcntl
import io
import json
import logging
import os
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import dotenv
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

# .env 必须先加载，再 import notify（notify 在调用时读取 BARK_API 环境变量）
dotenv.load_dotenv()

from .notify import (
    decide_notification,
    notify_failure,
    notify_login_failure,
    notify_run_failure,
    notify_success,
)

# ── 配置 ─────────────────────────────────────────────────────────────

LOG = logging.getLogger("garmin-sync")
SYNC_DB_PATH = Path.home() / ".garmin-sync" / "synced_activities.json"

# 从环境变量读取账号信息
CN_EMAIL = os.getenv("GARMIN_CN_EMAIL")
CN_PASSWORD = os.getenv("GARMIN_CN_PASSWORD")
INTL_EMAIL = os.getenv("GARMIN_INTL_EMAIL")
INTL_PASSWORD = os.getenv("GARMIN_INTL_PASSWORD")
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "7"))


def _token_dir() -> Path:
    """Garmin token 存储目录。调用时读取并展开 ~（.env.example 示例使用 ~）。"""
    return Path(os.getenv("TOKEN_DIR", str(Path.home() / ".garminconnect"))).expanduser()


# ── 同步状态管理 ──────────────────────────────────────────────────────

MAX_FAILURES = 5  # 单条活动自动重试上限，超过后永久跳过（--activity-id 可强制重试）


class SyncDB:
    """同步状态：已同步记录 + 失败重试计数。

    文件格式（向后兼容，旧文件可没有 "failed" 键）：
        {"synced": {"<cn_id>": {...}}, "failed": {"<cn_id>": {...}}, "last_sync": "..."}
    """

    def __init__(self, path: Path, data: dict):
        self.path = path
        self.data = data

    @classmethod
    def load(cls, path: Path) -> "SyncDB":
        """加载同步记录；文件损坏或结构不对时备份旧文件后重新初始化。"""
        data: dict = {}
        if path.exists():
            try:
                data = json.loads(path.read_text())
                if not isinstance(data, dict) or not isinstance(data.get("synced"), dict):
                    raise ValueError("文件结构不完整")
            except (json.JSONDecodeError, OSError, ValueError) as e:
                ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                backup = path.with_name(f"{path.name}.corrupt-{ts}")
                try:
                    path.rename(backup)
                    LOG.warning("同步记录损坏，已备份到 %s 并重新初始化: %s", backup, e)
                except OSError:
                    LOG.warning("同步记录损坏（备份失败），重新初始化: %s", e)
                data = {}
        data.setdefault("synced", {})
        data.setdefault("failed", {})
        data.setdefault("last_sync", None)
        return cls(path, data)

    def save(self) -> None:
        """原子保存：先写临时文件再替换，避免中途崩溃留下截断的 JSON。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False))
        os.replace(tmp, self.path)

    def is_synced(self, cn_activity_id: int) -> bool:
        return str(cn_activity_id) in self.data["synced"]

    def mark_synced(self, cn_activity_id: int, intl_activity_id) -> None:
        """标记已同步，并清除该活动的失败记录。"""
        self.data["synced"][str(cn_activity_id)] = {
            "intl_activity_id": intl_activity_id,
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }
        self.data["failed"].pop(str(cn_activity_id), None)
        self.data["last_sync"] = datetime.now(timezone.utc).isoformat()
        self.save()

    def record_failure(self, cn_activity_id: int, error: str) -> int:
        """记录一次失败，返回累计失败次数。"""
        entry = self.data["failed"].setdefault(str(cn_activity_id), {"attempts": 0})
        entry["attempts"] += 1
        entry["last_error"] = error[:200]
        entry["last_attempt"] = datetime.now(timezone.utc).isoformat()
        self.save()
        return entry["attempts"]

    def is_permanently_failed(self, cn_activity_id: int) -> bool:
        attempts = self.data["failed"].get(str(cn_activity_id), {}).get("attempts", 0)
        return attempts >= MAX_FAILURES


def try_acquire_lock(lock_path: Path):
    """尝试获取进程排他锁；已被其他实例占用时返回 None。

    返回的文件对象必须保持存活以持有锁（进程退出时自动释放）。
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "a")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.close()
        return None
    return fd


# ── Garmin 客户端 ─────────────────────────────────────────────────────

def create_client(email: str, password: str, is_cn: bool, label: str) -> Garmin:
    """创建并登录 Garmin 客户端。

    Args:
        email: 账号邮箱
        password: 密码
        is_cn: 是否为国内版账号
        label: 日志标签（如 "CN" / "INTL"）

    Returns:
        已登录的 Garmin 客户端实例
    """
    # 使用独立 token 文件，避免两个 session 互相覆盖
    suffix = "cn" if is_cn else "intl"
    token_path = str(_token_dir() / f"garmin_tokens_{suffix}.json")

    LOG.info("[%s] 正在登录 (is_cn=%s)...", label, is_cn)

    client = Garmin(email, password, is_cn=is_cn)

    try:
        client.login(token_path)
        display_name = client.display_name
        LOG.info("[%s] 登录成功 ✓  用户: %s", label, display_name)
        return client
    except GarminConnectAuthenticationError as e:
        LOG.error("[%s] 登录失败 - 账号密码错误或需要 MFA: %s", label, e)
        raise
    except GarminConnectConnectionError as e:
        LOG.error("[%s] 登录失败 - 网络连接错误: %s", label, e)
        raise
    except GarminConnectTooManyRequestsError as e:
        LOG.error("[%s] 登录失败 - 请求过于频繁，稍后再试: %s", label, e)
        raise


# ── 活动同步 ──────────────────────────────────────────────────────────

def fetch_recent_activities(
    client: Garmin, days: int, label: str
) -> list[dict]:
    """拉取最近 N 天的活动列表。"""
    end = datetime.now()
    start = end - timedelta(days=days)

    LOG.info("[%s] 拉取活动: %s ~ %s", label, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))

    activities = client.get_activities_by_date(
        start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    )

    LOG.info("[%s] 找到 %d 条活动", label, len(activities))
    return activities


def fetch_activity_by_id(client: Garmin, activity_id: int, label: str) -> dict:
    """按 ID 拉取单条活动详情（--activity-id 模式用，让日志和通知带上名称和时间）。

    拉取失败时退化为仅含 ID 的 dict，不影响同步本身。
    """
    try:
        activity = client.get_activity(activity_id)
        # 详情接口的形状与列表接口不同，归一化后下游（日志/通知/标签）可统一处理：
        # 时间嵌在 summaryDTO 且为 ISO 格式（2026-08-07T22:32:56.0），类型在 activityTypeDTO
        summary = activity.get("summaryDTO") or {}
        if not activity.get("startTimeLocal") and summary.get("startTimeLocal"):
            activity["startTimeLocal"] = summary["startTimeLocal"].replace("T", " ").split(".")[0]
        if not activity.get("activityType") and activity.get("activityTypeDTO"):
            activity["activityType"] = activity["activityTypeDTO"]
        LOG.info(
            "[%s] 活动详情: %s — %s",
            label, activity.get("activityName"), activity.get("startTimeLocal"),
        )
        return activity
    except Exception as e:
        LOG.warning("[%s] 拉取活动 %s 详情失败，按仅有 ID 继续: %s", label, activity_id, e)
        return {"activityId": activity_id}


def download_fit(client: Garmin, activity_id: int, label: str) -> bytes:
    """下载活动的 .fit 文件。

    download_activity 返回的 bytes 是一个 ZIP 包，内含 .fit 文件。
    """
    LOG.info("[%s] 下载活动 %s 的 FIT 文件...", label, activity_id)

    try:
        zip_data = client.download_activity(
            activity_id,
            dl_fmt=client.ActivityDownloadFormat.ORIGINAL,
        )
        return zip_data
    except Exception as e:
        LOG.error("[%s] 下载活动 %s 失败: %s", label, activity_id, e)
        raise


def extract_fit_from_zip(zip_data: bytes, activity_id: int) -> Path:
    """从 ZIP 包中解压出 .fit 文件，返回临时文件路径。"""
    with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
        names = zf.namelist()
        fit_names = [n for n in names if n.lower().endswith(".fit")]
        if not fit_names:
            raise ValueError(f"ZIP 中未找到 .fit 文件。内容: {names}")
        fit_name = fit_names[0]
        LOG.debug("ZIP 内含: %s", fit_name)

        # 先读完再写临时文件：read 抛异常时不会留下残留空文件；with 确保句柄关闭
        data = zf.read(fit_name)
        with tempfile.NamedTemporaryFile(suffix=".fit", delete=False) as tmp:
            tmp.write(data)
        return Path(tmp.name)


def upload_to_garmin(
    client: Garmin, fit_path: Path, label: str
) -> Optional[int]:
    """上传 .fit 文件到 Garmin，返回新活动的 ID。"""
    LOG.info("[%s] 上传: %s", label, fit_path.name)

    try:
        result = client.upload_activity(str(fit_path))
        LOG.debug("[%s] 上传响应: %s", label, result)

        # 尝试从响应中提取活动 ID（不同版本响应格式可能不同）
        if isinstance(result, dict):
            activity_id = result.get("detailedImportResult", {}).get("uploadId")
            if activity_id is None:
                activity_id = result.get("uploadId") or result.get("activityId")
        else:
            activity_id = None

        return activity_id
    except Exception as e:
        LOG.error("[%s] 上传失败: %s", label, e)
        raise


def sync_activity(
    cn_client: Garmin,
    intl_client: Garmin,
    activity: dict,
    db: SyncDB,
    dry_run: bool = False,
) -> bool:
    """同步单条活动：从 CN 下载 → 上传到 INTL。

    调用方负责检查 is_synced / is_permanently_failed。
    失败时向 db 记录一次失败计数（dry_run 不产生任何副作用）。
    """
    cn_id = activity["activityId"]
    activity_name = activity.get("activityName", "未知")
    activity_type = activity.get("activityType", {}).get("typeKey", "未知")
    start_time = activity.get("startTimeLocal", "未知")

    LOG.info("─" * 60)
    LOG.info("同步: [%s] %s (%s) — %s", cn_id, activity_name, activity_type, start_time)

    if dry_run:
        LOG.info("  → [DRY RUN] 将上传到国际版")
        return False

    fit_path = None
    try:
        # 1. 从国内版下载
        zip_data = download_fit(cn_client, cn_id, "CN")

        # 2. 解压 .fit 文件
        fit_path = extract_fit_from_zip(zip_data, cn_id)
        LOG.info("  FIT 大小: %s bytes", fit_path.stat().st_size)

        # 3. 上传到国际版
        intl_id = upload_to_garmin(intl_client, fit_path, "INTL")

        # 4. 记录同步状态
        db.mark_synced(cn_id, intl_id)
        LOG.info("  ✓ 同步成功 CN:%s → INTL:%s", cn_id, intl_id)
        return True

    except Exception as e:
        attempts = db.record_failure(cn_id, str(e))
        LOG.error("  ✗ 同步失败 [%s] %s（第 %d 次）: %s", cn_id, activity_name, attempts, e)
        return False

    finally:
        if fit_path and fit_path.exists():
            fit_path.unlink(missing_ok=True)


def _activity_label(activity: dict) -> str:
    """成功通知里的活动标签：名称 + 本地开始时间（MM-DD HH:MM，省略年份和秒）。

    单条补同步模式的活动只有 activityId，退化为 "活动 <id>"，不带时间。
    """
    cn_id = activity.get("activityId")
    name = activity.get("activityName") or f"活动 {cn_id}"
    start = activity.get("startTimeLocal") or ""
    # startTimeLocal 格式为 "2026-08-01 07:00:00"
    if len(start) >= 16:
        return f"{name} ({start[5:16]})"
    return name


def run_sync(
    cn_client: Garmin,
    intl_client: Garmin,
    activities: list[dict],
    db: SyncDB,
    *,
    dry_run: bool = False,
    force_ids=frozenset(),
    sleep_fn=time.sleep,
) -> dict:
    """同步活动列表，返回统计结果（供报告与通知使用）。

    - 已同步的活动直接跳过
    - 失败次数达到 MAX_FAILURES 的活动永久跳过（force_ids 中的 ID 强制重试）
    - dry_run 不产生副作用，也不做限流 sleep
    """
    result = {
        "synced": 0,
        "skipped": 0,
        "permanent_skipped": 0,
        "failed": 0,
        "synced_names": [],
        "failed_items": [],
    }

    for activity in activities:
        cn_id = activity.get("activityId")
        name = activity.get("activityName") or f"活动 {cn_id}"

        if db.is_synced(cn_id):
            result["skipped"] += 1
            continue

        if cn_id not in force_ids and db.is_permanently_failed(cn_id):
            result["permanent_skipped"] += 1
            LOG.info(
                "跳过: [%s] %s — 已失败 %d 次达到上限，不再自动重试（--activity-id %s 可强制重试）",
                cn_id, name, MAX_FAILURES, cn_id,
            )
            continue

        success = sync_activity(cn_client, intl_client, activity, db, dry_run=dry_run)
        if success:
            result["synced"] += 1
            result["synced_names"].append(_activity_label(activity))
        elif not dry_run:
            result["failed"] += 1
            if db.is_permanently_failed(cn_id):
                result["failed_items"].append(
                    f"{name} ({cn_id})（已重试 {MAX_FAILURES} 次仍失败，不再自动重试；--activity-id 可强制重试）"
                )
            else:
                result["failed_items"].append(f"{name} ({cn_id})")

        # 请求间隔，避免被限流（dry-run 不发起请求，无需间隔）
        if not dry_run:
            sleep_fn(1)

    return result


# ── 主流程 ────────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Garmin 国内版 → 国际版 活动同步",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="预览模式：只列出待同步活动，不上传",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=LOOKBACK_DAYS,
        help=f"回溯天数（默认: {LOOKBACK_DAYS}）",
    )
    parser.add_argument(
        "--activity-id",
        type=int,
        help="只同步指定活动 ID（用于单条补同步，可强制重试已达失败上限的活动）",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="输出 DEBUG 级别的详细日志",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="关闭通知（成功/失败都不发送）",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── 参数校验 ──────────────────────────────────────────────
    if not all([CN_EMAIL, CN_PASSWORD, INTL_EMAIL, INTL_PASSWORD]):
        LOG.error("缺少必要配置，请检查环境变量:")
        LOG.error("  GARMIN_CN_EMAIL, GARMIN_CN_PASSWORD")
        LOG.error("  GARMIN_INTL_EMAIL, GARMIN_INTL_PASSWORD")
        LOG.error("可以复制 .env.example 为 .env 并填入你的账号信息。")
        if not args.no_notify:
            # macOS 通道不依赖任何配置，.env 缺失时也能提醒用户
            notify_run_failure("缺少 GARMIN_* 账号配置，请检查 .env 文件")
        sys.exit(1)

    # ── 进程锁：防止手动运行与 launchd 定时任务并发 ────────────
    lock = try_acquire_lock(SYNC_DB_PATH.parent / "sync.lock")
    if lock is None:
        LOG.error("已有另一个 garmin-sync 实例在运行，本次退出。")
        sys.exit(0)

    # ── 登录 ──────────────────────────────────────────────────
    try:
        cn = create_client(CN_EMAIL, CN_PASSWORD, is_cn=True, label="CN")
        intl = create_client(INTL_EMAIL, INTL_PASSWORD, is_cn=False, label="INTL")
    except Exception as e:
        LOG.error("登录失败，退出。")
        if not args.no_notify:
            notify_login_failure(str(e))
        sys.exit(1)

    # ── 同步主体（任何异常都兜底通知，避免定时任务静默失败）─────
    try:
        db = SyncDB.load(SYNC_DB_PATH)

        # ── 拉取国内版活动 ────────────────────────────────────
        if args.activity_id:
            # 单条补同步模式（可强制重试已达失败上限的活动）
            # 先拉详情，让日志和通知能显示名称和时间
            activities = [fetch_activity_by_id(cn, args.activity_id, "CN")]
            LOG.info("单条同步模式: 活动 %s", args.activity_id)
        else:
            activities = fetch_recent_activities(cn, args.days, "CN")

        if not activities:
            LOG.info("没有找到需要处理的活动。")
            return

        # 按时间排序（最旧先同步）
        activities.sort(key=lambda a: a.get("startTimeLocal", ""))

        # ── 同步 ──────────────────────────────────────────────
        force_ids = {args.activity_id} if args.activity_id else frozenset()
        result = run_sync(
            cn, intl, activities, db,
            dry_run=args.dry_run, force_ids=force_ids,
        )
    except Exception as e:
        LOG.exception("同步过程异常中断")
        if not args.no_notify:
            notify_run_failure(str(e))
        sys.exit(1)

    # ── 报告 ──────────────────────────────────────────────────
    LOG.info("=" * 60)
    LOG.info(
        "同步完成: 成功 %d, 跳过 %d, 失败 %d, 永久跳过 %d",
        result["synced"], result["skipped"], result["failed"], result["permanent_skipped"],
    )
    if args.dry_run:
        LOG.info("（预览模式，未实际修改数据）")

    # ── 通知 ──────────────────────────────────────────────────
    if not args.dry_run and not args.no_notify:
        kind = decide_notification(result["failed_items"], result["synced_names"])
        if kind == "failure":
            notify_failure(result["failed_items"])
        elif kind == "success":
            notify_success(result["synced_names"])


if __name__ == "__main__":
    main()
