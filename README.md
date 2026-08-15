<div align="center">

# garmin-sync

**Garmin 国内版 → 国际版 活动同步桥接：手表数据自动流向 Strava**

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)

**中文** | [English](README_EN.md)

</div>

```
手表 → Garmin CN → [garmin-sync] → Garmin INTL → Strava
                      ▲
                定时自动运行
```

## 目录

- [✨ 特性](#-特性)
- [🔧 工作原理](#-工作原理)
- [🚀 快速开始](#-快速开始)
- [📦 部署教程](#-部署教程)
- [📖 命令参考](#-命令参考)
- [⚙️ 配置项](#-配置项)
- [🔔 通知](#-通知)
- [🔁 失败重试](#-失败重试)
- [🛠️ 故障排查](#-故障排查)
- [❓ 常见问题](#-常见问题)
- [🤝 贡献](#-贡献)
- [⚠️ 免责声明](#-免责声明)
- [📄 开源协议](#-开源协议)

## ✨ 特性

- **自动同步**：定时运行，新活动自动完成 CN → INTL → Strava 全链路，无需手动干预
- **数据完整**：上传 Garmin 原始 `.fit` 文件，GPS、心率、功率、游泳指标等全部保留
- **结果通知**：同步成功/失败时推送 macOS 通知 + Bark 手机推送，成功通知列出每条活动的名称和运动时间
- **失败重试**：单条失败自动重试，连续失败 5 次后永久跳过，可随时用 `--activity-id` 手动补同步
- **状态可靠**：同步记录原子写入、损坏自动备份重置；进程锁防止手动运行与定时任务并发

## 🔧 工作原理

1. **登录**：用 [garminconnect](https://github.com/cyberjunky/python-garminconnect) 库分别登录国内版 (`is_cn=True`) 和国际版（两个独立 session、独立 token 文件）
2. **拉取**：获取国内版最近 N 天的活动列表（默认 7 天）
3. **去重**：本地 JSON 记录已同步的活动 ID，已同步的直接跳过
4. **下载**：从国内版下载 `.fit` 原始活动文件
5. **上传**：将 `.fit` 文件上传到国际版（国际版绑定 Strava 后自动推送）
6. **记录**：保存同步状态，发送结果通知

运行数据存放在：

| 路径 | 内容 |
| --- | --- |
| `~/.garmin-sync/synced_activities.json` | 已同步记录 + 失败计数（删除可重置同步状态） |
| `~/.garmin-sync/sync.lock` | 进程锁（flock，进程退出自动释放，不会残留） |
| `~/.garminconnect/garmin_tokens_{cn,intl}.json` | 登录令牌缓存（避免重复登录 / MFA），可用 `TOKEN_DIR` 改位置 |

## 🚀 快速开始

前置条件：

- [uv](https://docs.astral.sh/uv/)（Python 3.12 会由 uv 按 `.python-version` 自动安装）
- Garmin 国内版账号 (connect.garmin.cn) + Garmin 国际版账号 (connect.garmin.com)
- 国际版账号已在 Garmin Connect 的"第三方应用"设置中绑定 Strava

```bash
# 1. 克隆并安装依赖
git clone https://github.com/ziyezeng511/garmin-sync.git
cd garmin-sync
uv sync

# 2. 配置账号
cp .env.example .env
# 用编辑器打开 .env，填入两个账号的邮箱和密码

# 3. 预览（不实际上传，验证登录和活动列表）
uv run garmin-sync --dry-run

# 4. 正式同步一次，确认国际版 / Strava 上出现活动
uv run garmin-sync
```

> 首次登录若账号开启了二次验证 (MFA)，会提示交互式输入验证码；令牌缓存后不再需要。

## 📦 部署教程

快速开始验证通过后，把同步部署为定时任务，全自动运行。

### macOS —— launchd（推荐，作者自用方案）

**① 替换 plist 中的示例路径**

仓库里的 `com.garmin-sync.plist` 用的是作者机器的路径，在**项目根目录**执行一键替换：

```bash
sed -i '' "s|/Users/ziyezeng/Projects/garmin-sync|$PWD|g" com.garmin-sync.plist
```

（也可以手动编辑：`WorkingDirectory` 和 stdout/stderr 两条日志路径，共 3 处。）

**② 安装并加载**

```bash
mkdir -p logs
cp com.garmin-sync.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.garmin-sync.plist
```

**③ 验证**

```bash
launchctl list | grep garmin-sync    # 应输出一行 com.garmin-sync
launchctl start com.garmin-sync      # 立即手动触发一次（不必等整点）
tail -f logs/stdout.log              # 看到登录与同步日志即部署成功
```

之后每小时第 7 分钟自动运行。电脑睡眠/关机错过的整点不补跑，等下一小时。

**④（可选）日志自动轮转**

`logs/` 会持续增长，用 macOS 自带的 newsyslog 轮转（单文件超 1MB 轮转，保留 4 份并 gzip）：

```bash
sed -i '' -e "s|<用户名>|$USER|g" -e "s|/Users/$USER/Projects/garmin-sync|$PWD|g" newsyslog.d/com.garmin-sync.conf
sudo cp newsyslog.d/com.garmin-sync.conf /etc/newsyslog.d/
```

**修改运行时间**：编辑 plist 中的 `StartCalendarInterval`（只指定 `Minute` = 每小时第 N 分钟），然后重新加载：

```bash
launchctl unload ~/Library/LaunchAgents/com.garmin-sync.plist
launchctl load ~/Library/LaunchAgents/com.garmin-sync.plist
```

### Linux —— cron

克隆、配置 `.env`、手动验证一次（同快速开始 1–4 步），然后：

```bash
mkdir -p logs
which uv    # 记下 uv 的绝对路径：cron 环境 PATH 极简，必须写绝对路径
crontab -e
```

添加一行（每小时第 7 分钟）：

```cron
7 * * * * cd /home/you/garmin-sync && /home/you/.local/bin/uv run garmin-sync >> logs/cron.log 2>&1
```

Linux 没有 macOS 桌面通知，建议配置 `BARK_API` 手机推送；日志轮转可用系统 logrotate。

<details>
<summary><b>备选：systemd timer（比 cron 更可靠，关机错过可补跑）</b></summary>

创建 `~/.config/systemd/user/garmin-sync.service`（路径按实际情况改，`%h` 自动展开为家目录）：

```ini
[Unit]
Description=Garmin CN to INTL activity sync

[Service]
Type=oneshot
WorkingDirectory=%h/Projects/garmin-sync
ExecStart=%h/.local/bin/uv run garmin-sync
```

创建 `~/.config/systemd/user/garmin-sync.timer`：

```ini
[Unit]
Description=Run garmin-sync hourly

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
```

启用并验证：

```bash
systemctl --user daemon-reload
systemctl --user enable --now garmin-sync.timer
systemctl --user list-timers | grep garmin-sync   # 应列出下次触发时间
journalctl --user -u garmin-sync.service          # 查看运行日志
```

`Persistent=true` 会在下次启动时补跑错过的任务。
</details>

### 升级

```bash
cd garmin-sync
git pull
uv sync    # 依赖有变化时自动更新
```

定时任务路径未变，无需重装。

### 卸载

```bash
# macOS：移除定时任务与日志轮转
launchctl unload ~/Library/LaunchAgents/com.garmin-sync.plist
rm ~/Library/LaunchAgents/com.garmin-sync.plist
sudo rm -f /etc/newsyslog.d/com.garmin-sync.conf

# Linux：crontab -e 删除对应行（或 systemctl --user disable --now garmin-sync.timer）

# 删除项目与运行数据（可选）
rm -rf /path/to/garmin-sync
rm -rf ~/.garmin-sync ~/.garminconnect
```

## 📖 命令参考

| 命令 | 说明 |
| --- | --- |
| `uv run garmin-sync` | 单次同步（默认回溯 7 天） |
| `uv run garmin-sync --dry-run` | 预览模式：只列出待同步活动，不上传、不通知 |
| `uv run garmin-sync --days 30` | 回溯 30 天 |
| `uv run garmin-sync --activity-id <id>` | 只同步指定活动（补同步 / 强制重试永久失败的活动） |
| `uv run garmin-sync -v` / `--verbose` | DEBUG 级别日志（含上传响应），排查问题时使用 |
| `uv run garmin-sync --no-notify` | 本次运行关闭所有通知 |
| `uv run pytest` | 运行测试 |

## ⚙️ 配置项

全部通过 `.env`（或环境变量）配置：

| 变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `GARMIN_CN_EMAIL` / `GARMIN_CN_PASSWORD` | ✅ | — | 国内版账号 |
| `GARMIN_INTL_EMAIL` / `GARMIN_INTL_PASSWORD` | ✅ | — | 国际版账号 |
| `LOOKBACK_DAYS` | 否 | `7` | 每次运行回溯的天数 |
| `BARK_API` | 否 | — | Bark 推送地址（含设备 key）；不配置则只发 macOS 通知 |
| `TOKEN_DIR` | 否 | `~/.garminconnect/` | 登录令牌缓存目录 |

## 🔔 通知

同步结果自动推送（macOS 通知中心 + 手机）：

- **成功**：有新活动同步成功 → 🏃 Garmin 同步成功（完整列出所有被同步的活动：名称 + 运动开始时间，如 `晨跑 (08-01 07:00)`）
- **失败**：有活动同步失败或登录失败 → ⚠️ 通知（含原因）
- **中断**：拉取活动失败、配置缺失等运行期异常 → ⚠️ Garmin 同步中断（避免定时任务静默失败）
- **全部跳过**：无新活动 → 静默，不打扰

手机推送使用 [Bark](https://bark.day.app)（iPhone 免费 App），在 `.env` 配置 `BARK_API=https://api.day.app/你的设备key`。不配置时只发电脑通知；`--no-notify` 关闭所有通知。

> 注：Bark 推送固定走直连（不经过 macOS 系统代理），避免代理节点不稳定导致推送失败。

## 🔁 失败重试

单条活动同步失败会自动重试（下次运行时）。**同一活动连续失败 5 次后永久跳过**，不再自动重试，避免不可逆失败（如活动文件损坏）造成每小时重复通知。需要重试时用 `--activity-id` 强制补同步：

```bash
uv run garmin-sync --activity-id 123456789
```

失败次数记录在 `~/.garmin-sync/synced_activities.json` 的 `failed` 字段中，同步成功后自动清零。

## 🛠️ 故障排查

| 症状 | 可能原因与处理 |
| --- | --- |
| 提示"已有另一个 garmin-sync 实例在运行" | 进程锁生效，确实有实例在跑（`ps aux \| grep garmin-sync` 确认），等它结束即可；flock 锁随进程退出自动释放，不会残留 |
| 登录失败 / 反复要求验证码 | 检查 `.env` 账号密码；短时间多次失败会触发 Garmin 限流，等半小时再试 |
| 定时任务到点没反应 | 睡眠/关机错过不补跑属预期；`launchctl list \| grep garmin-sync` 查看上次退出码；查 `logs/stderr.log` |
| macOS 通知不弹 | 系统设置 → 通知中找到 "Script Editor"（osascript 通知以此名义出现），允许通知 |
| Bark 收不到 | 检查 `BARK_API` 设备 key 是否正确；Bark 走直连，与系统代理无关；日志中搜 `Bark 推送失败` |
| 想看更详细的日志 | `uv run garmin-sync -v` |

## ❓ 常见问题

**Q: 国际版的活动会自动同步到 Strava 吗？**
A: 会的。只要你在 Garmin International 的"第三方应用"设置中绑定了 Strava，通过本脚本上传到国际版的活动会自动推送到 Strava。

**Q: 同步后国际版活动数据完整吗？**
A: 是的。`.fit` 是 Garmin 原始格式，包含全部数据（GPS、心率、功率、步频、游泳指标等），和手表直接上传的活动无异。

**Q: 重复运行会造成重复活动吗？**
A: 不会。脚本通过活动 ID 去重，已同步的会自动跳过。

## 🤝 贡献

欢迎 Issue 和 PR！提交 PR 前请确认：

1. `uv run pytest` 全部通过
2. 代码注释与日志沿用中文，风格与现有代码一致

## ⚠️ 免责声明

本项目为个人开源工具，与 Garmin 公司无任何关联。同步能力基于非公开接口（经由 `garminconnect` 库），Garmin 侧政策或接口变动可能导致功能失效。请仅用于同步**本人账号**的数据并保持克制使用（脚本已内置 1 秒请求间隔与失败重试上限）。账号密码仅保存在你本机的 `.env` 中，登录令牌缓存在本机 `TOKEN_DIR`，不会发送到任何第三方。使用风险自负。

## 📄 开源协议

[GPL-3.0](LICENSE) © 2026 Ziye Zeng

---

如果这个项目帮到了你，欢迎点一个 ⭐ Star！
