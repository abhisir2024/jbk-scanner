# Unusual Options Activity (UOA) Tracker

## What is Unusual Options Activity?

When someone buys **large quantities of options in a single order**, it often signals:
- **Institutional activity** (mutual funds, FIIs, DIIs)
- **Smart money** positioning before events
- **Insider knowledge** (earnings, corporate actions)
- **Hedging** by large portfolio managers

## What We Track

| Signal | What It Means | Example |
|--------|---------------|---------|
| **Unusual Volume** | Volume > 2x average at a strike | 50,000 CE bought vs 20,000 avg |
| **Large OI Change** | Open interest jumps significantly | OI increases by 10,000 in one day |
| **Premium Spike** | Option premium jumps suddenly | ₹50 → ₹80 in 10 minutes |
| **IV Spike** | Implied volatility increases | IV from 15% to 25% |
| **Call/Put Ratio** | Skew in call vs put buying | 80% calls, 20% puts = bullish |

## Data Sources Needed

### 1. Option Chain Data (Fyers API) ✅
```python
# Fyers has optionchain endpoint
fyers.optionchain(data={"symbol": "NSE:NIFTY50-INDEX"})
```
**Returns:** Strike prices, LTP, volume, OI for all strikes

### 2. Real-time Trade Data (Fyers WebSocket)
```python
# Subscribe to option symbols for real-time trades
# Track individual large orders as they happen
```
**Returns:** Live trade feed with quantity, price, timestamp

### 3. Open Interest History (NSE Website)
```python
# Scrape NSE for daily OI data
# Compare today's OI with yesterday's
```
**Returns:** Historical OI for change calculation

### 4. Implied Volatility (Sensibull/Opstra)
```python
# Get IV data from options analytics platforms
# Track IV changes before events
```
**Returns:** IV for each strike, IV percentile, IV rank

## How to Detect Large Single Orders

### Method 1: Volume Spike
```
If (current_volume > 2 * average_volume) then
    FLAG as unusual
```

### Method 2: OI Change
```
If (today_OI - yesterday_OI > threshold) then
    NEW positions opened (not just rolling)
```

### Method 3: Time & Sales Analysis
```
Monitor real-time trades:
- Single trade > 1000 lots = LARGE ORDER
- Multiple large trades in short time = INSTITUTIONAL
```

### Method 4: Bid-Ask Spread
```
If (bid-ask spread narrows + volume spikes) then
    Institutional buyer absorbing all offers
```

## Example Scenarios

### Scenario 1: Bullish Signal 🔥
```
NIFTY 25000 CE (Weekly Expiry)
- Volume: 150,000 (avg: 40,000) → 3.75x
- OI Change: +25,000
- Premium: ₹120 → ₹180 (+50%)
- Score: 92/100

Interpretation: Someone bought 25,000 lots of 25000 CE
→ Bullish on NIFTY crossing 25000
```

### Scenario 2: Bearish Signal 📉
```
RELIANCE 2900 PE (Monthly Expiry)
- Volume: 80,000 (avg: 15,000) → 5.3x
- OI Change: +18,000
- Premium: ₹45 → ₹75 (+67%)
- Score: 88/100

Interpretation: Someone bought 18,000 lots of 2900 PE
→ Bearish on RELIANCE falling below 2900
```

### Scenario 3: Neutral/Straddle 📊
```
TCS 3500 CE + 3500 PE
- Both calls and puts showing unusual activity
- Score: 75/100 each

Interpretation: Expecting big move in either direction
→ Possibly before earnings announcement
```

## Integration with Scanner

The UOA tracker can enhance your scanner by:

1. **Confirming Breakout Signals**
   - If stock breaks out + unusual call buying = STRONG BUY
   - If stock breaks down + unusual put buying = STRONG SELL

2. **Early Warning System**
   - Unusual activity often happens 1-3 days before big moves
   - Can give early entry signals

3. **Risk Management**
   - If unusual put buying detected on your holdings = EXIT signal
   - If unusual call buying on watchlist = ENTRY signal

## Current Limitations

| Issue | Impact | Solution |
|-------|--------|----------|
| Fyers option chain API format unclear | Can't parse data | Need to test with correct parameters |
| No real-time WebSocket for options | Delayed data | Use Fyers WebSocket API |
| No historical OI data | Can't calculate OI change | Scrape NSE or use paid API |
| No IV data | Can't detect IV spikes | Integrate with Sensibull/Opstra |

## Next Steps

1. **Test Fyers option chain API** with correct parameters
2. **Build real-time WebSocket** for live option trades
3. **Scrape NSE** for daily OI data
4. **Integrate with scanner** to confirm signals
5. **Add Telegram alerts** for unusual activity

## Files

| File | Description |
|------|-------------|
| `scanner/options_tracker.py` | Main UOA tracker module |
| `UNUSUAL_OPTIONS_TRACKER.md` | This file |

## Commands

```bash
# Scan all F&O stocks for unusual activity
python -m scanner.options_tracker

# Scan specific symbol
python -m scanner.options_tracker --symbol NIFTY50

# Continuous monitoring (every 5 minutes)
python -m scanner.options_tracker --watch

# Custom minimum score
python -m scanner.options_tracker --score 70
```
