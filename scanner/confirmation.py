"""
Confirmation Filter — accurate-entry rules for options trading.
================================================================
Applies three confirmation rules to every signal. A signal is CONFIRMED only
when it passes ALL rules that apply to its type:

  1. VOLUME GATE  : signal day volume >= vol_mult (2x default) of 20-day average
                    -> real participation, not a fake move.
  2. HOLD CHECK   : price still holds above the reference level (stop / breakout
                    high) and the close is not in the bottom of the candle
                    -> no immediate reversal / failed breakout.
  3. PULLBACK     : entry is within `atr_extension` ATRs of EMA20 (not chased
                    after a big run) -> wait for a pullback to enter.

Each signal is tagged: details["confirmed"] = True/False and
details["conf_rules"] = list of rules that passed.
"""


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


class ConfirmationFilter:
    def __init__(self, vol_mult: float = 1.5, atr_extension: float = 2.5,
                 weak_close_pct: float = 15.0):
        self.vol_mult = vol_mult
        self.atr_extension = atr_extension
        self.weak_close_pct = weak_close_pct

    def filter(self, signals, opens, highs, lows, closes, volumes):
        """Tag signals with confirmation status. Returns the same signal list."""
        from scanner.strategies import _atr, _ema

        n = len(closes)
        if n < 25 or not signals:
            for s in signals:
                s.details["confirmed"] = False
                s.details["conf_rules"] = []
            return signals

        avg_vol = _avg(volumes[-21:-1]) if n >= 21 else _avg(volumes)
        atr_vals = _atr(highs, lows, closes)
        atr = atr_vals[-1] if atr_vals[-1] else closes[-1] * 0.02
        ema20 = _ema(closes, 20)
        ema20_val = ema20[-1] if ema20[-1] else 0

        last_c = closes[-1]
        bar_high, bar_low = highs[-1], lows[-1]
        vol_ratio = volumes[-1] / max(avg_vol, 1)
        mid = (bar_high + bar_low) / 2

        for sig in signals:
            is_buy = sig.signal_type.value == "BUY"
            rules = []

            # 1) VOLUME GATE
            if vol_ratio >= self.vol_mult:
                rules.append("VOL")

            # 2) HOLD CHECK — close on the correct side of the reference level,
            #    and no rejection close (bottom of bar for BUY, top for SELL)
            ref = sig.stop_loss
            d = sig.details or {}
            ref = ref or d.get("range_high") or d.get("recent_high") or d.get("support") or 0
            if is_buy:
                weak_close = last_c < mid * (1 - self.weak_close_pct / 100)
                hold_ok = last_c > ref and not weak_close
            else:
                weak_close = last_c > mid * (1 + self.weak_close_pct / 100)
                hold_ok = last_c < ref and not weak_close
            if hold_ok:
                rules.append("HOLD")

            # 3) PULLBACK ENTRY — not over-extended from EMA20 (don't chase)
            if ema20_val > 0:
                extended = (last_c - ema20_val) > atr * self.atr_extension if is_buy else (ema20_val - last_c) > atr * self.atr_extension
            else:
                extended = False
            if not extended:
                rules.append("PB")

            sig.details["confirmed"] = len(rules) >= 3
            sig.details["conf_rules"] = rules
            sig.details["vol_ratio"] = round(vol_ratio, 2)

        return signals
