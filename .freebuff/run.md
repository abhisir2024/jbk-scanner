# Fyers Stock Scanner — Preview Run Doc

## How to Reproduce

The dashboard is a Python HTTP server (no npm/build needed).

1. Copy `.env` from the main checkout if missing (contains Fyers API credentials, Telegram bot token, Gmail app password)
2. Install Python dependencies: `pip install -r requirements.txt`
3. Run `python daily_login.py` to get a fresh Fyers API token (needed once per market day)

## How to Run the Server

```bash
cd "E:\Fyers API"
python dashboard.py --port 5001
```

The server starts on http://127.0.0.1:5001

### Key Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Dashboard HTML (v2 with sparklines) |
| `GET /api/signals` | Current scan results (JSON) |
| `GET /api/scan?timeframe=D` | Trigger a new scan (D, 15min, 5min) |
| `GET /api/status` | Scanner status (scanning, last_scan, signal_count) |
| `GET /api/auto?enable=true` | Toggle auto-scan during market hours |
| `GET /api/history?symbol=NSE:TCS-EQ&days=30` | 30-day OHLCV data for sparkline charts |

### Notes

- Port 5001 is the default; use `--port` to change
- Dashboard polls `/api/signals` every 15s for auto-refresh
- Sparkline charts are cached client-side per symbol
- Market hours: 9:15 AM - 3:30 PM IST (auto-scan only runs during these hours)
- **Critical**: Fyers API max is 365 days for daily candles. Beyond that returns 0 candles. Scanner fetches 365 days to ensure proper 52W high calculation (~248 candles).
