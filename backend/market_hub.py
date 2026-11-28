"""Lightweight in-memory SharedMarketHub, per-account ordering locks, bounded cycle executor,
and event-invalidated read-only query snapshot cache for BTC Paper Lab V3.1.

Design principles (zero microservices / zero Redis / zero Kafka):
- Single unified market data hub: shares validated quote + incrementally maintained closed
  15m/1h/4h/1d bars + shared derived indicator contexts in memory (zero duplicate exchange
  requests, zero redundant HTF aggregation or indicator recomputation across 38/50/100/200 strategies).
- Per-account sequential execution locks (`AccountLockRegistry`): guarantees atomic, ordered
  open/close/protection/ledger updates within any single strategy account, with clean `remove()`
  for dynamic strategy unloading.
- Bounded concurrent cycle fan-out (`BoundedCycleExecutor`): prevents a slow or failing strategy
  from blocking other strategies.
- Query isolation (`ReadOnlySnapshotCache`): event-invalidated + TTL snapshot cache with version
  and freshness metadata for Desktop Console, Research Center V3, Live K-line, and GrokBot read-only APIs.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from strategies.r_series.indicators import aggregate_closed_htf, normalize_closed_15m_bar

UTC = timezone.utc


class SharedMarketHub:
    """Single in-memory market & derived-context hub shared across all strategies and query endpoints."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._latest_quote: Optional[Dict[str, Any]] = None
        self._bars_15m: List[Dict[str, Any]] = []
        self._bars_15m_by_t: Dict[int, Dict[str, Any]] = {}
        self._latest_15m_close_ms: Optional[int] = None
        self._htf_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._context_cache: Dict[str, Tuple[Optional[int], Any]] = {}
        self.htf_build_count: int = 0
        self.htf_cache_hits: int = 0
        self.context_build_count: int = 0
        self.context_cache_hits: int = 0
        self.incremental_appends: int = 0
        self.full_rebuilds: int = 0

    def set_quote(self, quote: Optional[Dict[str, Any]]) -> None:
        with self._lock:
            self._latest_quote = quote

    def get_quote(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self._latest_quote) if self._latest_quote else None

    @property
    def latest_15m_close_ms(self) -> Optional[int]:
        with self._lock:
            return self._latest_15m_close_ms

    @staticmethod
    def _ensure_norm(bar: Dict[str, Any]) -> Dict[str, Any]:
        if "t" in bar and "close_ms" in bar and "o" in bar and "c" in bar:
            return bar
        return normalize_closed_15m_bar(bar)

    def update_15m_bars(self, bars_15m: List[Dict[str, Any]]) -> int:
        """Update shared normalized closed 15m bar series.

        Fast incremental path: when existing bars are already loaded and only new tail bars
        arrive (or the same list with 1 new bar is passed), normalize ONLY the new tail bars
        instead of re-normalizing thousands of historical bars on every 15m close.
        """
        if not bars_15m:
            return 0
        with self._lock:
            if self._bars_15m and self._latest_15m_close_ms is not None:
                # Check if incoming list is strictly an extension or subset with new tail bars
                last_raw = bars_15m[-1]
                if last_raw.get("is_closed") is not False:
                    last_norm = self._ensure_norm(last_raw)
                    if last_norm["close_ms"] == self._latest_15m_close_ms and len(bars_15m) == len(self._bars_15m):
                        return len(self._bars_15m)
                    # Scan from tail to find only bars newer than our latest_15m_close_ms
                    new_tail: List[Dict[str, Any]] = []
                    for raw in reversed(bars_15m):
                        if raw.get("is_closed") is False:
                            continue
                        nb = self._ensure_norm(raw)
                        if nb["close_ms"] <= self._latest_15m_close_ms:
                            break
                        new_tail.append(nb)
                    if new_tail and len(new_tail) <= 16:
                        new_tail.reverse()
                        for nb in new_tail:
                            if nb["t"] not in self._bars_15m_by_t:
                                self._bars_15m_by_t[nb["t"]] = nb
                                self._bars_15m.append(nb)
                        self._bars_15m.sort(key=lambda x: x["t"])
                        self._latest_15m_close_ms = self._bars_15m[-1]["close_ms"]
                        self._htf_cache.clear()
                        self._context_cache.clear()
                        self.incremental_appends += 1
                        return len(self._bars_15m)

        # Full load / bootstrap path (runs once on startup or after large backfill)
        norm = [self._ensure_norm(b) for b in bars_15m if b.get("is_closed") is not False]
        if not norm:
            return 0
        by_t = {b["t"]: b for b in norm}
        ordered = [by_t[t] for t in sorted(by_t.keys())]
        latest_ms = ordered[-1]["close_ms"]
        with self._lock:
            if self._latest_15m_close_ms != latest_ms or len(ordered) != len(self._bars_15m):
                self._bars_15m = ordered
                self._bars_15m_by_t = by_t
                self._latest_15m_close_ms = latest_ms
                self._htf_cache.clear()
                self._context_cache.clear()
                self.full_rebuilds += 1
        return len(ordered)

    def append_15m_bars(self, recent_raw_bars: List[Dict[str, Any]]) -> int:
        """Incrementally merge only recent closed 15m bars into the hub without full archive reload."""
        if not recent_raw_bars:
            with self._lock:
                return len(self._bars_15m)
        changed = False
        with self._lock:
            for raw in recent_raw_bars:
                if raw.get("is_closed") is False:
                    continue
                nb = self._ensure_norm(raw)
                prev = self._bars_15m_by_t.get(nb["t"])
                if prev is None or prev["close_ms"] != nb["close_ms"] or prev["c"] != nb["c"]:
                    self._bars_15m_by_t[nb["t"]] = nb
                    changed = True
            if changed:
                self._bars_15m = [self._bars_15m_by_t[t] for t in sorted(self._bars_15m_by_t.keys())]
                self._latest_15m_close_ms = self._bars_15m[-1]["close_ms"]
                self._htf_cache.clear()
                self._context_cache.clear()
                self.incremental_appends += 1
            return len(self._bars_15m)

    def get_bars(self, timeframe: str = "15m", *, up_to_close_ms: Optional[int] = None) -> List[Dict[str, Any]]:
        """Return shared closed bars for 15m / 1h / 4h / 1d (aggregated once per 15m close)."""
        tf = timeframe.lower()
        with self._lock:
            if tf == "15m":
                self.htf_cache_hits += 1
                bars = self._bars_15m
            elif tf in self._htf_cache:
                self.htf_cache_hits += 1
                bars = self._htf_cache[tf]
            else:
                factor = {"1h": 4, "4h": 16, "1d": 96}.get(tf)
                if factor is None:
                    raise ValueError(f"UNSUPPORTED_TIMEFRAME:{timeframe}")
                bars = aggregate_closed_htf(self._bars_15m, factor)
                self._htf_cache[tf] = bars
                self.htf_build_count += 1
            if up_to_close_ms is None or not bars or bars[-1]["close_ms"] <= up_to_close_ms:
                return bars
            return [b for b in bars if b["close_ms"] <= up_to_close_ms]

    def get_or_build_context(
        self,
        key: str,
        builder: Callable[[], Any],
        *,
        watermark_ms: Optional[int] = None,
    ) -> Any:
        """Share a derived multi-timeframe / indicator context across strategies on the same close."""
        wm = watermark_ms if watermark_ms is not None else self._latest_15m_close_ms
        with self._lock:
            cached = self._context_cache.get(key)
            if cached is not None and cached[0] == wm:
                self.context_cache_hits += 1
                return cached[1]
        ctx = builder()
        with self._lock:
            self._context_cache[key] = (wm, ctx)
            self.context_build_count += 1
        return ctx

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            total_htf = self.htf_build_count + self.htf_cache_hits
            total_ctx = self.context_build_count + self.context_cache_hits
            return {
                "bars_15m_count": len(self._bars_15m),
                "latest_15m_close_ms": self._latest_15m_close_ms,
                "cached_htfs": sorted(self._htf_cache.keys()),
                "cached_contexts": sorted(self._context_cache.keys()),
                "htf_build_count": self.htf_build_count,
                "htf_cache_hits": self.htf_cache_hits,
                "htf_hit_rate": round(self.htf_cache_hits / total_htf, 4) if total_htf else 0.0,
                "context_build_count": self.context_build_count,
                "context_cache_hits": self.context_cache_hits,
                "context_hit_rate": round(self.context_cache_hits / total_ctx, 4) if total_ctx else 0.0,
                "incremental_appends": self.incremental_appends,
                "full_rebuilds": self.full_rebuilds,
            }


class AccountLockRegistry:
    """Per-strategy reentrant lock registry ensuring strict in-account ordering & atomicity."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: Dict[str, threading.RLock] = {}

    def lock_for(self, strategy_id: str) -> threading.RLock:
        key = str(strategy_id)
        with self._guard:
            lk = self._locks.get(key)
            if lk is None:
                lk = threading.RLock()
                self._locks[key] = lk
            return lk

    def remove(self, strategy_id: str) -> bool:
        """Release lock entry when a strategy is dynamically unloaded."""
        key = str(strategy_id)
        with self._guard:
            return self._locks.pop(key, None) is not None

    def count(self) -> int:
        with self._guard:
            return len(self._locks)


class BoundedCycleExecutor:
    """Bounded concurrent fan-out for native-timeframe strategy cycle evaluation with per-account locking."""

    def __init__(
        self,
        locks: AccountLockRegistry,
        *,
        max_workers: int = 6,
    ) -> None:
        self._locks = locks
        self._max_workers = max(1, int(max_workers))
        self._pool = ThreadPoolExecutor(max_workers=self._max_workers, thread_name_prefix="strat-cycle")
        self.last_fanout_ms: float = 0.0
        self.last_strategy_count: int = 0
        self.total_cycles_run: int = 0
        self.total_strategy_errors: int = 0

    def run_batch(self, tasks: List[Tuple[str, Callable[[], Any]]]) -> Dict[str, Any]:
        """Execute (strategy_id, fn) concurrently under per-account locks; isolate individual failures."""
        t0 = time.perf_counter()
        results: Dict[str, Any] = {}
        if not tasks:
            return results

        def _wrapped(sid: str, fn: Callable[[], Any]) -> Tuple[str, Any]:
            with self._locks.lock_for(sid):
                return sid, fn()

        futures = {self._pool.submit(_wrapped, sid, fn): sid for sid, fn in tasks}
        for fut in as_completed(futures):
            sid = futures[fut]
            try:
                _, res = fut.result()
                results[sid] = res
            except Exception as exc:
                self.total_strategy_errors += 1
                logging.getLogger(__name__).exception("BoundedCycleExecutor strategy %s failed", sid)
                results[sid] = {"status": "ERROR", "error": type(exc).__name__}

        self.last_fanout_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        self.last_strategy_count = len(tasks)
        self.total_cycles_run += 1
        return results

    def stats(self) -> Dict[str, Any]:
        return {
            "max_workers": self._max_workers,
            "last_fanout_ms": self.last_fanout_ms,
            "last_strategy_count": self.last_strategy_count,
            "total_cycles_run": self.total_cycles_run,
            "total_strategy_errors": self.total_strategy_errors,
        }


class ReadOnlySnapshotCache:
    """Event-invalidated + TTL in-memory cache isolating read-only query APIs from trading SQLite writes.

    Includes version and timestamp freshness metadata for Desktop Console, Research Center V3,
    Live K-line, and future GrokBot read-only APIs.
    """

    def __init__(self, ttl_seconds: float = 15.0) -> None:
        self._ttl = float(ttl_seconds)
        self._lock = threading.Lock()
        # key -> (mono_ts, utc_iso, version, value)
        self._entries: Dict[str, Tuple[float, str, int, Any]] = {}
        self.version: int = 1
        self.hits: int = 0
        self.misses: int = 0
        self.invalidations: int = 0

    def get_or_build(self, key: str, builder: Callable[[], Any], *, ttl_override: Optional[float] = None) -> Any:
        now = time.monotonic()
        ttl = self._ttl if ttl_override is None else float(ttl_override)
        with self._lock:
            hit = self._entries.get(key)
            if hit is not None and (now - hit[0]) < ttl:
                self.hits += 1
                return hit[3]
            self.misses += 1
            ver = self.version
        val = builder()
        now_done = time.monotonic()
        now_iso = datetime.now(UTC).isoformat()
        with self._lock:
            # Only cache if no invalidation occurred while building (prevents stale write-back)
            if self.version == ver:
                self._entries[key] = (now_done, now_iso, ver, val)
        return val

    def snapshot_meta(self, key: str) -> Dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            hit = self._entries.get(key)
            if hit is None:
                return {"cached": False, "version": self.version, "cached_at": None, "age_ms": None}
            return {
                "cached": True,
                "version": hit[2],
                "cached_at": hit[1],
                "age_ms": int(max(0.0, (now - hit[0]) * 1000.0)),
            }

    def invalidate(self, key: Optional[str] = None) -> None:
        with self._lock:
            self.version += 1
            self.invalidations += 1
            if key is None:
                self._entries.clear()
            else:
                self._entries.pop(key, None)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            total = self.hits + self.misses
            return {
                "ttl_seconds": self._ttl,
                "version": self.version,
                "keys": sorted(self._entries.keys()),
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / total, 4) if total else 0.0,
                "invalidations": self.invalidations,
            }
