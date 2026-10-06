import json
import os

import pytest

import funding_rate_manager as frm


class FakeCcxtClient:
    def __init__(self, batches):
        self._batches = list(batches)
        self._calls = []
        self.closed = False

    async def fetch_funding_rate_history(self, symbol, since=None, limit=None):
        self._calls.append((symbol, since, limit))
        if not self._batches:
            return []
        return self._batches.pop(0)

    async def close(self):
        self.closed = True


def _row(ts_ms, rate):
    return {"timestamp": ts_ms, "fundingRate": rate}


def test_cache_path_uses_standard_exchange_name():
    path = frm._cache_path("binanceusdm", "BTC")
    assert path == os.path.join("caches", "funding_rates", "binance", "BTC.json")


def test_merge_rates_dedups_by_timestamp_and_sorts():
    existing = [
        {"timestamp_ms": 300, "funding_rate": 0.0003},
        {"timestamp_ms": 100, "funding_rate": 0.0001},
    ]
    new = [
        {"timestamp_ms": 200, "funding_rate": 0.0002},
        {"timestamp_ms": 100, "funding_rate": 0.00011},  # overwrites stale row
    ]
    merged = frm._merge_rates(existing, new)
    assert [r["timestamp_ms"] for r in merged] == [100, 200, 300]
    assert merged[0]["funding_rate"] == 0.00011


def test_covers_range():
    cached = [{"timestamp_ms": 100}, {"timestamp_ms": 500}]
    assert frm._covers_range(cached, 100, 500)
    assert frm._covers_range(cached, 200, 400)
    assert not frm._covers_range(cached, 50, 500)
    assert not frm._covers_range(cached, 100, 600)
    assert not frm._covers_range([], 100, 500)


@pytest.mark.asyncio
async def test_fetch_funding_rate_history_pages_forward_until_exhausted(monkeypatch):
    batches = [
        [_row(0, 0.0001), _row(28_800_000, 0.0002)],
        [_row(57_600_000, 0.0003)],
        [],
    ]
    fake = FakeCcxtClient(batches)
    monkeypatch.setattr(frm, "load_ccxt_instance", lambda exchange: fake)
    monkeypatch.setattr(frm, "coin_to_symbol", lambda coin, exchange, verbose=True: "BTC/USDT:USDT")

    rows = await frm.fetch_funding_rate_history("binance", "BTC", 0, 100_000_000)

    assert [r["timestamp_ms"] for r in rows] == [0, 28_800_000, 57_600_000]
    assert fake.closed is True
    assert len(fake._calls) == 3  # two data batches plus the terminating empty page


@pytest.mark.asyncio
async def test_fetch_funding_rate_history_stops_on_duplicate_page(monkeypatch):
    # A misbehaving/exhausted source repeating the same page must not spin forever.
    batches = [[_row(0, 0.0001)], [_row(0, 0.0001)]]
    fake = FakeCcxtClient(batches)
    monkeypatch.setattr(frm, "load_ccxt_instance", lambda exchange: fake)
    monkeypatch.setattr(frm, "coin_to_symbol", lambda coin, exchange, verbose=True: "BTC/USDT:USDT")

    rows = await frm.fetch_funding_rate_history("binance", "BTC", 0, 100_000_000)

    assert [r["timestamp_ms"] for r in rows] == [0]
    assert fake.closed is True


@pytest.mark.asyncio
async def test_fetch_funding_rate_history_missing_coin_returns_empty_without_error(monkeypatch):
    # A coin listed after start_ts_ms (or otherwise unmapped) is coverage
    # metadata, not a fatal error -- matches the HLCV data contract.
    monkeypatch.setattr(frm, "coin_to_symbol", lambda coin, exchange, verbose=True: "")

    def fail_if_called(exchange):
        raise AssertionError("should not construct a client for an unmapped coin")

    monkeypatch.setattr(frm, "load_ccxt_instance", fail_if_called)

    rows = await frm.fetch_funding_rate_history("binance", "NOPE", 0, 100_000_000)

    assert rows == []


@pytest.mark.asyncio
async def test_get_funding_rates_uses_cache_without_network_when_fully_covered(tmp_path, monkeypatch):
    monkeypatch.setattr(frm, "CACHE_ROOT", str(tmp_path))
    cached = [
        {"timestamp_ms": 0, "funding_rate": 0.0001},
        {"timestamp_ms": 100_000_000, "funding_rate": 0.0002},
    ]
    frm._save_cached_rates("binance", "BTC", cached)

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("should not hit the network when cache already covers the range")

    monkeypatch.setattr(frm, "fetch_funding_rate_history", fail_if_called)

    rows = await frm.get_funding_rates("binance", "BTC", 0, 100_000_000)

    assert rows == cached


@pytest.mark.asyncio
async def test_get_funding_rates_fetches_and_persists_when_cache_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(frm, "CACHE_ROOT", str(tmp_path))

    async def fake_fetch(exchange, coin, start_ts_ms, end_ts_ms, verbose=True):
        return [{"timestamp_ms": 0, "funding_rate": 0.0001}]

    monkeypatch.setattr(frm, "fetch_funding_rate_history", fake_fetch)

    rows = await frm.get_funding_rates("binance", "BTC", 0, 100_000_000)

    assert rows == [{"timestamp_ms": 0, "funding_rate": 0.0001}]
    cache_path = frm._cache_path("binance", "BTC")
    assert os.path.exists(cache_path)
    with open(cache_path) as f:
        assert json.load(f) == rows


def test_load_cached_rates_recovers_from_corrupt_json(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(frm, "CACHE_ROOT", str(tmp_path))
    path = frm._cache_path("binance", "BTC")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("{not valid json")

    rows = frm._load_cached_rates("binance", "BTC")

    assert rows == []
