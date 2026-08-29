# Fyers Scanner - Signal Quality Analysis

## Backtest Results (5 Major Stocks, 252 Days)

### Overall Performance
| Metric | Value |
|--------|-------|
| Total Trades | 88 |
| Win Rate | 33.0% |
| Avg P&L/Trade | -1.52% |
| Total P&L | -133.47% |
| Profit Factor | 0.49 |

**Verdict: UNDERPERFORMING** - Scanner is generating losing signals overall.

---

## Strategy Performance

### 1. 52W High Support Buy - POOR
| Metric | Value |
|--------|-------|
| Trades | 22 |
| Win Rate | 18.2% |
| Avg P&L | -5.31% |
| Profit Factor | 0.20 |

**Issues:**
- Buying dips in downtrends (catching falling knives)
- Only 18% win rate - terrible
- Many signals triggered when stock was 8-10% from 52W high (too far)
- No trend confirmation - signals fire in downtrends

### 2. Early Breakout - POOR
| Metric | Value |
|--------|-------|
| Trades | 20 |
| Win Rate | 40.0% |
| Avg P&L | -0.47% |
| Profit Factor | 0.70 |

**Issues:**
- Better than 52W High but still losing
- Many false breakouts - price approaches range high but doesn't break
- Volume confirmation too weak

### 3. Range Breakout 15D - BEST (but still losing)
| Metric | Value |
|--------|-------|
| Trades | 46 |
| Win Rate | 37.0% |
| Avg P&L | -0.16% |
| Profit Factor | 0.91 |

**Issues:**
- Close to breakeven - needs optimization
- Too many false breakouts
- Stop loss too tight (gets stopped out before move)

---

## Current Signal Quality (Today's Scan)

### GOOD Signals (near 52W high, above EMA, uptrend):
- TITAN: 2.8% from high, above EMA20, +6% 20d trend
- JSWSTEEL: 3.2% from high, above EMA20, +3.9% 20d trend
- BAJFINANCE: 6.7% from high, above EMA20, +4.7% 20d trend

### QUESTIONABLE Signals:
- ICICIBANK: 4.2% from high, but below EMA20 (-0.3%)
- ADANIENT: 8% from high, below EMA20 (-1.2%)
- SUNPHARMA: 7.2% from high, below EMA20, downtrend (-3.8%)

### Issues Found:
1. **Only 52W High Support Buy is triggering** - no breakout signals
2. **Volume filter too loose** - signals fire with 0.1x average volume
3. **No trend confirmation** - some signals in downtrends

---

## Root Cause Analysis

### Why 52W High Support Buy Fails:
1. **No trend filter** - buys dips even in strong downtrends
2. **Distance threshold too wide** (10%) - should be 5-7%
3. **Support detection too loose** - "at EMA" check has 1% tolerance
4. **No momentum confirmation** - doesn't check RSI or MACD

### Why Breakout Strategies Underperform:
1. **False breakouts** - no retest confirmation
2. **Stop loss too tight** - gets stopped out before move completes
3. **No multi-timeframe confirmation** - daily breakout without 15min confirmation

---

## Improvement Recommendations

### Priority 1: Fix 52W High Support Buy (Highest Impact)
```python
# ADD these filters:
1. TREND FILTER: Only buy if price > EMA50 (uptrend)
2. RSI FILTER: Only buy if RSI > 40 (not oversold in downtrend)
3. REDUCE DISTANCE: proximity_pct from 10% to 7%
4. TIGHTEN SUPPORT: EMA tolerance from 1% to 0.5%
5. VOLUME CONFIRMATION: Require volume > 0.8x average
```

### Priority 2: Improve Breakout Strategies
```python
# ADD these filters:
1. RETEST CONFIRMATION: Wait for price to retest breakout level
2. MULTI-TIMEFRAME: Only trigger if 15min also shows breakout
3. WIDER STOP LOSS: Use 1.5x ATR instead of 0.5x ATR
4. VOLUME TREND: Require 3 days of increasing volume
```

### Priority 3: Add New Strategies
```python
# Consider adding:
1. VWAP REVERSAL - price crosses VWAP with volume (intraday)
2. RSI DIVERGENCE - price makes new low but RSI doesn't
3. MOVING AVERAGE CONFLUENCE - 20/50/200 EMA alignment
```

### Priority 4: Signal Quality Filters
```python
# ADD overall filters:
1. MARKET TREND: Only take BUY signals if NIFTY50 > EMA20
2. SECTOR STRENGTH: Only buy stocks in strong sectors
3. CORRELATION: Avoid multiple signals in same sector
4. RISK MANAGEMENT: Max 3 open positions at a time
```

---

## Action Plan

### Immediate (This Week):
1. Add trend filter to 52W High Support Buy
2. Add RSI confirmation to all strategies
3. Reduce 52W High proximity from 10% to 7%
4. Test changes with backtest

### Short-term (Next Week):
1. Add multi-timeframe confirmation
2. Implement retest confirmation for breakouts
3. Add position sizing rules

### Medium-term (This Month):
1. Add VWAP and RSI Divergence strategies
2. Build signal scoring system (0-100 quality score)
3. Create automated backtesting pipeline

---

## Files Created

| File | Purpose |
|------|---------|
| `scanner/tracker.py` | Signal tracking and outcome logging |
| `track_signals.py` | CLI for tracking signals |
| `signal_history.json` | Auto-created signal log |
| `SCANNER_ANALYSIS.md` | This analysis report |

## Usage

```bash
# Scan and log signals
python track_signals.py scan

# Update prices for active signals
python track_signals.py update

# View performance report
python track_signals.py report

# Run backtest and log trades
python track_signals.py backtest

# View signal history
python track_signals.py history
```

---

## Key Takeaway

**Current scanner is NOT profitable.** The 52W High Support Buy strategy has only 18% win rate. Need to add trend filters and momentum confirmation before using for real trading.

**Next step:** Implement Priority 1 fixes (trend filter + RSI) and re-run backtest.
