# Garmin CN → INTL 活动同步桥接

## 概述

将 Garmin 国内版 (connect.garmin.cn) 的活动数据自动同步到 Garmin 国际版 (connect.garmin.com)，国际版再通过原生连接推送到 Strava。

链路：`手表 → Garmin CN → [garmin-sync] → Garmin INTL → Strava`

## 项目结构

```
.
├── pyproject.toml            # 项目配置与依赖（uv 管理）
├── uv.lock                   # 依赖锁定文件
├── .python-version           # Python 版本（uv 使用）
├── src/
│   └── garmin_sync/
│       ├── __init__.py       # 核心同步逻辑
│       └── notify.py         # 通知（macOS + Bark）
├── tests/                    # pytest：notify / SyncDB / run_sync / extract / config
├── com.garmin-sync.plist     # macOS launchd 定时任务（每小时第 7 分钟，StartCalendarInterval，错过不补跑）
├── newsyslog.d/              # 日志轮转配置（sudo cp 到 /etc/newsyslog.d/ 安装）
├── .env.example              # 环境变量模板
├── .env                      # 实际配置（含密码，已 gitignore）
├── logs/                     # 运行时日志（stdout.log / stderr.log，已 gitignore）
└── .venv/                    # Python 虚拟环境
```

## 运行命令

```bash
uv run garmin-sync                    # 单次同步（回溯 7 天）
uv run garmin-sync --dry-run          # 预览模式
uv run garmin-sync --days 30          # 回溯 30 天
uv run garmin-sync --activity-id 123456  # 单条补同步
uv run garmin-sync --verbose           # 调试模式（DEBUG 日志）
uv run garmin-sync --no-notify         # 关闭通知

# 测试
uv run pytest                            # 运行通知模块测试（pytest 在 dev 依赖组）

# 定时任务管理
launchctl load ~/Library/LaunchAgents/com.garmin-sync.plist     # 启用
launchctl unload ~/Library/LaunchAgents/com.garmin-sync.plist   # 停止
launchctl list | grep garmin-sync                                # 状态
```

## 关键依赖

- `garminconnect` (≥0.3.4) — Garmin Connect API 封装库，`is_cn=True` 参数支持国内版
- `python-dotenv` (≥1.0) — 从 `.env` 文件加载环境变量
- 通知：macOS 通知中心（osascript）+ Bark 手机推送（可选，`BARK_API` 环境变量）；成功/失败/中断时通知（成功含完整活动清单），全部跳过时静默，`--no-notify` 关闭
- 两个独立的 Garmin session，用不同的 token 文件（`garmin_tokens_cn.json` / `garmin_tokens_intl.json`）
- plist 通过 `/opt/homebrew/bin/uv run garmin-sync` 调用（而非 `.venv/bin/garmin-sync`），删除/重建 `.venv` 不会破坏定时任务；`PYTHONUNBUFFERED=1` 确保日志实时刷新

## 同步状态机制

同步记录存储在 `~/.garmin-sync/synced_activities.json`，由 `SyncDB` 类管理（原子写入、损坏自动备份重置），按国内版活动 ID 去重。删除该文件可重置同步状态。

- **失败重试**：`failed` 字段记录每条活动的失败次数，同一活动失败 5 次（`MAX_FAILURES`）后永久跳过，`--activity-id <id>` 可强制重试
- **进程锁**：`~/.garmin-sync/sync.lock`（flock）防止手动运行与 launchd 定时任务并发
