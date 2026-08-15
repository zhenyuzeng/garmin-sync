<div align="center">

# garmin-sync

**Garmin CN → INTL activity sync bridge: get your watch data to Strava automatically**

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)

[中文](README.md) | **English**

</div>

```
Watch → Garmin CN → [garmin-sync] → Garmin INTL → Strava
                       ▲
             runs automatically on a schedule
```

## Contents

- [✨ Features](#-features)
- [🔧 How It Works](#-how-it-works)
- [🚀 Quick Start](#-quick-start)
- [📦 Deployment](#-deployment)
- [📖 Command Reference](#-command-reference)
- [⚙️ Configuration](#-configuration)
- [🔔 Notifications](#-notifications)
- [🔁 Failure Retry](#-failure-retry)
- [🛠️ Troubleshooting](#-troubleshooting)
- [❓ FAQ](#-faq)
- [🤝 Contributing](#-contributing)
- [⚠️ Disclaimer](#-disclaimer)
- [📄 License](#-license)

## ✨ Features

- **Automatic sync** — run it on a schedule and new activities flow CN → INTL → Strava with no manual steps
- **Lossless data** — uploads the original Garmin `.fit` file, preserving GPS, heart rate, power, swim metrics, and everything else
- **Result notifications** — macOS Notification Center + Bark (iPhone) push on success/failure; the success message lists every synced activity with its start time
- **Failure retry** — failed activities are retried on later runs; after 5 consecutive failures an activity is skipped permanently, and `--activity-id` forces a re-sync anytime
- **Robust state** — atomic sync-record writes with automatic backup-and-reset on corruption; a process lock prevents manual runs from racing the scheduled job

## 🔧 How It Works

1. **Login** — uses the [garminconnect](https://github.com/cyberjunky/python-garminconnect) library to log in to CN (`is_cn=True`) and INTL as two independent sessions with separate token files
2. **Fetch** — pulls the activity list for the last N days from Garmin CN (default: 7)
3. **Deduplicate** — a local JSON file tracks already-synced activity IDs; those are skipped
4. **Download** — downloads the original `.fit` activity file from Garmin CN
5. **Upload** — uploads the `.fit` file to Garmin International (which pushes it to Strava once linked)
6. **Record** — saves the sync state and sends a result notification

Runtime data lives in:

| Path | Contents |
| --- | --- |
| `~/.garmin-sync/synced_activities.json` | Synced records + failure counts (delete to reset sync state) |
| `~/.garmin-sync/sync.lock` | Process lock (flock; released automatically when the process exits, never goes stale) |
| `~/.garminconnect/garmin_tokens_{cn,intl}.json` | Cached login tokens (avoids repeated logins / MFA); relocate with `TOKEN_DIR` |

## 🚀 Quick Start

Prerequisites:

- [uv](https://docs.astral.sh/uv/) (Python 3.12 is installed automatically by uv via `.python-version`)
- A Garmin China account (connect.garmin.cn) **and** a Garmin International account (connect.garmin.com)
- Strava linked to the International account (Garmin Connect settings → Connected Apps)

```bash
# 1. Clone and install dependencies
git clone https://github.com/ziyezeng511/garmin-sync.git
cd garmin-sync
uv sync

# 2. Configure your accounts
cp .env.example .env
# open .env in an editor and fill in both account emails + passwords

# 3. Dry run (uploads nothing; verifies login and the activity list)
uv run garmin-sync --dry-run

# 4. Run a real sync and confirm the activities appear on INTL / Strava
uv run garmin-sync
```

> If MFA is enabled on an account, the first login prompts for the code interactively; cached tokens make it a one-time step.

## 📦 Deployment

Once the quick start works, deploy the sync as a scheduled job so it runs fully automatically.

### macOS — launchd (recommended; the author's own setup)

**① Rewrite the example paths in the plist**

The shipped `com.garmin-sync.plist` contains the author's machine paths. From the **project root**, rewrite them in one shot:

```bash
sed -i '' "s|/Users/ziyezeng/Projects/garmin-sync|$PWD|g" com.garmin-sync.plist
```

(Or edit manually: `WorkingDirectory` plus the stdout/stderr log paths — 3 places.)

**② Install and load**

```bash
mkdir -p logs
cp com.garmin-sync.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.garmin-sync.plist
```

**③ Verify**

```bash
launchctl list | grep garmin-sync    # should print one com.garmin-sync row
launchctl start com.garmin-sync      # trigger a run right now (no need to wait for the hour)
tail -f logs/stdout.log              # login + sync logs mean the deployment works
```

It then runs at minute 7 of every hour. A schedule missed while the machine is asleep or off is not caught up — it waits for the next hour.

**④ (Optional) Automatic log rotation**

`logs/` grows forever; rotate with macOS's built-in newsyslog (rotates above 1 MB, keeps 4 gzipped generations):

```bash
sed -i '' -e "s|<用户名>|$USER|g" -e "s|/Users/$USER/Projects/garmin-sync|$PWD|g" newsyslog.d/com.garmin-sync.conf
sudo cp newsyslog.d/com.garmin-sync.conf /etc/newsyslog.d/
```

(The first substitution fills in your username; the second fixes the project path if you cloned somewhere other than `~/Projects/garmin-sync`.)

**Changing the schedule**: edit `StartCalendarInterval` in the plist (specifying only `Minute` = that minute of every hour), then reload:

```bash
launchctl unload ~/Library/LaunchAgents/com.garmin-sync.plist
launchctl load ~/Library/LaunchAgents/com.garmin-sync.plist
```

### Linux — cron

Clone, configure `.env`, and verify with a manual run first (quick start steps 1–4), then:

```bash
mkdir -p logs
which uv    # note the absolute path: cron's PATH is minimal, so it must be absolute
crontab -e
```

Add one line (minute 7 of every hour):

```cron
7 * * * * cd /home/you/garmin-sync && /home/you/.local/bin/uv run garmin-sync >> logs/cron.log 2>&1
```

There are no macOS desktop notifications on Linux — configure `BARK_API` for phone push. Use the system logrotate for log rotation.

<details>
<summary><b>Alternative: systemd timer (more reliable than cron; catches up after downtime)</b></summary>

Create `~/.config/systemd/user/garmin-sync.service` (adjust paths; `%h` expands to your home directory):

```ini
[Unit]
Description=Garmin CN to INTL activity sync

[Service]
Type=oneshot
WorkingDirectory=%h/Projects/garmin-sync
ExecStart=%h/.local/bin/uv run garmin-sync
```

Create `~/.config/systemd/user/garmin-sync.timer`:

```ini
[Unit]
Description=Run garmin-sync hourly

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
```

Enable and verify:

```bash
systemctl --user daemon-reload
systemctl --user enable --now garmin-sync.timer
systemctl --user list-timers | grep garmin-sync   # should show the next trigger time
journalctl --user -u garmin-sync.service          # view run logs
```

`Persistent=true` catches up runs missed while the machine was off.
</details>

### Upgrading

```bash
cd garmin-sync
git pull
uv sync    # picks up dependency changes automatically
```

The scheduled job's paths don't change, so it needs no reinstall.

### Uninstalling

```bash
# macOS: remove the scheduled job and log rotation
launchctl unload ~/Library/LaunchAgents/com.garmin-sync.plist
rm ~/Library/LaunchAgents/com.garmin-sync.plist
sudo rm -f /etc/newsyslog.d/com.garmin-sync.conf

# Linux: remove the crontab line (or: systemctl --user disable --now garmin-sync.timer)

# Delete the project and runtime data (optional)
rm -rf /path/to/garmin-sync
rm -rf ~/.garmin-sync ~/.garminconnect
```

## 📖 Command Reference

| Command | Description |
| --- | --- |
| `uv run garmin-sync` | Single sync (looks back 7 days by default) |
| `uv run garmin-sync --dry-run` | Preview: lists pending activities without uploading or notifying |
| `uv run garmin-sync --days 30` | Look back 30 days |
| `uv run garmin-sync --activity-id <id>` | Sync one specific activity (backfill / force-retry a permanently failed one) |
| `uv run garmin-sync -v` / `--verbose` | DEBUG-level logs (incl. upload responses) for troubleshooting |
| `uv run garmin-sync --no-notify` | Disable all notifications for this run |
| `uv run pytest` | Run the test suite |

## ⚙️ Configuration

Everything is configured via `.env` (or environment variables):

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `GARMIN_CN_EMAIL` / `GARMIN_CN_PASSWORD` | ✅ | — | Garmin China account |
| `GARMIN_INTL_EMAIL` / `GARMIN_INTL_PASSWORD` | ✅ | — | Garmin International account |
| `LOOKBACK_DAYS` | No | `7` | How many days to look back on each run |
| `BARK_API` | No | — | Bark push URL (with device key); without it only macOS notifications are sent |
| `TOKEN_DIR` | No | `~/.garminconnect/` | Login token cache directory |

## 🔔 Notifications

Sync results are pushed automatically (macOS Notification Center + phone):

- **Success**: new activities synced → 🏃 success notification listing every synced activity with its name and start time, e.g. `Morning Run (08-01 07:00)`
- **Failure**: an activity failed to sync, or login failed → ⚠️ notification with the reason
- **Interrupted**: runtime errors such as fetch failures or missing config → ⚠️ "sync interrupted" notification, so a broken scheduled job never fails silently
- **All skipped**: nothing new → stays silent, no disturbance

Phone push uses [Bark](https://bark.day.app) (free iPhone app): set `BARK_API=https://api.day.app/your-device-key` in `.env`. Without it, only desktop notifications are sent; `--no-notify` disables everything.

> Note: Bark requests always use a direct connection (bypassing the macOS system proxy), so an unstable proxy node can't break push delivery.

## 🔁 Failure Retry

A failed activity is retried automatically on the next run. **After 5 consecutive failures it is skipped permanently** and no longer retried automatically — this prevents unrecoverable failures (e.g. a corrupt activity file) from triggering repeat notifications every hour. Force a retry with `--activity-id`:

```bash
uv run garmin-sync --activity-id 123456789
```

Failure counts are stored in the `failed` field of `~/.garmin-sync/synced_activities.json` and reset to zero after a successful sync.

## 🛠️ Troubleshooting

| Symptom | Likely cause & fix |
| --- | --- |
| "Another garmin-sync instance is running" | The process lock is doing its job — an instance really is running (`ps aux \| grep garmin-sync` to confirm); wait for it to finish. flock locks are released on process exit and never go stale |
| Login fails / repeated MFA prompts | Check the credentials in `.env`; repeated failures in a short window trigger Garmin rate limiting — wait half an hour and retry |
| Scheduled job didn't run on time | Missed runs while asleep/off are expected (no catch-up); check the last exit code via `launchctl list \| grep garmin-sync` and inspect `logs/stderr.log` |
| macOS notifications don't appear | System Settings → Notifications → find "Script Editor" (osascript notifications appear under its name) and allow them |
| Bark push not received | Verify the device key in `BARK_API`; Bark uses a direct connection, so the system proxy is irrelevant; search the log for `Bark 推送失败` |
| Want more detailed logs | `uv run garmin-sync -v` |

## ❓ FAQ

**Q: Do International activities sync to Strava automatically?**
A: Yes. As long as Strava is linked under "Connected Apps" in your Garmin International settings, anything uploaded by this tool is pushed to Strava automatically.

**Q: Is the synced activity data complete?**
A: Yes. `.fit` is Garmin's original format and contains all data (GPS, heart rate, power, cadence, swim metrics, etc.) — identical to an activity uploaded directly by the watch.

**Q: Will re-running create duplicate activities?**
A: No. Activities are deduplicated by ID; already-synced ones are skipped automatically.

## 🤝 Contributing

Issues and PRs are welcome! Before submitting a PR, please make sure:

1. `uv run pytest` passes
2. Code comments and log messages stay in Chinese, matching the existing style

## ⚠️ Disclaimer

This is a personal open-source tool, not affiliated with Garmin in any way. Syncing relies on unofficial APIs (via the `garminconnect` library); changes on Garmin's side may break it. Use it only to sync **your own** data and keep usage moderate (the script already enforces a 1-second request interval and a failure cap). Your credentials live only in the local `.env` file and the token cache under `TOKEN_DIR` on your machine — they are never sent to any third party. Use at your own risk.

## 📄 License

[GPL-3.0](LICENSE) © 2026 Ziye Zeng

---

If this project helps you, a ⭐ star is always appreciated!
