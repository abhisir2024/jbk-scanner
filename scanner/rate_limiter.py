"""
Fyers API Rate Limiter
======================
Manages API call rate, auto-throttles, retries on failure, and saves checkpoints.

Fyers API limits:
- ~50-100 calls per minute (varies by plan)
- Returns 429 or error "Rate limit exceeded" when exceeded
- History calls: ~20 per second burst, then throttled

Usage:
    from scanner.rate_limiter import RateLimiter

    limiter = RateLimiter(max_per_minute=50)

    def fetch_data(symbol):
        limiter.wait_if_needed()
        try:
            result = fyers.history(data={...})
            limiter.on_success()
            return result
        except Exception as e:
            limiter.on_error(e)
            return None
"""

import json
import os
import time
import threading
from datetime import datetime
from typing import Optional, Callable, Any
from collections import deque


class RateLimiter:
    """Thread-safe rate limiter with auto-throttle, retry, and checkpoint."""

    def __init__(
        self,
        max_per_minute: int = 50,
        burst_per_second: int = 10,
        max_retries: int = 3,
        base_delay: float = 1.0,
        checkpoint_interval: int = 50,
    ):
        self.max_per_minute = max_per_minute
        self.burst_per_second = burst_per_second
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.checkpoint_interval = checkpoint_interval

        # Track call timestamps
        self._call_times: deque = deque()
        self._second_times: deque = deque()
        self._lock = threading.Lock()

        # Stats
        self.total_calls = 0
        self.total_retries = 0
        self.total_throttled = 0
        self.total_errors = 0
        self.total_rate_limited = 0
        self.start_time = time.time()

        # Checkpoint state
        self._checkpoint_file = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "scan_checkpoint.json"
        )
        self._last_checkpoint_time = 0

    def wait_if_needed(self):
        """Block until we can safely make another API call."""
        with self._lock:
            now = time.time()

            # Clean old timestamps (older than 60s)
            while self._call_times and self._call_times[0] < now - 60:
                self._call_times.popleft()

            # Clean old second timestamps (older than 1s)
            while self._second_times and self._second_times[0] < now - 1:
                self._second_times.popleft()

            # Check per-minute limit
            if len(self._call_times) >= self.max_per_minute:
                wait_time = self._call_times[0] + 60 - now
                if wait_time > 0:
                    self.total_throttled += 1
                    print(f"  [WAIT] Rate limit: waiting {wait_time:.1f}s...")
                    time.sleep(wait_time)
                    now = time.time()
                    # Clean again after wait
                    while self._call_times and self._call_times[0] < now - 60:
                        self._call_times.popleft()

            # Check per-second burst limit
            if len(self._second_times) >= self.burst_per_second:
                wait_time = self._second_times[0] + 1 - now
                if wait_time > 0:
                    time.sleep(wait_time)
                    now = time.time()

            # Record this call
            self._call_times.append(now)
            self._second_times.append(now)
            self.total_calls += 1

    def on_success(self):
        """Call after a successful API request."""
        pass  # Success doesn't need special handling

    def on_error(self, error: Exception) -> bool:
        """
        Call after a failed API request.
        Returns True if should retry, False if should skip.
        """
        error_str = str(error).lower()
        self.total_errors += 1

        # Rate limit error — wait and retry
        if "rate" in error_str or "limit" in error_str or "429" in error_str:
            self.total_rate_limited += 1
            wait = min(60, self.base_delay * (2 ** min(self.total_rate_limited, 5)))
            print(f"  [WARN]  Rate limited! Waiting {wait:.0f}s...")
            time.sleep(wait)
            # Clear old timestamps to reset window
            with self._lock:
                self._call_times.clear()
                self._second_times.clear()
            return True

        # Network errors — retry with backoff
        if "connection" in error_str or "timeout" in error_str or "network" in error_str:
            return True

        # Auth errors — don't retry
        if "token" in error_str or "auth" in error_str or "login" in error_str:
            print(f"  [ERROR] Auth error (not retrying): {error}")
            return False

        # Other errors — retry once
        return True

    def retry_call(self, func: Callable, *args, **kwargs) -> Optional[Any]:
        """
        Execute an API call with automatic rate limiting and retry.

        Args:
            func: The function to call (e.g., fyers.history)
            *args, **kwargs: Arguments to pass to func

        Returns:
            Result of func, or None if all retries failed.
        """
        last_error = None

        for attempt in range(self.max_retries + 1):
            self.wait_if_needed()

            try:
                result = func(*args, **kwargs)
                self.on_success()
                return result
            except Exception as e:
                last_error = e
                self.total_retries += 1

                if attempt < self.max_retries:
                    should_retry = self.on_error(e)
                    if not should_retry:
                        return None
                    delay = self.base_delay * (2 ** attempt)
                    print(f"  [RETRY] Retry {attempt + 1}/{self.max_retries} in {delay:.1f}s...")
                    time.sleep(delay)
                else:
                    print(f"  [ERROR] Failed after {self.max_retries} retries: {e}")

        return None

    def save_checkpoint(self, position: int, total: int, results: list):
        """Save scan progress to resume after crash."""
        now = time.time()
        # Don't checkpoint too frequently (min 5 seconds apart)
        if now - self._last_checkpoint_time < 5:
            return

        checkpoint = {
            "position": position,
            "total": total,
            "results_count": len(results),
            "timestamp": datetime.now().isoformat(),
            "stats": self.get_stats(),
        }

        try:
            with open(self._checkpoint_file, "w") as f:
                json.dump(checkpoint, f, indent=2)
            self._last_checkpoint_time = now
        except Exception:
            pass  # Checkpoint failure is non-critical

    def load_checkpoint(self) -> Optional[dict]:
        """Load last checkpoint if it exists and is recent (< 1 hour)."""
        try:
            if not os.path.exists(self._checkpoint_file):
                return None

            with open(self._checkpoint_file) as f:
                checkpoint = json.load(f)

            # Check if checkpoint is recent (within 1 hour)
            ts = datetime.fromisoformat(checkpoint["timestamp"])
            age_minutes = (datetime.now() - ts).total_seconds() / 60

            if age_minutes > 60:
                print(f"  ℹ️  Checkpoint is {age_minutes:.0f} min old, starting fresh")
                return None

            print(f"  [INFO] Resuming from checkpoint: {checkpoint['position']}/{checkpoint['total']}")
            return checkpoint

        except Exception:
            return None

    def clear_checkpoint(self):
        """Clear checkpoint after successful scan completion."""
        try:
            if os.path.exists(self._checkpoint_file):
                os.remove(self._checkpoint_file)
        except Exception:
            pass

    def get_stats(self) -> dict:
        """Get current rate limiter statistics."""
        elapsed = time.time() - self.start_time
        with self._lock:
            now = time.time()
            recent_calls = sum(1 for t in self._call_times if t > now - 60)

        return {
            "total_calls": self.total_calls,
            "total_retries": self.total_retries,
            "total_throttled": self.total_throttled,
            "total_errors": self.total_errors,
            "total_rate_limited": self.total_rate_limited,
            "calls_per_minute": recent_calls,
            "max_per_minute": self.max_per_minute,
            "elapsed_seconds": round(elapsed, 1),
            "calls_per_second": round(self.total_calls / max(elapsed, 1), 2),
        }

    def reset(self):
        """Reset all counters."""
        with self._lock:
            self._call_times.clear()
            self._second_times.clear()
            self.total_calls = 0
            self.total_retries = 0
            self.total_throttled = 0
            self.total_errors = 0
            self.total_rate_limited = 0
            self.start_time = time.time()


# Global rate limiter instance
_global_limiter: Optional[RateLimiter] = None


def get_limiter() -> RateLimiter:
    """Get the global rate limiter instance."""
    global _global_limiter
    if _global_limiter is None:
        _global_limiter = RateLimiter(
            max_per_minute=50,
            burst_per_second=10,
            max_retries=3,
            base_delay=1.0,
        )
    return _global_limiter
