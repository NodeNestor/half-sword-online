"""Adaptive bitrate controller for remote play streaming.

Adjusts the encoding bitrate based on network conditions (RTT, packet loss,
client decode time) using a conservative AIMD-like algorithm: decrease
quickly on congestion, increase slowly when the link is clear.
"""

import logging
import threading
import time
from collections import deque
from typing import Optional

log = logging.getLogger(__name__)

# How long after a decrease before we allow increases (seconds).
_COOLDOWN_SECS = 3.0

# Exponential moving average smoothing factor (0..1, higher = more responsive).
_EMA_ALPHA = 0.3

# Window size for RTT / loss history (number of samples).
_HISTORY_SIZE = 60


class AdaptiveBitrateController:
    """Decides the target bitrate based on live network stats.

    Thread-safe: :meth:`update` and :meth:`get_target_bitrate` may be
    called from different threads.

    Parameters
    ----------
    min_bitrate_kbps:
        Floor for the bitrate.
    max_bitrate_kbps:
        Ceiling for the bitrate.
    initial_bitrate_kbps:
        Starting bitrate.  Clamped to [min, max].
    """

    def __init__(
        self,
        min_bitrate_kbps: int = 500,
        max_bitrate_kbps: int = 15_000,
        initial_bitrate_kbps: int = 6_000,
    ) -> None:
        self.min_bitrate = min_bitrate_kbps
        self.max_bitrate = max_bitrate_kbps

        self._lock = threading.Lock()
        self._bitrate = float(max(min_bitrate_kbps, min(initial_bitrate_kbps, max_bitrate_kbps)))
        self._smoothed_bitrate = self._bitrate

        # Timestamps
        self._last_decrease_time: float = 0.0
        self._last_update_time: float = 0.0

        # Rolling history for statistics
        self._rtt_history: deque[float] = deque(maxlen=_HISTORY_SIZE)
        self._loss_history: deque[float] = deque(maxlen=_HISTORY_SIZE)
        self._decode_history: deque[float] = deque(maxlen=_HISTORY_SIZE)

        # EMA-smoothed stats
        self._avg_rtt: float = 0.0
        self._avg_loss: float = 0.0
        self._avg_decode: float = 0.0
        self._jitter: float = 0.0  # RTT variance proxy
        self._prev_rtt: Optional[float] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, rtt_ms: float, packet_loss_pct: float, decode_time_ms: float = 0.0) -> None:
        """Feed fresh network / client stats and recompute the target bitrate.

        Parameters
        ----------
        rtt_ms:
            Latest round-trip time in milliseconds.
        packet_loss_pct:
            Recent packet loss as a percentage (0-100).
        decode_time_ms:
            Client-reported decode time for the last frame (ms).
            Used as an auxiliary quality signal — high decode times
            suggest the client is struggling.
        """
        with self._lock:
            now = time.monotonic()
            self._last_update_time = now

            # ---- Update history / EMA stats ----
            self._rtt_history.append(rtt_ms)
            self._loss_history.append(packet_loss_pct)
            self._decode_history.append(decode_time_ms)

            self._avg_rtt = _ema(self._avg_rtt, rtt_ms, _EMA_ALPHA)
            self._avg_loss = _ema(self._avg_loss, packet_loss_pct, _EMA_ALPHA)
            self._avg_decode = _ema(self._avg_decode, decode_time_ms, _EMA_ALPHA)

            if self._prev_rtt is not None:
                self._jitter = _ema(self._jitter, abs(rtt_ms - self._prev_rtt), _EMA_ALPHA)
            self._prev_rtt = rtt_ms

            # ---- Decide adjustment ----
            in_cooldown = (now - self._last_decrease_time) < _COOLDOWN_SECS

            if packet_loss_pct > 5.0:
                # Severe congestion — cut hard
                self._apply_decrease(0.20)
                self._last_decrease_time = now
                log.debug("ABR: loss %.1f%% > 5%% — decrease 20%%", packet_loss_pct)

            elif packet_loss_pct > 2.0:
                # Moderate congestion
                self._apply_decrease(0.10)
                self._last_decrease_time = now
                log.debug("ABR: loss %.1f%% > 2%% — decrease 10%%", packet_loss_pct)

            elif decode_time_ms > 30.0:
                # Client struggling to keep up
                self._apply_decrease(0.05)
                self._last_decrease_time = now
                log.debug("ABR: decode %.1fms high — decrease 5%%", decode_time_ms)

            elif not in_cooldown and packet_loss_pct < 0.5 and self._is_rtt_stable():
                # Link looks healthy — slowly probe upward
                self._apply_increase(0.05)
                log.debug("ABR: link clear — increase 5%%")

            # ---- Smooth final value ----
            self._smoothed_bitrate = _ema(
                self._smoothed_bitrate, self._bitrate, _EMA_ALPHA
            )

    def get_target_bitrate(self) -> int:
        """Return the current target bitrate in kbps (integer)."""
        with self._lock:
            return int(self._smoothed_bitrate)

    def get_stats(self) -> dict:
        """Return a snapshot of monitoring statistics.

        Useful for logging, UI overlays, or telemetry.
        """
        with self._lock:
            return {
                "target_bitrate_kbps": int(self._smoothed_bitrate),
                "raw_bitrate_kbps": int(self._bitrate),
                "avg_rtt_ms": round(self._avg_rtt, 1),
                "jitter_ms": round(self._jitter, 1),
                "avg_loss_pct": round(self._avg_loss, 2),
                "avg_decode_ms": round(self._avg_decode, 1),
                "loss_trend": self._loss_trend(),
                "rtt_samples": len(self._rtt_history),
            }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _apply_decrease(self, fraction: float) -> None:
        """Multiplicative decrease by *fraction* (e.g. 0.20 = drop 20%)."""
        self._bitrate *= (1.0 - fraction)
        self._clamp()

    def _apply_increase(self, fraction: float) -> None:
        """Additive increase by *fraction* of current bitrate."""
        self._bitrate *= (1.0 + fraction)
        self._clamp()

    def _clamp(self) -> None:
        self._bitrate = max(float(self.min_bitrate), min(self._bitrate, float(self.max_bitrate)))

    def _is_rtt_stable(self) -> bool:
        """Return True if RTT jitter is low relative to the average RTT."""
        if self._avg_rtt < 1.0:
            return True  # Essentially zero RTT — stable enough
        return self._jitter < (self._avg_rtt * 0.25)

    def _loss_trend(self) -> str:
        """Simple trend indicator: 'improving', 'stable', or 'worsening'."""
        if len(self._loss_history) < 10:
            return "insufficient_data"
        recent = list(self._loss_history)
        first_half = sum(recent[: len(recent) // 2]) / (len(recent) // 2)
        second_half = sum(recent[len(recent) // 2 :]) / (len(recent) - len(recent) // 2)
        diff = second_half - first_half
        if diff > 0.5:
            return "worsening"
        elif diff < -0.5:
            return "improving"
        return "stable"


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _ema(prev: float, new: float, alpha: float) -> float:
    """Exponential moving average update."""
    return alpha * new + (1.0 - alpha) * prev
