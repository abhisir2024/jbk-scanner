# JBK Scanner — Fyers Platform Conditions

## How to Use in Fyers Scanner

1. Open **Fyers Web Scanner** or **Fyers Desktop**
2. Create **New Screener**
3. Add conditions from below
4. Set universe to **F&O Stocks** (208 stocks)
5. Set timeframe to **Daily**
6. Save and run

---

## Strategy 1: Range Breakout (9D/15D/21D/60D)

### Range Breakout 9D
```
CLOSE > HIGHEST(HIGH, 9)[1]
VOLUME > SMA(VOLUME, 20) * 0.5
EMA(50) > EMA(50)[5]  // Rising EMA50
```

### Range Breakout 15D
```
CLOSE > HIGHEST(HIGH, 15)[1]
VOLUME > SMA(VOLUME, 20) * 0.5
EMA(50) > EMA(50)[5]
```

### Range Breakout 21D
```
CLOSE > HIGHEST(HIGH, 21)[1]
VOLUME > SMA(VOLUME, 20) * 0.5
EMA(50) > EMA(50)[5]
```

### Range Breakout 60D
```
CLOSE > HIGHEST(HIGH, 60)[1]
VOLUME > SMA(VOLUME, 20) * 0.5
EMA(50) > EMA(50)[5]
```

---

## Strategy 2: 52W High Support Buy

```
CLOSE > HIGHEST(HIGH, 252) * 0.95  // Within 5% of 52W high
EMA(50) > EMA(50)[5]               // EMA50 rising
MACD Histogram > 0                  // MACD positive
RSI(14) > 45                        // RSI building
VOLUME > SMA(VOLUME, 20) * 0.8      // Volume OK
```

---

## Strategy 3: Channel Consolidation Breakout

```
// Bollinger Band Squeeze
BBWIDTH(20, 2) < 0.03              // BB width narrow
// Breakout
CLOSE > BB UPPER(20, 2)            // Close above upper BB
RSI(14) > 50                        // RSI confirms
VOLUME > SMA(VOLUME, 20)           // Volume expansion
```

---

## Strategy 4: Early Breakout

```
CLOSE > HIGHEST(HIGH, 20) * 0.98   // Within 2% of 20D high
VOLUME > SMA(VOLUME, 20) * 1.0      // Volume rising
CLOSE > CLOSE[1]                     // Higher close
CLOSE[1] > CLOSE[2]                 // 2 consecutive higher closes
EMA(50) > EMA(50)[5]               // Trend up
RSI(14) > 45                        // Momentum building
```

---

## Strategy 5: Volume Shocker

```
VOLUME > SMA(VOLUME, 20) * 2.0      // 2x volume spike
CLOSE > CLOSE[1]                     // Green candle
CLOSE > EMA(20)                     // Above EMA20
EMA(50) > EMA(50)[5]               // Trend up
```

---

## Strategy 6: Momentum Breakout

```
CLOSE > EMA(9)                      // Above fast EMA
EMA(9) > EMA(20)                    // Fast above medium
EMA(20) > EMA(50)                   // Medium above slow
MACD Histogram > 0                   // MACD positive
MACD Histogram > MACD Histogram[1]  // MACD rising
VOLUME > SMA(VOLUME, 20)           // Volume OK
```

---

## Strategy 7: Buy on Retracement

```
// Price at 50% Fibonacci retracement
CLOSE > (LOWEST(LOW, 10) + (HIGHEST(HIGH, 10) - LOWEST(LOW, 10)) * 0.45)
CLOSE < (LOWEST(LOW, 10) + (HIGHEST(HIGH, 10) - LOWEST(LOW, 10)) * 0.55)
EMA(50) > EMA(50)[5]               // Trend up
RSI(14) > 40 AND RSI(14) < 60      // Neutral RSI
VOLUME > SMA(VOLUME, 20) * 0.8     // Volume OK
```

---

## Quick Reference — Combined Filters

### Buy Signal (any strategy)
```
EMA(50) > EMA(50)[5]               // Trend filter
RSI(14) > 40                        // Momentum filter
VOLUME > SMA(VOLUME, 20) * 0.8     // Volume filter
```

### Strong Buy (2+ strategies agree)
```
// Apply any 2 of the above strategy conditions together
```

---

## Recommended F&O Universe

Set stock universe to: **F&O Stocks** (208 stocks)

### Key Stocks to Watch
- **Banking**: HDFCBANK, ICICIBANK, KOTAKBANK, SBIN, AXISBANK
- **IT**: TCS, INFY, WIPRO, HCLTECH, TECHM
- **Auto**: MARUTI, BAJAJ-AUTO, M&M, TATAMOTORS
- **Metal**: TATASTEEL, HINDALCO, JSWSTEEL, VEDL
- **Pharma**: SUNPHARMA, DRREDDY, CIPLA
- **Finance**: BAJFINANCE, BAJAJFINSV, SBICARD

---

## Fyers Scanner API (Python)

```python
from fyers_apiv3 import fyers

# Create scanner
scanner = fyers.Scanner()

# Add conditions
scanner.add_condition({
    "field": "close",
    "operation": ">",
    "value": "highest(high, 21)[1]"
})

# Run scan
results = scanner.run()
```
