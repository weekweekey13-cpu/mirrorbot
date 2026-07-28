# Deploy mirror-bot → GitHub + Render (free) + UptimeRobot

## 1. GitHub
Private repo with this project. Do **not** commit `.env` or `user.session`.

## 2. Render
1. https://dashboard.render.com → New → Web Service
2. Connect GitHub repo
3. Settings:
   - **Runtime:** Python 3
   - **Build:** `pip install -r requirements.txt`
   - **Start:** `python -u bot.py`
   - **Plan:** Free
4. Environment variables:

| Key | Value |
|-----|--------|
| `BOT_TOKEN` | from local `.env` |
| `API_ID` | from local `.env` |
| `API_HASH` | from local `.env` |
| `DATA_FILE` | `data/config.json` |
| `SESSION_B64` | contents of `SESSION_B64.txt` (generate locally) |
| `CONFIG_JSON` | optional: full `data/config.json` text |

5. Deploy → open service URL → should show `ok`

## 3. UptimeRobot (anti-sleep)
Free Render sleeps ~15 min without HTTP traffic.

1. https://uptimerobot.com → Add Monitor
2. Type: **HTTP(s)**
3. URL: `https://YOUR-APP.onrender.com/health`
4. Interval: **5 minutes**
5. Create

Keep the monitor ON. Bot stays warm.

## Generate SESSION_B64 (Windows PowerShell)
```powershell
cd $env:USERPROFILE\Desktop\mirror-bot
[Convert]::ToBase64String([IO.File]::ReadAllBytes("data\user.session")) | Set-Content SESSION_B64.txt
```
Copy the file contents into Render env `SESSION_B64` (one long line).
