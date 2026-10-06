"""
Funding-rate history fetch and cache, used to price funding cost/gain into
backtest and optimizer simulations (see docs/risk_management.md: "Funding
fees slowly drain the balance").

Funding-rate history is far lower volume than 1m OHLCV -- one row per funding
interval (8h on Binance, 1h on Hyperliquid) versus 1440 rows/day for candles
-- so this uses a simple flat per-coin JSON cache rather than the chunked,
checksummed v2 OHLCV store in ohlcv_store.py. Missing history (e.g. a coin
listed after the requested start date) is coverage metadata, not a fatal
error, matching the HLCV data contract described in docs/backtesting.md.
"""

import json
import logging
import os

from utils import coin_to_symbol, load_ccxt_instance, to_standard_exchange_name

CACHE_ROOT = "caches/funding_rates"


def _cache_path(exchange: str, coin: str) -> str:
    ex = to_standard_exchange_name(exchange)
    return os.path.join(CACHE_ROOT, ex, f"{coin}.json")


def _load_cached_rates(exchange: str, coin: str) -> list[dict]:
    path = _cache_path(exchange, coin)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logging.warning(f"funding_rate_manager: failed to read cache {path}: {e}")
        return []
    if not isinstance(data, list):
        return []
    return data


def _save_cached_rates(exchange: str, coin: str, rates: list[dict]) -> None:
    path = _cache_path(exchange, coin)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(rates, f)
    os.replace(tmp_path, path)


def _merge_rates(existing: list[dict], new: list[dict]) -> list[dict]:
    by_ts = {row["timestamp_ms"]: row for row in existing}
    for row in new:
        by_ts[row["timestamp_ms"]] = row
    return [by_ts[ts] for ts in sorted(by_ts)]


async def fetch_funding_rate_history(
    exchange: str, coin: str, start_ts_ms: int, end_ts_ms: int, verbose: bool = True
) -> list[dict]:
    """
    Fetch raw funding-rate history for one coin from the exchange's public API,
    paging forward until the requested range is covered or the exchange stops
    returning new rows. Unauthenticated public market data only.
    """
    symbol = coin_to_symbol(coin, exchange, verbose=verbose)
    if not symbol:
        return []
    cc = load_ccxt_instance(exchange)
    rows: list[dict] = []
    try:
        since = start_ts_ms
        seen_timestamps: set[int] = set()
        while since < end_ts_ms:
            try:
                batch = await cc.fetch_funding_rate_history(symbol, since=since, limit=500)
            except Exception as e:
                logging.warning(
                    "funding_rate_manager: fetch_funding_rate_history failed for "
                    f"{exchange} {coin} ({symbol}) since={since}: {e}"
                )
                break
            if not batch:
                break
            new_rows = 0
            for entry in batch:
                ts = entry.get("timestamp")
                rate = entry.get("fundingRate")
                if ts is None or rate is None or ts in seen_timestamps or ts > end_ts_ms:
                    continue
                seen_timestamps.add(ts)
                rows.append({"timestamp_ms": int(ts), "funding_rate": float(rate)})
                new_rows += 1
            last_ts = batch[-1].get("timestamp")
            if last_ts is None or new_rows == 0:
                break
            since = int(last_ts) + 1
    finally:
        await cc.close()
    rows.sort(key=lambda r: r["timestamp_ms"])
    return rows


def _covers_range(cached: list[dict], start_ts_ms: int, end_ts_ms: int) -> bool:
    if not cached:
        return False
    return cached[0]["timestamp_ms"] <= start_ts_ms and cached[-1]["timestamp_ms"] >= end_ts_ms


async def get_funding_rates(
    exchange: str, coin: str, start_ts_ms: int, end_ts_ms: int, verbose: bool = True
) -> list[dict]:
    """
    Return cached funding-rate history for [start_ts_ms, end_ts_ms], fetching
    and extending the cache when the requested range isn't already covered. A
    coin with no funding history in range (e.g. listed after start_ts_ms)
    returns an empty list -- coverage metadata, not an error, per the HLCV
    data contract in docs/backtesting.md.
    """
    cached = _load_cached_rates(exchange, coin)
    if _covers_range(cached, start_ts_ms, end_ts_ms):
        return [r for r in cached if start_ts_ms <= r["timestamp_ms"] <= end_ts_ms]

    fetched = await fetch_funding_rate_history(exchange, coin, start_ts_ms, end_ts_ms, verbose=verbose)
    merged = _merge_rates(cached, fetched)
    _save_cached_rates(exchange, coin, merged)
    return [r for r in merged if start_ts_ms <= r["timestamp_ms"] <= end_ts_ms]
