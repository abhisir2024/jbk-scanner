# JBK Scanner — TradingView & Fyers Integration

## TradingView Pine Scripts

Copy any `.pine` file content into TradingView → Pine Editor → Add to Chart.

### Available Indicators

| File | Strategy | Description |
|------|----------|-------------|
| `jbk_master_scanner.pine` | **ALL STRATEGIES** | Combined master indicator (recommended) |
| `range_breakout.pine` | Range Breakout | 9D/15D/21D/60D range breakouts |
| `high_52w_support.pine` | 52W High Support | Buy near 52-week high with filters |
| `channel_breakout.pine` | Channel Breakout | Bollinger Band squeeze breakout |
| `volume_momentum.pine` | Volume & Momentum | Volume Shocker + Momentum + Retracement |
| `index_options.pine` | Index Options | NIFTY/BANKNIFTY regime + entry system |

---

## How to Install

### Step 1: Open TradingView
1. Go to [tradingview.com](https://tradingview.com)
2. Open any chart (e.g., NIFTY 50, RELIANCE)

### Step 2: Open Pine Editor
1. Click **Pine Editor** at the bottom of the screen
2. Click **Open** → **New indicator**

### Step 3: Paste Script
1. Copy the content of any `.pine` file
2. Paste into the Pine Editor
3. Click **Add to Chart**

### Step 4: Configure
1. Click the **Settings** icon on the indicator
2. Toggle strategies on/off
3. Adjust filters and risk management

---

## Alert Setup

### Method 1: TradingView Alerts
1. Right-click on indicator → **Add Alert**
2. Set **Condition** to the indicator
3. Choose notification:
   - **Webhook** → Send to your server
   - **Email** → TradingView sends email
   - **Push** → TradingView mobile app

### Method 2: Webhook to Telegram
```python
# Receive TradingView alerts and send to Telegram
from flask import Flask, request
import requests

app = Flask(__name__)

@app.route('/tradingview-webhook', methods=['POST'])
def handle_webhook():
    data = request.json
    bot_token = "8735165009:AAFvM5f-IbXULBsTw3kKLUsAU7HOt7r2d4c"
    chat_id = "JBK21_bot"
    
    msg = f"🔔 {data['action']} {data['symbol']} @ {data['price']}"
    requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
                  json={"chat_id": chat_id, "text": msg})
    return "OK"
```

---

## Desktop Setup Guide

### Multi-Monitor Layout
```
Monitor 1: TradingView (Master Scanner on daily chart)
Monitor 2: TradingView (15-min chart for entries)
Monitor 3: Fyers Desktop (execution)
```

### Recommended Charts
| Chart | Timeframe | Indicator | Purpose |
|-------|-----------|-----------|---------|
| NIFTY 50 | Daily | Master Scanner | Regime + signals |
| NIFTY 50 | 15-min | Index Options | Entry timing |
| RELIANCE | Daily | Range Breakout | Breakout trades |
| BANKNIFTY | Daily | Master Scanner | Index trades |
| Your watchlist | Daily | Master Scanner | Stock picks |

### Hotkeys
- `Alt + A` → Add alert
- `Alt + R` → Remove alert
- `Ctrl + S` → Save chart layout

---

## Strategy Comparison

| Strategy | TradingView | Fyers Scanner | Python Scanner |
|----------|-------------|---------------|----------------|
| Range Breakout 9D | ✅ | ✅ | ✅ |
| Range Breakout 15D | ✅ | ✅ | ✅ |
| Range Breakout 21D | ✅ | ✅ | ✅ |
| Range Breakout 60D | ✅ | ✅ | ✅ |
| 52W High Support | ✅ | ✅ | ✅ |
| Channel Breakout | ✅ | ✅ | ✅ |
| Early Breakout | ✅ | ✅ | ✅ |
| Volume Shocker | ✅ | ✅ | ✅ |
| Momentum | ✅ | ✅ | ✅ |
| Retracement | ✅ | ✅ | ✅ |
| Candlestick | ✅ | ✅ | ✅ |
| Index Options | ✅ | ❌ | ✅ |
| Quality Scoring | ❌ | ❌ | ✅ |
| Backtest | ❌ | ❌ | ✅ |
| Big Money | ❌ | ❌ | ✅ |

---

## Fyers Scanner Conditions

See `fyers_scanner_conditions.md` for all conditions to use in the Fyers Scanner platform.

### Quick Setup
1. Open Fyers Web Scanner
2. Create New Screener
3. Add conditions from the markdown file
4. Set universe to F&O Stocks
5. Save and run

---

## Python → TradingView Integration

### Send Python Scanner Signals to TradingView
```python
# In your scanner, after generating signals:
import requests

def send_to_tradingview_webhook(signal):
    webhook_url = "https://your-webhook-url.com"
    payload = {
        "symbol": signal["symbol"],
        "action": signal["signal_type"],
        "price": signal["price"],
        "strategy": signal["strategy"],
        "sl": signal["stop_loss"],
        "target": signal["target"]
    }
    requests.post(webhook_url, json=payload)
```

---

## Tips

1. **Start with Master Scanner** — it combines all strategies
2. **Use alerts** — don't watch charts all day
3. **Check timeframe** — daily for direction, 15-min for entry
4. **Risk management** — always use SL/Target lines
5. **Combine with Python scanner** — for quality scoring and backtesting
