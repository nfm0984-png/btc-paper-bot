#!/usr/bin/env python3
"""Kalshi 15-minute BTC paper-trading & signal bot (KXBTC15M).

Public market data only. Simulated money. No API keys.
"""

from __future__ import annotations

import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
STATE_PATH = DATA_DIR / "state.json"
BACKTEST_PATH = DATA_DIR / "backtest.json"
DASHBOARD_PATH = DOCS_DIR / "data.json"

SERIES = "KXBTC15M"
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_MARKET_URL = "https://kalshi.com/markets/kxbtc15m/{ticker}"

STARTING_BALANCE = 100.00
# Variable base stake: $4 normal, $6 strong, $8 top-conviction.
# Every ticket’s total cost (contracts × price + taker fee) stays in [$4, $20].
# A 2× press after a win is hard-capped at $20 ($4→$8, $6→$12, $8→$16, then $20 max).
STAKE_NORMAL = 4.00
STAKE_STRONG = 6.00
STAKE_TOP = 8.00
STAKE_MIN = 4.00
STAKE_PRESS_CAP = 20.00
STAKE = STAKE_NORMAL  # default / display alias
SESSION_STARTING = 40.00
SESSION_STAKE = STAKE_NORMAL
SESSION_MINUTES = 60
# Head-to-head $100 race: Version 1 (multi-trade) vs Version 2 (strict / learned rules).
RACE_STARTING = 100.00
RACE_BOOK_KEYS = ("v1", "v2", "every15m", "selective")
V1_ASK_MIN = 0.05
V1_ASK_MAX = 0.95
V1_MIN_REMAINING_SEC = 120
V1_MAX_TRADES_PER_WINDOW = 3
# Trades opened under the old flat-$5 rule have no `stake` field.
LEGACY_FLAT_STAKE = 5.00
FEE_RATE = 0.07  # Kalshi quadratic taker fee
FEE_MULTIPLIER = 1.0

FAST_EMA = 9
SLOW_EMA = 21
TREND_EMA = 50
RSI_PERIOD = 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9

# v1 defaults — also stored in data/strategy_config.json and overridable by the hourly learner.
ASK_MIN = 0.35
ASK_MAX = 0.72
MIN_REMAINING_SEC = 120
MIN_STRIKE_DISTANCE = 20.0  # dollars
EMA_FLAT_DOLLARS = 8.0
RSI_YES_LO, RSI_YES_HI = 52.0, 75.0
RSI_NO_LO, RSI_NO_HI = 25.0, 48.0
STRATEGY_CONFIG_PATH = DATA_DIR / "strategy_config.json"
HISTORY_WINDOWS_PATH = DATA_DIR / "history_windows.json"
LEARNING_LOG_PATH = DATA_DIR / "learning_log.json"
HISTORY_DAYS = 14
PROMOTION_COOLDOWN_SEC = 3 * 3600
MIN_PROMOTION_TRADES = 20
MIN_PROMOTION_VAL_TRADES = 6
TAKE_PROFIT_BID = 0.80
TAKE_PROFIT_GAIN = 0.15
STOP_LOSS_DROP = 0.12
EARLY_EXIT_CUTOFF_SEC = 30
DECISION_OFFSET_SEC = 60  # backtest snapshot: 1 minute after the window opens
BACKTEST_DAYS = 5
BACKTEST_CANDLE_PAD_HOURS = 6

USER_AGENT = "btc-paper-bot/1.1 (+https://github.com/nfm0984-png/btc-paper-bot)"
HTTP_TIMEOUT = 20
MAX_RETRIES = 3


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def parse_iso(ts: str | int | float | datetime) -> datetime:
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=UTC)
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(int(ts), tz=UTC)
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(UTC)


def to_ts(ts: str | int | float | datetime) -> int:
    return int(parse_iso(ts).timestamp())


def fmt_pt(ts: str | int | float | datetime) -> str:
    dt = parse_iso(ts).astimezone(PT)
    hour = dt.strftime("%I").lstrip("0") or "0"
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}, {hour}:{dt.strftime('%M %p')} PT"


def fmt_pt_short(ts: str | int | float | datetime) -> str:
    dt = parse_iso(ts).astimezone(PT)
    hour = dt.strftime("%I").lstrip("0") or "0"
    return f"{hour}:{dt.strftime('%M %p')}"


def session_window_label(start: str | datetime, end: str | datetime) -> str:
    return f"{fmt_pt_short(start)} – {fmt_pt_short(end)} PT"


def iso_utc(ts: str | int | float | datetime) -> str:
    return parse_iso(ts).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def round_money(x: float) -> float:
    return round(float(x) + 0.0, 2)


def round_px(x: float, n: int = 2) -> float:
    return round(float(x) + 0.0, n)


def round_cent(x: float) -> float:
    return round(float(x) + 0.0, 4)


def dollars(x: Any) -> Optional[float]:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def cents_label(price: Optional[float]) -> str:
    if price is None:
        return "—"
    return f"{round(price * 100):.0f}¢"


def http_get_json(url: str) -> Any:
    last_err: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            last_err = exc
            time.sleep(0.35 * attempt)
    raise RuntimeError(f"GET failed after {MAX_RETRIES} tries: {url} ({last_err})")


# ---------------------------------------------------------------------------
# Kalshi fee + market helpers
# ---------------------------------------------------------------------------

def kalshi_taker_fee(contracts: int, price: float, multiplier: float = FEE_MULTIPLIER) -> float:
    """Official-style quadratic taker fee, rounded up to the next cent.

    fee = ceil(0.07 * multiplier * C * P * (1-P) * 100) / 100
    """
    if contracts <= 0 or price <= 0 or price >= 1:
        return 0.0
    raw = FEE_RATE * multiplier * contracts * price * (1.0 - price)
    return math.ceil(raw * 100.0 - 1e-12) / 100.0


def contract_url(ticker: str) -> str:
    return KALSHI_MARKET_URL.format(ticker=ticker.lower())


# ---------------------------------------------------------------------------
# Market data: Kalshi + Coinbase/Kraken
# ---------------------------------------------------------------------------

def fetch_kalshi_markets(status: str, limit: int = 200, max_pages: int = 20) -> list[dict]:
    out: list[dict] = []
    cursor = None
    pages = 0
    while pages < max_pages:
        pages += 1
        params = {"series_ticker": SERIES, "status": status, "limit": str(limit)}
        if cursor:
            params["cursor"] = cursor
        url = f"{KALSHI_API}/markets?{urllib.parse.urlencode(params)}"
        data = http_get_json(url)
        batch = data.get("markets") or []
        out.extend(batch)
        cursor = data.get("cursor") or ""
        if not cursor or not batch:
            break
        time.sleep(0.12)
    return out


def fetch_kalshi_event(event_ticker: str) -> dict:
    return http_get_json(f"{KALSHI_API}/events/{urllib.parse.quote(event_ticker)}")


def fetch_candlesticks_for_markets(markets: list[dict], first_minutes: int = 3) -> dict[str, list[dict]]:
    """Map market ticker -> 1-minute yes bid/ask candlesticks near the open."""
    by_ticker: dict[str, list[dict]] = {}
    chunk = 15
    for i in range(0, len(markets), chunk):
        group = [m for m in markets[i : i + chunk] if m.get("open_time") and m.get("ticker")]
        if not group:
            continue
        starts = [to_ts(m["open_time"]) for m in group]
        ends = [to_ts(m.get("close_time") or m["open_time"]) for m in group]
        start = min(starts)
        # Keep the window tight around each group's opens so the payload stays small,
        # but always cover each market's first few minutes.
        end = max(s + first_minutes * 60 + 5 for s in starts)
        end = max(end, min(ends))
        tickers = ",".join(m["ticker"] for m in group)
        url = (
            f"{KALSHI_API}/markets/candlesticks?"
            + urllib.parse.urlencode(
                {
                    "market_tickers": tickers,
                    "start_ts": start,
                    "end_ts": end,
                    "period_interval": 1,
                }
            )
        )
        try:
            data = http_get_json(url)
        except Exception as exc:
            print(f"  candlestick batch failed ({exc}); retrying one-by-one", flush=True)
            for m in group:
                try:
                    s = to_ts(m["open_time"])
                    one = http_get_json(
                        f"{KALSHI_API}/markets/candlesticks?"
                        + urllib.parse.urlencode(
                            {
                                "market_tickers": m["ticker"],
                                "start_ts": s,
                                "end_ts": s + first_minutes * 60 + 5,
                                "period_interval": 1,
                            }
                        )
                    )
                    for row in one.get("markets") or []:
                        by_ticker[row.get("ticker") or row.get("market_ticker") or m["ticker"]] = (
                            row.get("candlesticks") or []
                        )
                except Exception as exc2:
                    print(f"  candlesticks missing for {m.get('ticker')}: {exc2}", flush=True)
                time.sleep(0.08)
            continue
        for row in data.get("markets") or []:
            t = row.get("ticker") or row.get("market_ticker")
            if t:
                by_ticker[t] = row.get("candlesticks") or []
        time.sleep(0.12)
    return by_ticker


def quote_at_decision(candles: list[dict], open_ts: int, offset: int = DECISION_OFFSET_SEC) -> Optional[dict]:
    if not candles:
        return None
    target = open_ts + offset
    # Prefer the 1-minute bar that closes at the decision time.
    chosen = None
    for c in candles:
        end = int(c.get("end_period_ts") or 0)
        if end <= target:
            chosen = c
        elif chosen is None and end > open_ts:
            chosen = c
            break
        else:
            break
    if chosen is None:
        chosen = candles[0]
    yes_ask = dollars((chosen.get("yes_ask") or {}).get("close_dollars"))
    yes_bid = dollars((chosen.get("yes_bid") or {}).get("close_dollars"))
    last = dollars((chosen.get("price") or {}).get("close_dollars"))
    if yes_ask is None or yes_bid is None:
        return None
    # Ignore empty-book prints of 0 or 1 at the very open.
    if yes_ask >= 0.99 or yes_ask <= 0.01:
        return None
    return {
        "end_ts": int(chosen.get("end_period_ts") or target),
        "yes_ask": yes_ask,
        "yes_bid": yes_bid,
        "no_bid": max(0.0, min(1.0, 1.0 - yes_ask)),
        "no_ask": max(0.0, min(1.0, 1.0 - yes_bid)),
        "last": last,
    }


def fetch_coinbase_ticker() -> float:
    data = http_get_json("https://api.exchange.coinbase.com/products/BTC-USD/ticker")
    return float(data["price"])


def fetch_kraken_ticker() -> float:
    data = http_get_json("https://api.kraken.com/0/public/Ticker?pair=XBTUSD")
    if data.get("error"):
        raise RuntimeError(f"Kraken ticker error: {data['error']}")
    result = data["result"]
    key = next(k for k in result)
    return float(result[key]["c"][0])


def fetch_live_price() -> tuple[float, str]:
    errors = []
    try:
        return fetch_coinbase_ticker(), "coinbase"
    except Exception as exc:
        errors.append(f"coinbase: {exc}")
    try:
        return fetch_kraken_ticker(), "kraken"
    except Exception as exc:
        errors.append(f"kraken: {exc}")
    raise RuntimeError("Could not fetch a live BTC/USD price. " + " | ".join(errors))


def _norm_cb(row: list) -> dict:
    # Coinbase: [time, low, high, open, close, volume]
    return {
        "time": int(row[0]),
        "open": float(row[3]),
        "high": float(row[2]),
        "low": float(row[1]),
        "close": float(row[4]),
        "volume": float(row[5]),
    }


def fetch_coinbase_candles(start_ts: int, end_ts: int, granularity: int) -> list[dict]:
    candles: dict[int, dict] = {}
    max_span = granularity * 300
    cursor = int(start_ts)
    end_ts = int(end_ts)
    while cursor < end_ts:
        chunk_end = min(cursor + max_span, end_ts)
        start_iso = datetime.fromtimestamp(cursor, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = datetime.fromtimestamp(chunk_end, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        url = (
            "https://api.exchange.coinbase.com/products/BTC-USD/candles"
            f"?granularity={granularity}&start={start_iso}&end={end_iso}"
        )
        raw = http_get_json(url)
        if not isinstance(raw, list):
            raise RuntimeError(f"Unexpected Coinbase candles payload: {raw}")
        for row in raw:
            candles[int(row[0])] = _norm_cb(row)
        cursor = chunk_end
        time.sleep(0.12)
    return [candles[k] for k in sorted(candles)]


def fetch_kraken_ohlc(interval_min: int, start_ts: int) -> list[dict]:
    url = f"https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval={interval_min}&since={int(start_ts)}"
    data = http_get_json(url)
    if data.get("error"):
        raise RuntimeError(f"Kraken OHLC error: {data['error']}")
    result = data["result"]
    key = next(k for k in result if k != "last")
    out = []
    for row in result[key]:
        ts = int(row[0])
        if ts < start_ts:
            continue
        out.append(
            {
                "time": ts,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[6]),
            }
        )
    out.sort(key=lambda c: c["time"])
    return out


def fetch_btc_candles(start_ts: int, end_ts: int, granularity: int) -> tuple[list[dict], str]:
    errors = []
    try:
        candles = fetch_coinbase_candles(start_ts, end_ts, granularity)
        if len(candles) >= 80:
            return candles, "coinbase"
        errors.append(f"coinbase returned only {len(candles)} candles")
    except Exception as exc:
        errors.append(f"coinbase: {exc}")
    try:
        interval = max(1, granularity // 60)
        candles = fetch_kraken_ohlc(interval, start_ts)
        candles = [c for c in candles if c["time"] <= end_ts]
        if len(candles) >= 80:
            return candles, "kraken"
        errors.append(f"kraken returned only {len(candles)} candles")
    except Exception as exc:
        errors.append(f"kraken: {exc}")
    raise RuntimeError(f"Could not fetch {granularity}s BTC/USD candles. " + " | ".join(errors))


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------

def series_ema(values: list[Optional[float]], period: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    k = 2.0 / (period + 1)
    seed_vals: list[float] = []
    seed = None
    for i, v in enumerate(values):
        if v is None:
            continue
        if seed is None:
            seed_vals.append(float(v))
            if len(seed_vals) == period:
                seed = sum(seed_vals) / period
                out[i] = seed
            continue
        seed = (float(v) - seed) * k + seed
        out[i] = seed
    return out


def series_rsi(closes: list[float], period: int = 14) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        ch = closes[i] - closes[i - 1]
        if ch >= 0:
            gains += ch
        else:
            losses -= ch
    avg_gain = gains / period
    avg_loss = losses / period

    def rsi_val(ag: float, al: float) -> float:
        if al == 0:
            return 100.0
        if ag == 0:
            return 0.0
        return 100.0 - (100.0 / (1.0 + ag / al))

    out[period] = rsi_val(avg_gain, avg_loss)
    for i in range(period + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        g = ch if ch > 0 else 0.0
        l = -ch if ch < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        out[i] = rsi_val(avg_gain, avg_loss)
    return out


@dataclass
class Indicators:
    ema9: Optional[float]
    ema21: Optional[float]
    ema50: Optional[float]
    rsi14: Optional[float]
    macd_hist: Optional[float]
    close: Optional[float]
    ready: bool = False


def compute_indicators(candles: list[dict]) -> list[Indicators]:
    closes = [c["close"] for c in candles]
    opts: list[Optional[float]] = list(closes)
    ema9 = series_ema(opts, FAST_EMA)
    ema21 = series_ema(opts, SLOW_EMA)
    ema50 = series_ema(opts, TREND_EMA)
    rsi14 = series_rsi(closes, RSI_PERIOD)
    macd_line = [
        (a - b) if a is not None and b is not None else None for a, b in zip(series_ema(opts, MACD_FAST), series_ema(opts, MACD_SLOW))
    ]
    macd_signal = series_ema(macd_line, MACD_SIGNAL)
    macd_hist = [
        (m - s) if m is not None and s is not None else None for m, s in zip(macd_line, macd_signal)
    ]
    out = []
    for i, c in enumerate(candles):
        ind = Indicators(
            ema9=ema9[i],
            ema21=ema21[i],
            ema50=ema50[i],
            rsi14=rsi14[i],
            macd_hist=macd_hist[i],
            close=c["close"],
        )
        ind.ready = None not in (ind.ema9, ind.ema21, ind.ema50, ind.rsi14, ind.macd_hist)
        out.append(ind)
    return out


def indicators_at(candles: list[dict], series: list[Indicators], ts: int) -> Optional[tuple[dict, Indicators]]:
    """Last fully closed candle at or before ts, plus its indicators."""
    chosen = None
    for i, c in enumerate(candles):
        if int(c["time"]) <= ts:
            chosen = (c, series[i])
        else:
            break
    return chosen


def ind_payload(ind: Optional[Indicators]) -> dict:
    if not ind:
        return {"ema9": None, "ema21": None, "ema50": None, "rsi14": None, "macd_hist": None, "close": None, "ready": False}
    return {
        "ema9": None if ind.ema9 is None else round_px(ind.ema9, 2),
        "ema21": None if ind.ema21 is None else round_px(ind.ema21, 2),
        "ema50": None if ind.ema50 is None else round_px(ind.ema50, 2),
        "rsi14": None if ind.rsi14 is None else round_px(ind.rsi14, 2),
        "macd_hist": None if ind.macd_hist is None else round_px(ind.macd_hist, 2),
        "close": None if ind.close is None else round_px(ind.close, 2),
        "ready": ind.ready,
    }


def indicators_from_payload(d: Optional[dict]) -> Optional[Indicators]:
    if not d:
        return None
    ind = Indicators(
        ema9=d.get("ema9"),
        ema21=d.get("ema21"),
        ema50=d.get("ema50"),
        rsi14=d.get("rsi14"),
        macd_hist=d.get("macd_hist"),
        close=d.get("close"),
        ready=bool(d.get("ready")),
    )
    if not ind.ready:
        ind.ready = None not in (ind.ema9, ind.ema21, ind.ema50, ind.rsi14, ind.macd_hist)
    return ind


# ---------------------------------------------------------------------------
# Tunable strategy config (hourly learner writes this; v1 defaults if missing)
# ---------------------------------------------------------------------------

@dataclass
class StrategyParams:
    min_entry_cents: int = 35
    max_entry_cents: int = 72
    min_strike_gap_usd: float = 20.0
    min_mins_left: int = 2
    max_mins_left: int = 15
    rsi_bull_min: float = 52.0
    rsi_bull_max: float = 75.0
    rsi_bear_min: float = 25.0
    rsi_bear_max: float = 48.0
    require_ema50_align: bool = True
    require_5m_align: bool = True
    min_volatility_usd: float = 0.0
    max_chop_ratio: float = 1.0
    ema_flat_dollars: float = 8.0

    @property
    def min_entry(self) -> float:
        return float(self.min_entry_cents) / 100.0

    @property
    def max_entry(self) -> float:
        return float(self.max_entry_cents) / 100.0

    @property
    def min_remaining_sec(self) -> int:
        return int(self.min_mins_left) * 60

    @property
    def max_remaining_sec(self) -> int:
        return int(self.max_mins_left) * 60

    def as_dict(self) -> dict:
        return {
            "min_entry_cents": int(self.min_entry_cents),
            "max_entry_cents": int(self.max_entry_cents),
            "min_strike_gap_usd": float(self.min_strike_gap_usd),
            "min_mins_left": int(self.min_mins_left),
            "max_mins_left": int(self.max_mins_left),
            "rsi_bull_min": float(self.rsi_bull_min),
            "rsi_bull_max": float(self.rsi_bull_max),
            "rsi_bear_min": float(self.rsi_bear_min),
            "rsi_bear_max": float(self.rsi_bear_max),
            "require_ema50_align": bool(self.require_ema50_align),
            "require_5m_align": bool(self.require_5m_align),
            "min_volatility_usd": float(self.min_volatility_usd),
            "max_chop_ratio": float(self.max_chop_ratio),
            "ema_flat_dollars": float(self.ema_flat_dollars),
        }


def params_from_dict(d: Optional[dict]) -> StrategyParams:
    base = StrategyParams()
    if not d:
        return base
    fields = base.as_dict()
    kw = {}
    for k in fields:
        if k in d and d[k] is not None:
            kw[k] = type(fields[k])(d[k])
    return StrategyParams(**{**fields, **kw})


def default_strategy_config(now: Optional[datetime] = None) -> dict:
    now = now or utc_now()
    params = StrategyParams()
    return {
        "version": 1,
        "updated_at": iso_utc(now),
        "updated_at_pt": fmt_pt(now),
        "source": "defaults",
        "params": params.as_dict(),
        "metrics": {},
        "plain_english": plain_english_params(params),
        "last_promoted_at": None,
        "last_promoted_at_pt": None,
    }


def plain_english_params(p: StrategyParams) -> str:
    bits = [
        f"Buys Yes/No when the 1-minute trend is clean, BTC is at least ${p.min_strike_gap_usd:.0f} from the strike, the ask is {p.min_entry_cents}–{p.max_entry_cents}¢, and {p.min_mins_left}–{p.max_mins_left} minutes remain in the window.",
        ("Requires the 50-period EMA to agree." if p.require_ema50_align else "Does not require the 50-period EMA to agree."),
        ("Requires the 5-minute trend to agree." if p.require_5m_align else "Does not require the 5-minute trend to agree."),
    ]
    if p.min_volatility_usd > 0 or p.max_chop_ratio < 0.999:
        chop = []
        if p.min_volatility_usd > 0:
            chop.append(f"skips windows that moved less than ${p.min_volatility_usd:.0f} over 15 minutes")
        if p.max_chop_ratio < 0.999:
            chop.append(f"skips choppy tapes (chop ratio above {p.max_chop_ratio:.2f})")
        bits.append("Also " + " and ".join(chop) + ".")
    else:
        bits.append("No extra chop filter.")
    return " ".join(bits)


def load_strategy_config() -> dict:
    if not STRATEGY_CONFIG_PATH.exists():
        return default_strategy_config()
    with STRATEGY_CONFIG_PATH.open() as f:
        raw = json.load(f)
    cfg = default_strategy_config()
    if isinstance(raw, dict):
        cfg.update({k: raw[k] for k in raw if k != "params"})
        cfg["params"] = params_from_dict(raw.get("params") or raw).as_dict()
        if not cfg.get("plain_english"):
            cfg["plain_english"] = plain_english_params(params_from_dict(cfg["params"]))
        cfg.setdefault("version", 1)
        cfg.setdefault("source", "defaults")
        cfg.setdefault("metrics", {})
    return cfg


def active_params(cfg: Optional[dict] = None) -> StrategyParams:
    if cfg is None:
        try:
            cfg = load_strategy_config()
        except Exception:
            return StrategyParams()
    return params_from_dict((cfg or {}).get("params"))


def window_chop_features(candles: list[dict], ts: int, lookback: int = 15) -> tuple[Optional[float], Optional[float]]:
    """15-minute range (volatility) and a 0–1 chop ratio (1 = went nowhere)."""
    bars = [c for c in candles if int(c["time"]) <= int(ts)]
    bars = bars[-lookback:]
    if len(bars) < 8:
        return None, None
    hi = max(float(c["high"]) for c in bars)
    lo = min(float(c["low"]) for c in bars)
    vol = hi - lo
    move = abs(float(bars[-1]["close"]) - float(bars[0]["open"]))
    chop = 1.0 if vol <= 1e-9 else max(0.0, min(1.0, 1.0 - move / vol))
    return round(vol, 2), round(chop, 3)


def contract_window_label(close_ts: str | int | float | datetime) -> str:
    close = parse_iso(close_ts)
    start = close - timedelta(minutes=15)
    return session_window_label(start, close)


# ---------------------------------------------------------------------------
# Signal engine
# ---------------------------------------------------------------------------

@dataclass
class Signal:
    action: str  # BUY_YES, BUY_NO, WAIT, TAKE_PROFIT, CUT_LOSS
    label: str
    reason: str
    side: Optional[str] = None  # yes / no
    ask: Optional[float] = None
    bid: Optional[float] = None
    strength: Optional[str] = None  # normal / strong / top
    base_stake: Optional[float] = None


def _bullish(ind: Indicators, trend: Optional[Indicators], p: StrategyParams) -> tuple[bool, list[str], list[str]]:
    ok, yes, no = True, [], []
    if not ind.ready:
        return False, [], ["indicators still warming up"]
    if ind.ema9 > ind.ema21:
        yes.append(f"EMA 9 > 21 (${ind.ema9:,.0f} > ${ind.ema21:,.0f})")
    else:
        ok = False
        no.append("EMA 9 is below EMA 21")
    if abs((ind.ema9 or 0) - (ind.ema21 or 0)) < p.ema_flat_dollars:
        ok = False
        no.append("EMAs are flat — looks choppy")
    if p.require_ema50_align:
        if ind.close > ind.ema50:
            yes.append("price above the 50-period EMA")
        else:
            ok = False
            no.append("price is below the 50-period EMA")
    if ind.macd_hist > 0:
        yes.append("MACD histogram is positive")
    else:
        ok = False
        no.append("MACD histogram is negative")
    if p.rsi_bull_min <= ind.rsi14 <= p.rsi_bull_max:
        yes.append(f"RSI {ind.rsi14:.0f}")
    else:
        ok = False
        if ind.rsi14 > p.rsi_bull_max:
            no.append(f"RSI {ind.rsi14:.0f} looks overheated")
        else:
            no.append(f"RSI {ind.rsi14:.0f} is short of the {p.rsi_bull_min:.0f} momentum mark")
    if p.require_5m_align and trend and trend.ready:
        if trend.ema9 >= trend.ema21:
            yes.append("5-minute trend agrees")
        else:
            ok = False
            no.append("5-minute trend is still down")
    return ok, yes, no


def _bearish(ind: Indicators, trend: Optional[Indicators], p: StrategyParams) -> tuple[bool, list[str], list[str]]:
    ok, yes, no = True, [], []
    if not ind.ready:
        return False, [], ["indicators still warming up"]
    if ind.ema9 < ind.ema21:
        yes.append(f"EMA 9 < 21 (${ind.ema9:,.0f} < ${ind.ema21:,.0f})")
    else:
        ok = False
        no.append("EMA 9 is still above EMA 21")
    if abs((ind.ema9 or 0) - (ind.ema21 or 0)) < p.ema_flat_dollars:
        ok = False
        no.append("EMAs are flat — looks choppy")
    if p.require_ema50_align:
        if ind.close < ind.ema50:
            yes.append("price below the 50-period EMA")
        else:
            ok = False
            no.append("price is still above the 50-period EMA")
    if ind.macd_hist < 0:
        yes.append("MACD histogram is negative")
    else:
        ok = False
        no.append("MACD histogram is still positive")
    if p.rsi_bear_min <= ind.rsi14 <= p.rsi_bear_max:
        yes.append(f"RSI {ind.rsi14:.0f}")
    else:
        ok = False
        if ind.rsi14 < p.rsi_bear_min:
            no.append(f"RSI {ind.rsi14:.0f} is oversold / stretched")
        else:
            no.append(f"RSI {ind.rsi14:.0f} is not in the bearish {p.rsi_bear_min:.0f}–{p.rsi_bear_max:.0f} band")
    if p.require_5m_align and trend and trend.ready:
        if trend.ema9 <= trend.ema21:
            yes.append("5-minute trend agrees")
        else:
            ok = False
            no.append("5-minute trend is still up")
    return ok, yes, no


def grade_signal(
    side: Optional[str],
    ask: Optional[float],
    ind: Optional[Indicators],
    trend: Optional[Indicators],
    btc_price: float,
    strike: Optional[float],
) -> tuple[str, float, list[str]]:
    """Score a qualifying buy and map it to a $4 / $6 / $8 base stake.

    Every buy already passed the hard filters. Extra quality points come from
    a wider EMA 9/21 spread, a larger BTC-vs-strike cushion, a stronger MACD
    histogram, RSI in the heart of its band, a better-value ask, and a clear
    5-minute trend. 0–1 points = normal $2, 2 = strong $3, 3+ = top $4.
    """
    points = 0
    notes: list[str] = []
    if ind and ind.ready:
        ema_spread = abs((ind.ema9 or 0) - (ind.ema21 or 0))
        if ema_spread >= 25:
            points += 1
            notes.append(f"EMA 9/21 spread ${ema_spread:,.0f}")
        macd = abs(ind.macd_hist or 0)
        if macd >= 8:
            points += 1
            notes.append(f"MACD hist {macd:,.1f}")
        rsi = ind.rsi14
        if rsi is not None:
            if side == "yes" and 56 <= rsi <= 70:
                points += 1
                notes.append(f"RSI {rsi:.0f} in the heart of the bullish band")
            elif side == "no" and 30 <= rsi <= 44:
                points += 1
                notes.append(f"RSI {rsi:.0f} in the heart of the bearish band")
    if strike is not None:
        dist = abs(float(btc_price) - float(strike))
        if dist >= 50:
            points += 1
            notes.append(f"BTC ${dist:,.0f} from the strike")
    if ask is not None and 0.35 <= float(ask) <= 0.58:
        points += 1
        notes.append(f"ask {cents_label(ask)} leaves upside")
    if trend and trend.ready:
        t_spread = abs((trend.ema9 or 0) - (trend.ema21 or 0))
        agrees = (side == "yes" and (trend.ema9 or 0) >= (trend.ema21 or 0)) or (
            side == "no" and (trend.ema9 or 0) <= (trend.ema21 or 0)
        )
        if agrees and t_spread >= 20:
            points += 1
            notes.append(f"5-minute trend is clearly aligned (${t_spread:,.0f} EMA spread)")
    if points >= 3:
        return "top", STAKE_TOP, notes
    if points >= 2:
        return "strong", STAKE_STRONG, notes
    return "normal", STAKE_NORMAL, notes


def apply_strength(
    sig: Signal,
    ind: Optional[Indicators],
    trend: Optional[Indicators],
    btc_price: float,
    strike: Optional[float],
) -> Signal:
    grade, stake, notes = grade_signal(sig.side, sig.ask, ind, trend, btc_price, strike)
    sig.strength = grade
    sig.base_stake = stake
    if notes:
        sig.reason = sig.reason + f" {grade.capitalize()} signal (${stake:.0f} ticket)" + (": " + "; ".join(notes) + "." if notes else ".")
    else:
        sig.reason = sig.reason + f" {grade.capitalize()} signal (${stake:.0f} ticket)."
    return sig


def make_signal(
    *,
    ind_1m: Optional[Indicators],
    ind_5m: Optional[Indicators],
    btc_price: float,
    strike: Optional[float],
    yes_ask: Optional[float],
    yes_bid: Optional[float],
    no_ask: Optional[float],
    no_bid: Optional[float],
    remaining_sec: Optional[int],
    position: Optional[dict] = None,
    params: Optional[StrategyParams] = None,
    features: Optional[dict] = None,
) -> Signal:
    p = params or StrategyParams()
    feats = features or {}
    if remaining_sec is not None and remaining_sec <= 0:
        return Signal("WAIT", "WAIT — Window closed", "This 15-minute window has closed. Waiting for the next contract.")

    # Manage an open paper position first.
    if position:
        side = position["side"]
        entry = float(position["entry_price"])
        bid = yes_bid if side == "yes" else no_bid
        exit_sig = _exit_signal(position, bid, ind_1m, btc_price, strike, remaining_sec)
        if exit_sig:
            return exit_sig
        held = f"Holding {side.upper()} from {cents_label(entry)}"
        if bid is not None:
            held += f"; live bid {cents_label(bid)}"
        held += ". No exit trigger yet — still watching until settlement or a flip."
        return Signal("HOLD", f"HOLD {side.upper()}", held, side=side, ask=None, bid=bid)

    if remaining_sec is not None and remaining_sec < p.min_remaining_sec:
        mm, ss = divmod(max(0, remaining_sec), 60)
        return Signal(
            "WAIT",
            "WAIT — Too close to expiry",
            f"Only {mm}:{ss:02d} left in this window. The bot stands aside under {p.min_mins_left} minutes.",
        )
    if remaining_sec is not None and remaining_sec > p.max_remaining_sec:
        return Signal(
            "WAIT",
            "WAIT — Too early",
            f"This window still has more than {p.max_mins_left} minutes left, so the bot waits for the tape to develop.",
        )
    if not ind_1m or not ind_1m.ready:
        return Signal("WAIT", "WAIT — No Trade", "1-minute indicators are still warming up.")
    if strike is None:
        return Signal("WAIT", "WAIT — No Trade", "The contract strike is not posted yet.")

    vol = feats.get("volatility_usd")
    chop = feats.get("chop_ratio")
    if vol is not None and p.min_volatility_usd > 0 and float(vol) < p.min_volatility_usd:
        return Signal(
            "WAIT",
            "WAIT — Too quiet",
            f"The last 15 minutes only moved ${float(vol):,.0f}, below the ${p.min_volatility_usd:.0f} volatility floor — a coin-flip window.",
        )
    if chop is not None and p.max_chop_ratio < 0.999 and float(chop) > p.max_chop_ratio:
        return Signal(
            "WAIT",
            "WAIT — Too choppy",
            f"The last 15 minutes were choppy (chop {float(chop):.2f} vs a {p.max_chop_ratio:.2f} cap) — the bot stands aside.",
        )

    dist = btc_price - float(strike)
    dist_txt = f"BTC {('+' if dist >= 0 else '')}${dist:,.2f} vs the ${float(strike):,.2f} strike"

    if abs(dist) < p.min_strike_gap_usd:
        return Signal(
            "WAIT",
            "WAIT — Too close to strike",
            f"{dist_txt}. That’s inside a ${p.min_strike_gap_usd:.0f} band, so settlement could still flip either way.",
        )

    bull_ok, bull_yes, bull_no = _bullish(ind_1m, ind_5m, p)
    bear_ok, bear_yes, bear_no = _bearish(ind_1m, ind_5m, p)
    band = f"{p.min_entry_cents}¢–{p.max_entry_cents}¢"

    if bull_ok and dist > 0:
        if yes_ask is None:
            return Signal("WAIT", "WAIT — No Trade", f"{'; '.join(bull_yes)}. {dist_txt}. Yes ask is unavailable.")
        if not (p.min_entry <= yes_ask <= p.max_entry):
            if yes_ask > p.max_entry:
                return Signal(
                    "WAIT",
                    "WAIT — No Edge",
                    f"{'; '.join(bull_yes)}. {dist_txt}, but Yes is already {cents_label(yes_ask)} — too expensive (bot only buys {band}).",
                )
            return Signal(
                "WAIT",
                "WAIT — No Edge",
                f"{'; '.join(bull_yes)}. {dist_txt}, but Yes ask is only {cents_label(yes_ask)} (below the {p.min_entry_cents}¢ floor — something looks off).",
            )
        reason = f"{'; '.join(bull_yes)}. {dist_txt}, Yes ask {cents_label(yes_ask)} — Buy Yes."
        return apply_strength(
            Signal("BUY_YES", f"BUY YES @ {cents_label(yes_ask)}", reason, side="yes", ask=yes_ask, bid=yes_bid),
            ind_1m, ind_5m, btc_price, strike,
        )

    if bear_ok and dist < 0:
        if no_ask is None:
            return Signal("WAIT", "WAIT — No Trade", f"{'; '.join(bear_yes)}. {dist_txt}. No ask is unavailable.")
        if not (p.min_entry <= no_ask <= p.max_entry):
            if no_ask > p.max_entry:
                return Signal(
                    "WAIT",
                    "WAIT — No Edge",
                    f"{'; '.join(bear_yes)}. {dist_txt}, but No is already {cents_label(no_ask)} — too expensive (bot only buys {band}).",
                )
            return Signal(
                "WAIT",
                "WAIT — No Edge",
                f"{'; '.join(bear_yes)}. {dist_txt}, but No ask is only {cents_label(no_ask)} (below the {p.min_entry_cents}¢ floor — something looks off).",
            )
        reason = f"{'; '.join(bear_yes)}. {dist_txt}, No ask {cents_label(no_ask)} — Buy No."
        return apply_strength(
            Signal("BUY_NO", f"BUY NO @ {cents_label(no_ask)}", reason, side="no", ask=no_ask, bid=no_bid),
            ind_1m, ind_5m, btc_price, strike,
        )

    bits = []
    if dist > 0 and not bull_ok:
        bits.append("price is above the strike, but the 1-minute trend/momentum is not cleanly bullish (" + "; ".join(bull_no) + ")")
    elif dist < 0 and not bear_ok:
        bits.append("price is below the strike, but the 1-minute trend/momentum is not cleanly bearish (" + "; ".join(bear_no) + ")")
    elif bull_ok and dist < 0:
        bits.append("EMAs look bullish, but BTC is still below the strike — the contract would settle No if it expired now")
    elif bear_ok and dist > 0:
        bits.append("EMAs look bearish, but BTC is still above the strike — the contract would settle Yes if it expired now")
    else:
        bits.append("trend and momentum are mixed")
    if yes_ask is not None:
        bits.append(f"Yes ask {cents_label(yes_ask)}")
    return Signal("WAIT", "WAIT — No Trade", "Choppy / no clean edge: " + "; ".join(bits) + ".")


def _exit_signal(
    position: dict,
    bid: Optional[float],
    ind: Optional[Indicators],
    btc_price: float,
    strike: Optional[float],
    remaining_sec: Optional[int],
) -> Optional[Signal]:
    if remaining_sec is not None and remaining_sec < EARLY_EXIT_CUTOFF_SEC:
        return None  # hold to settlement
    side = position["side"]
    entry = float(position["entry_price"])
    if bid is None:
        return None
    if bid >= max(TAKE_PROFIT_BID, entry + TAKE_PROFIT_GAIN) or bid >= entry + TAKE_PROFIT_GAIN:
        return Signal(
            "TAKE_PROFIT",
            f"SELL NOW @ {cents_label(bid)}",
            f"Take profit: {side.upper()} bid is {cents_label(bid)} vs a {cents_label(entry)} entry. Sell now rather than holding to settlement.",
            side=side,
            bid=bid,
        )
    if bid <= entry - STOP_LOSS_DROP:
        return Signal(
            "CUT_LOSS",
            f"CUT LOSS @ {cents_label(bid)}",
            f"Cut loss: {side.upper()} bid fell to {cents_label(bid)} from a {cents_label(entry)} entry (−{(entry-bid)*100:.0f}¢). Sell now rather than hoping settlement saves it.",
            side=side,
            bid=bid,
        )
    # Momentum flip against the position, and the bid has at least stopped being a great hold.
    if ind and ind.ready and strike is not None:
        dist = btc_price - float(strike)
        if side == "yes" and ind.ema9 < ind.ema21 and dist < 0 and bid < entry:
            return Signal(
                "CUT_LOSS",
                f"CUT LOSS @ {cents_label(bid)}",
                f"Momentum flipped: EMA 9 crossed back below EMA 21 and BTC is now below the strike. Sell Yes at {cents_label(bid)} (entry {cents_label(entry)}).",
                side=side,
                bid=bid,
            )
        if side == "no" and ind.ema9 > ind.ema21 and dist > 0 and bid < entry:
            return Signal(
                "CUT_LOSS",
                f"CUT LOSS @ {cents_label(bid)}",
                f"Momentum flipped: EMA 9 crossed back above EMA 21 and BTC is now above the strike. Sell No at {cents_label(bid)} (entry {cents_label(entry)}).",
                side=side,
                bid=bid,
            )
    return None


# ---------------------------------------------------------------------------
# Paper book
# ---------------------------------------------------------------------------

def inferred_stake(obj: Optional[dict]) -> float:
    """Stake used for a trade or position. Legacy tickets with no field were $5."""
    if obj and obj.get("stake") is not None:
        try:
            return round_money(float(obj["stake"]))
        except (TypeError, ValueError):
            pass
    return LEGACY_FLAT_STAKE


def _clamp_press_value(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return round_money(min(STAKE_PRESS_CAP, max(STAKE_MIN, float(value))))
    except (TypeError, ValueError):
        return None


def ensure_press_fields(book: dict) -> dict:
    """Upgrade an existing book/session in place without touching cash or trades.

    Clamps any stored press/last-win stake into the [$4, $20] band.
    """
    if not isinstance(book, dict):
        return book
    book.setdefault("press_next", False)
    book.setdefault("last_win_stake", None)
    book.setdefault("stake", STAKE)
    for key in ("last_win_stake", "press_stake"):
        if book.get(key) is not None:
            book[key] = _clamp_press_value(book[key])
    return book


def resolve_entry_stake(
    base_stake: float,
    press_next: bool,
    last_win_stake: Optional[float],
    cash: float,
) -> tuple[float, bool]:
    """Return (stake, is_press). Press is 2× the last winning stake, hard-capped at $20 and at cash.

    If cash is under the $4.00 minimum, returns (0, False) so the caller skips the trade.
    """
    cash = max(0.0, float(cash or 0))
    if cash + 1e-9 < STAKE_MIN:
        return 0.0, False
    if press_next and last_win_stake:
        last = min(STAKE_PRESS_CAP, max(STAKE_MIN, float(last_win_stake)))
        target = min(STAKE_PRESS_CAP, round_money(2.0 * last))
        pressed = True
    else:
        target = float(base_stake if base_stake is not None else STAKE_NORMAL)
        pressed = False
    target = min(round_money(target), STAKE_PRESS_CAP, round_money(cash))
    if target + 1e-9 < STAKE_MIN:
        return 0.0, False
    return target, pressed and target > 0


def apply_press_from_trade(press_next: bool, last_win_stake: Optional[float], trade: dict) -> tuple[bool, Optional[float]]:
    """Update press state after a trade settles or exits. A win arms 2×; a loss resets."""
    pnl = trade.get("pnl")
    if pnl is None:
        return press_next, last_win_stake
    stake = inferred_stake(trade)
    if float(pnl) > 0:
        return True, _clamp_press_value(stake)
    return False, None


def next_stake_label(press_next: bool, last_win_stake: Optional[float], cash: Optional[float] = None) -> str:
    if press_next and last_win_stake:
        stake, _ = resolve_entry_stake(STAKE_NORMAL, True, last_win_stake, cash if cash is not None else STAKE_PRESS_CAP)
        return f"Next trade: 2× press (${stake:.0f})"
    return "Next stake: $4–$20"


def dedupe_trades(trades: Optional[list]) -> list[dict]:
    """If an open ticket and its closed record both exist, keep the closed one."""
    rows = [t for t in (trades or []) if t]
    best: dict[str, dict] = {}
    order: list[str] = []
    for t in rows:
        key = f"{t.get('ticker')}|{t.get('entry_time')}|{t.get('side')}"
        prev = best.get(key)
        if prev is None:
            best[key] = t
            order.append(key)
        elif t.get("status") == "closed" or (prev.get("status") != "closed" and t.get("pnl") is not None):
            best[key] = t
    return [best[k] for k in order]


def upsert_closed_trade(trades: list[dict], closed: dict) -> list[dict]:
    key_ticker = closed.get("ticker")
    key_time = closed.get("entry_time")
    key_side = closed.get("side")
    for i, t in enumerate(trades):
        if t.get("ticker") == key_ticker and t.get("entry_time") == key_time and t.get("side") == key_side:
            trades[i] = closed
            return trades
    trades.append(closed)
    return trades


def book_totals(trades: Optional[list], position: Optional[dict] = None) -> tuple[float, int]:
    rows = dedupe_trades(trades)
    if not rows and position:
        rows = [position]
    amount = round_money(sum(float(t.get("amount_bought") or t.get("total_in") or 0) for t in rows))
    contracts = int(sum(int(t.get("contracts") or 0) for t in rows))
    return amount, contracts


def decorate_position(position: Optional[dict], yes_bid: Optional[float], no_bid: Optional[float]) -> Optional[dict]:
    if not position:
        return position
    pos = dict(position)
    total_in = round_money(float(pos.get("total_in") or pos.get("amount_bought") or 0))
    pos["amount_bought"] = total_in
    pos["stake"] = inferred_stake(pos)
    bid = yes_bid if pos.get("side") == "yes" else no_bid
    if bid is not None and pos.get("contracts") is not None:
        pos["mark"] = round_money(int(pos["contracts"]) * float(bid))
        pos["pnl"] = round_money(pos["mark"] - total_in)
        pos["pnl_kind"] = "unrealized"
    return pos


def decorate_trade(trade: dict, yes_bid: Optional[float] = None, no_bid: Optional[float] = None) -> dict:
    t = dict(trade)
    total_in = t.get("amount_bought") or t.get("total_in")
    if total_in is not None:
        t["amount_bought"] = round_money(float(total_in))
    t["stake"] = inferred_stake(t)
    if t.get("status") == "open" and t.get("pnl") is None:
        bid = yes_bid if t.get("side") == "yes" else no_bid
        if bid is not None and t.get("contracts") is not None and t.get("amount_bought") is not None:
            t["pnl"] = round_money(int(t["contracts"]) * float(bid) - float(t["amount_bought"]))
            t["pnl_kind"] = "unrealized"
    elif t.get("pnl") is not None:
        t.setdefault("pnl_kind", "settled")
    return t


def default_state() -> dict:
    return {
        "cash": STARTING_BALANCE,
        "starting_balance": STARTING_BALANCE,
        "stake": STAKE,
        "press_next": False,
        "last_win_stake": None,
        "position": None,
        "trades": [],
        "equity_history": [],
        "created_at": iso_utc(utc_now()),
    }


def lift_race_bankroll(book: dict, target: float = RACE_STARTING) -> dict:
    """Raise a race book to a $100 starting balance without wiping trades or P&L.

    Adds the exact delta to cash (and equity, if present) so Amount Bought,
    contracts, record, and Total Gain/Lost stay the same.
    """
    if not isinstance(book, dict):
        return book
    try:
        start = float(book.get("starting_balance") or 0)
    except (TypeError, ValueError):
        start = 0.0
    target = float(target)
    if start + 1e-9 >= target:
        return book
    delta = round_money(target - start)
    book["starting_balance"] = round_money(target)
    book["cash"] = round_money(float(book.get("cash") or 0) + delta)
    if book.get("equity") is not None:
        try:
            book["equity"] = round_money(float(book["equity"]) + delta)
        except (TypeError, ValueError):
            pass
    if book.get("equity") is not None:
        try:
            book["pnl"] = round_money(float(book["equity"]) - float(book["starting_balance"]))
        except (TypeError, ValueError):
            pass
    if book.get("pnl") is not None:
        book["net_pnl"] = book["pnl"]
        try:
            start_now = float(book["starting_balance"])
            if start_now:
                book["pnl_pct"] = round((float(book["pnl"]) / start_now) * 100.0, 2)
        except (TypeError, ValueError):
            pass
    if book.get("stake") is not None:
        try:
            if float(book["stake"]) < STAKE_MIN:
                book["stake"] = STAKE_NORMAL
        except (TypeError, ValueError):
            pass
    book["next_stake_label"] = next_stake_label(
        bool(book.get("press_next")), book.get("last_win_stake"), book.get("cash")
    )
    return book


def lift_race(race: dict, target: float = RACE_STARTING) -> dict:
    """Lift every Version 1 / Version 2 race book (including legacy key aliases)."""
    if not isinstance(race, dict):
        return race
    try:
        start = float(race.get("starting_balance") or 0)
    except (TypeError, ValueError):
        start = 0.0
    if start + 1e-9 < float(target):
        race["starting_balance"] = round_money(target)
    for key in RACE_BOOK_KEYS:
        if isinstance(race.get(key), dict):
            lift_race_bankroll(race[key], target)
            ensure_press_fields(race[key])
    return race


def load_state() -> dict:
    if not STATE_PATH.exists():
        return default_state()
    with STATE_PATH.open() as f:
        state = json.load(f)
    state.setdefault("cash", STARTING_BALANCE)
    state.setdefault("starting_balance", STARTING_BALANCE)
    state.setdefault("stake", STAKE)
    state.setdefault("position", None)
    state.setdefault("trades", [])
    state.setdefault("equity_history", [])
    ensure_press_fields(state)
    if isinstance(state.get("session"), dict):
        ensure_press_fields(state["session"])
    if isinstance(state.get("race"), dict):
        lift_race(state["race"])
    return state


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    tmp.replace(path)


def mark_equity(cash: float, position: Optional[dict], yes_bid: Optional[float], no_bid: Optional[float]) -> float:
    if not position:
        return round_money(cash)
    bid = yes_bid if position["side"] == "yes" else no_bid
    if bid is None:
        return round_money(cash + float(position.get("cost", 0)))
    return round_money(cash + float(position["contracts"]) * bid)


def ticket_total(contracts: int, ask: float) -> tuple[float, float, float]:
    """Return (cost, fee, total) for a ticket."""
    fee = kalshi_taker_fee(contracts, ask)
    cost = round_money(contracts * float(ask))
    return cost, fee, round_money(cost + fee)


def size_ticket(ask: float, target_stake: float, cash: float) -> Optional[tuple[int, float, float, float]]:
    """Pick a whole number of contracts whose total cost is in [$4, $20] and ≤ cash.

    Starts with the largest size whose total ≤ the clamped target, then bumps
    contracts up (while staying ≤ $20 and ≤ cash) if that total would be under $4.
    Returns (contracts, cost, fee, total), or None to skip the trade.
    """
    if ask <= 0 or ask >= 1:
        return None
    cash = max(0.0, float(cash or 0))
    if cash + 1e-9 < STAKE_MIN:
        return None
    target = min(max(float(target_stake), STAKE_MIN), STAKE_PRESS_CAP, round_money(cash))
    if target + 1e-9 < STAKE_MIN:
        return None
    # Generous upper bound: fees are small, so this covers the $20 cap.
    max_n = int(min(STAKE_PRESS_CAP, cash) // ask) + 3
    best: Optional[tuple[int, float, float, float]] = None
    for n in range(1, max(1, max_n) + 1):
        cost, fee, total = ticket_total(n, ask)
        if total <= target + 1e-9:
            best = (n, cost, fee, total)
        else:
            break
    if best is None:
        n, cost, fee, total = 0, 0.0, 0.0, 0.0
    else:
        n, cost, fee, total = best
    while n >= 0 and total + 1e-9 < STAKE_MIN:
        nxt = ticket_total(n + 1, ask)
        if nxt[2] <= STAKE_PRESS_CAP + 1e-9 and nxt[2] <= cash + 1e-9:
            n += 1
            cost, fee, total = nxt
        else:
            break
    if n < 1 or total + 1e-9 < STAKE_MIN or total > STAKE_PRESS_CAP + 1e-9 or total > cash + 1e-9:
        return None
    return n, cost, fee, total


def open_trade(
    cash: float,
    market: dict,
    side: str,
    ask: float,
    reason: str,
    now: datetime,
    stake: float = STAKE,
    *,
    pressed: bool = False,
    strength: Optional[str] = None,
) -> Optional[tuple[float, dict, dict]]:
    if ask <= 0 or ask >= 1:
        return None
    sized = size_ticket(ask, stake, cash)
    if not sized:
        return None
    contracts, cost, fee, total = sized
    cash_after = round_money(cash - total)
    position = {
        "ticker": market["ticker"],
        "event_ticker": market.get("event_ticker"),
        "title": market.get("title") or "BTC price up in next 15 mins?",
        "side": side,
        "contracts": contracts,
        "entry_price": round_cent(ask),
        "entry_fee": fee,
        "cost": cost,
        "total_in": total,
        "amount_bought": total,
        "stake": round_money(float(stake)),
        "pressed": bool(pressed),
        "strength": strength,
        "entry_time": iso_utc(now),
        "entry_time_pt": fmt_pt(now),
        "close_time": market.get("close_time"),
        "close_time_pt": fmt_pt(market["close_time"]) if market.get("close_time") else None,
        "strike": dollars(market.get("floor_strike")),
        "reason": reason,
        "kalshi_url": contract_url(market["ticker"]),
    }
    log = {
        "status": "open",
        "ticker": position["ticker"],
        "side": side,
        "contracts": contracts,
        "entry_price": position["entry_price"],
        "entry_fee": fee,
        "entry_time": position["entry_time"],
        "entry_time_pt": position["entry_time_pt"],
        "reason": reason,
        "strike": position["strike"],
        "running_balance": cash_after,
        "total_in": total,
        "amount_bought": total,
        "stake": position["stake"],
        "pressed": bool(pressed),
        "strength": strength,
    }
    return cash_after, position, log


def close_trade(
    cash: float,
    position: dict,
    *,
    exit_price: Optional[float],
    exit_fee: float,
    result: Optional[str],
    outcome: str,
    reason: str,
    now: datetime,
    expiration_value: Optional[float] = None,
) -> tuple[float, dict]:
    contracts = int(position["contracts"])
    if outcome == "settlement":
        # Winning side pays $1 per contract, losing side pays $0. No extra settlement fee.
        won = (position["side"] == "yes" and result == "yes") or (position["side"] == "no" and result == "no")
        payout = contracts * (1.0 if won else 0.0)
        exit_price = 1.0 if won else 0.0
        exit_fee = 0.0
    else:
        payout = contracts * float(exit_price or 0.0)
    proceeds = round_money(payout - exit_fee)
    cash_after = round_money(cash + proceeds)
    pnl = round_money(proceeds - float(position["total_in"]))
    trade = {
        "status": "closed",
        "ticker": position["ticker"],
        "side": position["side"],
        "contracts": contracts,
        "entry_price": position["entry_price"],
        "entry_fee": position["entry_fee"],
        "entry_time": position["entry_time"],
        "entry_time_pt": position["entry_time_pt"],
        "exit_price": round_cent(exit_price or 0.0),
        "exit_fee": round_money(exit_fee),
        "exit_time": iso_utc(now),
        "exit_time_pt": fmt_pt(now),
        "outcome": outcome,
        "result": result,
        "expiration_value": expiration_value,
        "strike": position.get("strike"),
        "pnl": pnl,
        "reason_entry": position.get("reason"),
        "reason_exit": reason,
        "kalshi_url": position.get("kalshi_url") or contract_url(position["ticker"]),
        "running_balance": cash_after,
        "total_in": position.get("total_in"),
        "amount_bought": position.get("amount_bought") or position.get("total_in"),
        "stake": inferred_stake(position),
        "pressed": bool(position.get("pressed")),
        "strength": position.get("strength"),
        "pnl_kind": "settled",
    }
    return cash_after, trade


def trade_stats(trades: list[dict]) -> dict:
    closed = [t for t in trades if t.get("status") == "closed" and t.get("pnl") is not None]
    wins = [t for t in closed if float(t["pnl"]) > 0]
    losses = [t for t in closed if float(t["pnl"]) <= 0]
    gp = sum(float(t["pnl"]) for t in wins)
    gl = abs(sum(float(t["pnl"]) for t in losses))
    if closed and gl == 0 and gp > 0:
        pf, pf_note = None, "n/a (no losing trades)"
    elif not closed:
        pf, pf_note = None, "n/a (no closed trades)"
    else:
        pf, pf_note = (round(gp / gl, 2) if gl else 0.0), None
    return {
        "trade_count": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100.0, 1) if closed else None,
        "profit_factor": pf,
        "profit_factor_note": pf_note,
        "gross_profit": round_money(gp),
        "gross_loss": round_money(gl),
        "net_pnl": round_money(sum(float(t["pnl"]) for t in closed)),
    }


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------

def run_backtest(
    settled: list[dict],
    sticks: dict[str, list[dict]],
    candles_1m: list[dict],
    series_1m: list[Indicators],
    candles_5m: list[dict],
    series_5m: list[Indicators],
    params: Optional[StrategyParams] = None,
) -> dict:
    params = params or StrategyParams()
    # Oldest first, only markets with a real yes/no result and a strike.
    markets = [
        m
        for m in settled
        if (m.get("result") in ("yes", "no")) and dollars(m.get("floor_strike")) is not None and m.get("open_time")
    ]
    markets.sort(key=lambda m: to_ts(m["open_time"]))

    cash = STARTING_BALANCE
    trades: list[dict] = []
    equity: list[dict] = []
    scanned = 0
    skipped = 0
    skip_reasons: dict[str, int] = {}
    press_next = False
    last_win_stake = None

    for m in markets:
        open_ts = to_ts(m["open_time"])
        decision_ts = open_ts + DECISION_OFFSET_SEC
        scanned += 1
        q = quote_at_decision(sticks.get(m["ticker"]) or [], open_ts)
        snap_1 = indicators_at(candles_1m, series_1m, decision_ts - 1)
        snap_5 = indicators_at(candles_5m, series_5m, decision_ts - 1)
        if not q or not snap_1 or not snap_1[1].ready:
            skipped += 1
            key = "missing quotes or indicators"
            skip_reasons[key] = skip_reasons.get(key, 0) + 1
            continue
        btc_px = snap_1[0]["close"]
        strike = dollars(m["floor_strike"])
        vol, chop = window_chop_features(candles_1m, decision_ts - 1)
        sig = make_signal(
            ind_1m=snap_1[1],
            ind_5m=snap_5[1] if snap_5 else None,
            btc_price=btc_px,
            strike=strike,
            yes_ask=q["yes_ask"],
            yes_bid=q["yes_bid"],
            no_ask=q["no_ask"],
            no_bid=q["no_bid"],
            remaining_sec=to_ts(m["close_time"]) - decision_ts,
            position=None,
            params=params,
            features={"volatility_usd": vol, "chop_ratio": chop},
        )
        if sig.action not in ("BUY_YES", "BUY_NO"):
            skipped += 1
            key = sig.label
            skip_reasons[key] = skip_reasons.get(key, 0) + 1
            equity.append({"t": decision_ts, "t_pt": fmt_pt(decision_ts), "equity": round_money(cash)})
            continue
        base = float(sig.base_stake or STAKE_NORMAL)
        entry_stake, pressed = resolve_entry_stake(base, press_next, last_win_stake, cash)
        opened = open_trade(
            cash, m, sig.side or "yes", float(sig.ask), sig.reason, parse_iso(decision_ts),
            stake=entry_stake, pressed=pressed, strength=sig.strength,
        )
        if not opened:
            skipped += 1
            skip_reasons["not enough cash / size"] = skip_reasons.get("not enough cash / size", 0) + 1
            continue
        cash, position, _log = opened
        close_dt = parse_iso(m.get("settlement_ts") or m["close_time"])
        cash, trade = close_trade(
            cash,
            position,
            exit_price=None,
            exit_fee=0.0,
            result=m["result"],
            outcome="settlement",
            reason=f"Held to settlement: official result was {m['result'].upper()} (BTC {dollars(m.get('expiration_value')) or 'n/a'} vs strike {strike}).",
            now=close_dt,
            expiration_value=dollars(m.get("expiration_value")),
        )
        trades.append(trade)
        press_next, last_win_stake = apply_press_from_trade(press_next, last_win_stake, trade)
        equity.append({"t": to_ts(close_dt), "t_pt": fmt_pt(close_dt), "equity": cash})

    stats = trade_stats(trades)
    start_eq = STARTING_BALANCE
    end_eq = cash
    # Max drawdown on the equity path.
    peak = start_eq
    max_dd = 0.0
    running = start_eq
    for pt in equity:
        running = float(pt["equity"])
        if running > peak:
            peak = running
        if peak > 0:
            max_dd = max(max_dd, (peak - running) / peak)

    first_t = to_ts(markets[0]["open_time"]) if markets else None
    last_t = to_ts(markets[-1]["close_time"]) if markets else None
    return {
        "start": iso_utc(first_t) if first_t else None,
        "end": iso_utc(last_t) if last_t else None,
        "start_pt": fmt_pt(first_t) if first_t else None,
        "end_pt": fmt_pt(last_t) if last_t else None,
        "days": round((last_t - first_t) / 86400, 1) if first_t and last_t else 0,
        "windows_scanned": scanned,
        "windows_traded": stats["trade_count"],
        "windows_skipped": skipped,
        "skip_reasons": skip_reasons,
        "starting_balance": STARTING_BALANCE,
        "stake": STAKE,
        "ending_equity": round_money(end_eq),
        "return_pct": round(((end_eq / start_eq) - 1.0) * 100.0, 2) if start_eq else 0.0,
        "max_drawdown_pct": round(max_dd * 100.0, 2),
        **stats,
        "equity_history": equity,
        "trades": list(reversed(trades[-80:])),
        "assumption_note": (
            "Each window is evaluated once, 1 minute after it opens, using the real Kalshi Yes/No "
            "quotes at that minute and Coinbase 1-minute / 5-minute BTC candles. Trades use a $4–$8 "
            "base stake by signal strength, then 2× after a win (capped at $20), "
            "the official quadratic taker fee, and are held to Kalshi’s official Yes/No settlement. "
            "The live bot can also enter later in a window (on a 5-minute scan) or exit early if momentum flips; "
            "those live extras are not in this backtest."
        ),
        "fee_note": "Paper PnL includes Kalshi’s taker fee: ceil(0.07 × contracts × price × (1 − price)), rounded up to the next cent.",
    }


def simulate_prepared_windows(
    windows: list[dict],
    params: StrategyParams,
    starting: float = STARTING_BALANCE,
) -> dict:
    """Replay settled feature-windows with live sizing, fees, and 2× press."""
    cash = float(starting)
    press_next = False
    last_win_stake: Optional[float] = None
    trades: list[dict] = []
    peak = float(starting)
    max_dd = 0.0
    for w in windows:
        ind1 = indicators_from_payload(w.get("ind_1m"))
        ind5 = indicators_from_payload(w.get("ind_5m"))
        sig = make_signal(
            ind_1m=ind1,
            ind_5m=ind5,
            btc_price=float(w["btc_price"]),
            strike=w.get("strike"),
            yes_ask=w.get("yes_ask"),
            yes_bid=w.get("yes_bid"),
            no_ask=w.get("no_ask"),
            no_bid=w.get("no_bid"),
            remaining_sec=w.get("remaining_sec"),
            position=None,
            params=params,
            features={"volatility_usd": w.get("volatility_usd"), "chop_ratio": w.get("chop_ratio")},
        )
        if sig.action not in ("BUY_YES", "BUY_NO") or sig.ask is None:
            continue
        base = float(sig.base_stake or STAKE_NORMAL)
        entry_stake, pressed = resolve_entry_stake(base, press_next, last_win_stake, cash)
        market = {
            "ticker": w["ticker"],
            "floor_strike": w.get("strike"),
            "open_time": w.get("open_time"),
            "close_time": w.get("close_time"),
        }
        when = parse_iso(w.get("decision_ts") or w.get("open_time"))
        opened = open_trade(
            cash, market, sig.side or "yes", float(sig.ask), sig.reason, when,
            stake=entry_stake, pressed=pressed, strength=sig.strength,
        )
        if not opened:
            continue
        cash, position, _log = opened
        result = (w.get("result") or "").lower()
        close_when = parse_iso(w.get("close_time") or w.get("decision_ts") or when)
        cash, trade = close_trade(
            cash,
            position,
            exit_price=None,
            exit_fee=0.0,
            result=result,
            outcome="settlement",
            reason=f"Settled {result.upper()} at BTC {w.get('expiration_value') or 'n/a'} vs strike {w.get('strike')}.",
            now=close_when,
            expiration_value=w.get("expiration_value"),
        )
        press_next, last_win_stake = apply_press_from_trade(press_next, last_win_stake, trade)
        trades.append(trade)
        peak = max(peak, cash)
        if peak > 0:
            max_dd = max(max_dd, (peak - cash) / peak * 100.0)
    stats = trade_stats(trades)
    wins = [t for t in trades if (t.get("pnl") or 0) > 0]
    losses = [t for t in trades if (t.get("pnl") or 0) <= 0]
    gross_win = sum(float(t["pnl"]) for t in wins)
    gross_loss = abs(sum(float(t["pnl"]) for t in losses))
    if gross_loss > 0:
        pf = gross_win / gross_loss
    elif gross_win > 0:
        pf = 999.0
    else:
        pf = 0.0
    return {
        "trade_count": stats["trade_count"],
        "wins": stats["wins"],
        "losses": stats["losses"],
        "win_rate": stats["win_rate"],
        "net_pnl": stats["net_pnl"],
        "ending_equity": round_money(cash),
        "max_drawdown_pct": round(max_dd, 2),
        "profit_factor": round(pf, 3),
        "starting_balance": starting,
        "trades": trades,
    }


# ---------------------------------------------------------------------------
# Live paper step
# ---------------------------------------------------------------------------

def pick_live_market(open_markets: list[dict], settled: list[dict], now: datetime) -> Optional[dict]:
    now_ts = int(now.timestamp())
    candidates = []
    for m in open_markets + settled:
        if not m.get("open_time") or not m.get("close_time"):
            continue
        if m.get("ticker") and to_ts(m["open_time"]) <= now_ts < to_ts(m["close_time"]) + 1:
            candidates.append(m)
    if not candidates:
        # Prefer an open/active market even if clocks are slightly off.
        live = [m for m in open_markets if (m.get("status") in ("open", "active", "initialized") or True) and dollars(m.get("yes_ask_dollars")) is not None]
        live = [m for m in live if m.get("status") in ("open", "active")]
        return live[0] if live else None
    candidates.sort(key=lambda m: to_ts(m["close_time"]))
    return candidates[-1]


def find_market(ticker: str, *groups: list[dict]) -> Optional[dict]:
    for group in groups:
        for m in group:
            if m.get("ticker") == ticker:
                return m
    return None


def market_payload(market: Optional[dict], now: datetime) -> Optional[dict]:
    if not market:
        return None
    close_ts = to_ts(market["close_time"]) if market.get("close_time") else None
    remaining = (close_ts - int(now.timestamp())) if close_ts else None
    yes_ask = dollars(market.get("yes_ask_dollars"))
    yes_bid = dollars(market.get("yes_bid_dollars"))
    no_ask = dollars(market.get("no_ask_dollars"))
    no_bid = dollars(market.get("no_bid_dollars"))
    if no_ask is None and yes_bid is not None:
        no_ask = round_cent(max(0.0, 1.0 - yes_bid))
    if no_bid is None and yes_ask is not None:
        no_bid = round_cent(max(0.0, 1.0 - yes_ask))
    return {
        "ticker": market["ticker"],
        "event_ticker": market.get("event_ticker"),
        "title": market.get("title") or "BTC price up in next 15 mins?",
        "subtitle": market.get("yes_sub_title") or "",
        "status": market.get("status"),
        "strike": dollars(market.get("floor_strike")),
        "open_time": market.get("open_time"),
        "open_time_pt": fmt_pt(market["open_time"]) if market.get("open_time") else None,
        "close_time": market.get("close_time"),
        "close_time_pt": fmt_pt(market["close_time"]) if market.get("close_time") else None,
        "remaining_sec": remaining,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "last": dollars(market.get("last_price_dollars")),
        "volume": dollars(market.get("volume_fp")),
        "result": market.get("result") or None,
        "expiration_value": dollars(market.get("expiration_value")),
        "kalshi_url": contract_url(market["ticker"]),
        "rules": market.get("rules_primary") or "",
    }


def step_paper_book(
    cash: float,
    position: Optional[dict],
    trades: list[dict],
    events: list[str],
    market: Optional[dict],
    settled: list[dict],
    ind_1m: Optional[Indicators],
    ind_5m: Optional[Indicators],
    btc_price: float,
    now: datetime,
    *,
    allow_new_entries: bool,
    stake: float = STAKE,
    press_next: bool = False,
    last_win_stake: Optional[float] = None,
    params: Optional[StrategyParams] = None,
    features: Optional[dict] = None,
    max_trades_per_window: Optional[int] = None,
) -> tuple[float, Optional[dict], list[dict], list[str], Signal, Optional[float], Optional[float], bool, Optional[float]]:
    """Apply settlements, early exits, and optional new entries to one paper book."""
    params = params or StrategyParams()
    features = features or {}
    if position:
        pos_m = find_market(position["ticker"], settled, [market] if market else [])
        if pos_m is None:
            try:
                data = http_get_json(f"{KALSHI_API}/markets/{urllib.parse.quote(position['ticker'])}")
                pos_m = data.get("market") or data
            except Exception as exc:
                events.append(f"Could not refresh open market {position['ticker']}: {exc}")

        if pos_m and (pos_m.get("result") in ("yes", "no") or pos_m.get("status") in ("finalized", "settled", "determined")):
            if pos_m.get("result") in ("yes", "no"):
                cash, trade = close_trade(
                    cash,
                    position,
                    exit_price=None,
                    exit_fee=0.0,
                    result=pos_m["result"],
                    outcome="settlement",
                    reason=f"Settled {pos_m['result'].upper()} at BTC {dollars(pos_m.get('expiration_value')) or 'n/a'} vs strike {position.get('strike')}.",
                    now=parse_iso(pos_m.get("settlement_ts") or now),
                    expiration_value=dollars(pos_m.get("expiration_value")),
                )
                upsert_closed_trade(trades, trade)
                press_next, last_win_stake = apply_press_from_trade(press_next, last_win_stake, trade)
                events.append(
                    f"Settled {position['side'].upper()} on {position['ticker']}: {trade['reason_exit']} PnL {trade['pnl']:+.2f}"
                )
                position = None

    live_mp = market_payload(market, now) if market else None
    yes_ask = live_mp["yes_ask"] if live_mp else None
    yes_bid = live_mp["yes_bid"] if live_mp else None
    no_ask = live_mp["no_ask"] if live_mp else None
    no_bid = live_mp["no_bid"] if live_mp else None
    strike = live_mp["strike"] if live_mp else (position or {}).get("strike")
    remaining = live_mp["remaining_sec"] if live_mp else None

    if position and market and market.get("ticker") == position["ticker"]:
        sig = make_signal(
            ind_1m=ind_1m,
            ind_5m=ind_5m,
            btc_price=btc_price,
            strike=strike,
            yes_ask=yes_ask,
            yes_bid=yes_bid,
            no_ask=no_ask,
            no_bid=no_bid,
            remaining_sec=remaining,
            position=position,
            params=params,
            features=features,
        )
        if sig.action in ("TAKE_PROFIT", "CUT_LOSS") and sig.bid is not None:
            fee = kalshi_taker_fee(int(position["contracts"]), float(sig.bid))
            cash, trade = close_trade(
                cash,
                position,
                exit_price=float(sig.bid),
                exit_fee=fee,
                result=None,
                outcome="early_exit",
                reason=sig.reason,
                now=now,
            )
            upsert_closed_trade(trades, trade)
            press_next, last_win_stake = apply_press_from_trade(press_next, last_win_stake, trade)
            events.append(
                f"{sig.action} {position['side'].upper()} on {position['ticker']} @ {cents_label(sig.bid)} PnL {trade['pnl']:+.2f}"
            )
            position = None
    else:
        sig = make_signal(
            ind_1m=ind_1m,
            ind_5m=ind_5m,
            btc_price=btc_price,
            strike=strike,
            yes_ask=yes_ask,
            yes_bid=yes_bid,
            no_ask=no_ask,
            no_bid=no_bid,
            remaining_sec=remaining,
            position=position,
            params=params,
            features=features,
        )

    ticker = (market or {}).get("ticker")
    window_trades = 0
    if ticker:
        window_trades = sum(1 for t in trades if t.get("ticker") == ticker)
        if position and position.get("ticker") == ticker:
            # An open ticket may already be in trades; don't double-count.
            window_trades = max(window_trades, 1)
    window_full = (
        max_trades_per_window is not None
        and ticker is not None
        and window_trades >= int(max_trades_per_window)
    )
    if window_full and position is None and sig.action in ("BUY_YES", "BUY_NO"):
        sig = Signal(
            "WAIT",
            "WAIT — Already traded this window",
            "Strict Rules already used its one trade in this 15-minute window, so it stands aside until the next contract.",
            side=sig.side,
            ask=sig.ask,
            bid=sig.bid,
            strength=sig.strength,
            base_stake=sig.base_stake,
        )

    if allow_new_entries and (not window_full) and position is None and market and sig.action in ("BUY_YES", "BUY_NO") and sig.ask is not None:
        base = float(sig.base_stake or stake or STAKE_NORMAL)
        entry_stake, pressed = resolve_entry_stake(base, press_next, last_win_stake, cash)
        opened = open_trade(
            cash, market, sig.side or "yes", float(sig.ask), sig.reason, now,
            stake=entry_stake, pressed=pressed, strength=sig.strength,
        )
        if opened:
            cash, position, log = opened
            trades.append(log)
            press_tag = " 2× PRESS" if pressed else ""
            events.append(
                f"Opened {position['side'].upper()} {position['contracts']}x {position['ticker']} @ {cents_label(position['entry_price'])} "
                f"(bought ${position['total_in']:.2f}, ${position['stake']:.0f} ticket{press_tag}, fee ${position['entry_fee']:.2f})"
            )
            sig = Signal(
                "HOLD",
                f"HOLD {position['side'].upper()} @ {cents_label(position['entry_price'])}",
                f"Just bought {position['side'].upper()} — {sig.reason}",
                side=position["side"],
                ask=sig.ask,
                bid=yes_bid if position["side"] == "yes" else no_bid,
                strength=sig.strength,
                base_stake=sig.base_stake,
            )
        else:
            events.append("Signal fired but not enough paper cash to size the ticket after fees.")
            sig = Signal(
                "WAIT",
                "WAIT — No Trade",
                sig.reason + " Not enough paper cash to size the ticket after fees.",
                strength=sig.strength,
                base_stake=sig.base_stake,
            )
    elif (not allow_new_entries) and position is None and sig.action in ("BUY_YES", "BUY_NO"):
        # Keep the underlying signal for the check log; caller will mark it as skipped.
        pass

    if position and sig.action not in ("TAKE_PROFIT", "CUT_LOSS", "HOLD"):
        sig = make_signal(
            ind_1m=ind_1m,
            ind_5m=ind_5m,
            btc_price=btc_price,
            strike=strike,
            yes_ask=yes_ask,
            yes_bid=yes_bid,
            no_ask=no_ask,
            no_bid=no_bid,
            remaining_sec=remaining,
            position=position,
            params=params,
            features=features,
        )

    return cash, position, trades, events, sig, yes_bid, no_bid, press_next, last_win_stake


def run_live(
    state: dict,
    market: Optional[dict],
    settled: list[dict],
    ind_1m: Optional[Indicators],
    ind_5m: Optional[Indicators],
    btc_price: float,
    now: datetime,
    params: Optional[StrategyParams] = None,
    features: Optional[dict] = None,
) -> tuple[dict, Signal, list[str]]:
    events: list[str] = []
    ensure_press_fields(state)
    cash, position, trades, events, sig, yes_bid, no_bid, press_next, last_win_stake = step_paper_book(
        float(state["cash"]),
        state.get("position"),
        list(state.get("trades") or []),
        events,
        market,
        settled,
        ind_1m,
        ind_5m,
        btc_price,
        now,
        allow_new_entries=True,
        stake=float(state.get("stake") or STAKE),
        press_next=bool(state.get("press_next")),
        last_win_stake=state.get("last_win_stake"),
        params=params,
        features=features,
    )
    equity_history = list(state.get("equity_history") or [])
    equity = mark_equity(cash, position, yes_bid, no_bid)
    equity_history.append({"t": int(now.timestamp()), "t_pt": fmt_pt(now), "equity": equity})
    if len(equity_history) > 2000:
        equity_history = equity_history[-2000:]

    new_state = {
        "cash": round_money(cash),
        "starting_balance": float(state.get("starting_balance") or STARTING_BALANCE),
        "stake": STAKE,
        "press_next": bool(press_next),
        "last_win_stake": last_win_stake,
        "position": decorate_position(position, yes_bid, no_bid),
        "trades": trades,
        "equity_history": equity_history,
        "created_at": state.get("created_at") or iso_utc(now),
        "updated_at": iso_utc(now),
        "updated_at_pt": fmt_pt(now),
        "session": state.get("session"),
        "race": state.get("race"),
    }
    return new_state, sig, events


def new_session(now: datetime) -> dict:
    end = now + timedelta(minutes=SESSION_MINUTES)
    return {
        "starting_balance": SESSION_STARTING,
        "stake": SESSION_STAKE,
        "press_next": False,
        "last_win_stake": None,
        "cash": SESSION_STARTING,
        "position": None,
        "trades": [],
        "checks": [],
        "start": iso_utc(now),
        "start_pt": fmt_pt(now),
        "end": iso_utc(end),
        "end_pt": fmt_pt(end),
        "window_label": session_window_label(now, end),
        "status": "LIVE",
        "status_label": f"LIVE · {SESSION_MINUTES}m left",
        "remaining_sec": SESSION_MINUTES * 60,
        "equity": SESSION_STARTING,
        "pnl": 0.0,
        "pnl_pct": 0.0,
        "final_balance": None,
        "completed_at": None,
        "completed_at_pt": None,
        "created_at": iso_utc(now),
    }


def run_session(
    state: dict,
    market: Optional[dict],
    settled: list[dict],
    ind_1m: Optional[Indicators],
    ind_5m: Optional[Indicators],
    btc_price: float,
    now: datetime,
    params: Optional[StrategyParams] = None,
    features: Optional[dict] = None,
) -> dict:
    sess = state.get("session")
    if not sess or not sess.get("start") or not sess.get("end"):
        sess = new_session(now)
        print(
            f"  Starting 1-hour ${SESSION_STARTING:.0f} session: {sess['window_label']}",
            flush=True,
        )

    # Already locked — do not reopen or change the book.
    if sess.get("status") == "COMPLETE" and sess.get("final_balance") is not None:
        sess = dict(sess)
        sess["remaining_sec"] = 0
        sess["status_label"] = "COMPLETE"
        return sess

    end = parse_iso(sess["end"])
    allow_new = now < end
    events: list[str] = []
    ensure_press_fields(sess)
    cash, position, trades, events, sig, yes_bid, no_bid, press_next, last_win_stake = step_paper_book(
        float(sess.get("cash") or SESSION_STARTING),
        sess.get("position"),
        list(sess.get("trades") or []),
        events,
        market,
        settled,
        ind_1m,
        ind_5m,
        btc_price,
        now,
        allow_new_entries=allow_new,
        stake=float(sess.get("stake") or SESSION_STAKE),
        press_next=bool(sess.get("press_next")),
        last_win_stake=sess.get("last_win_stake"),
        params=params,
        features=features,
    )

    action = sig.action
    label = sig.label
    reason = sig.reason
    if not allow_new and action in ("BUY_YES", "BUY_NO"):
        action = "SKIP"
        label = "SKIP — Session window ended"
        reason = f"The 1-hour $40 window has ended, so no new trade was opened. Signal was: {sig.reason}"
    elif not allow_new and position is None and action == "WAIT":
        reason = "Hour ended — watching only to settle any leftover trade. " + sig.reason

    mp = market_payload(market, now) if market else None
    checks = list(sess.get("checks") or [])
    ticker = (mp or {}).get("ticker") or (position or {}).get("ticker")
    close_time = (mp or {}).get("close_time") or (position or {}).get("close_time")
    checks.append(
        {
            "t": iso_utc(now),
            "t_pt": fmt_pt(now),
            "ticker": ticker,
            "close_time": close_time,
            "close_time_pt": (mp or {}).get("close_time_pt") or (fmt_pt(close_time) if close_time else None),
            "window_label": contract_window_label(close_time) if close_time else None,
            "action": action,
            "label": label,
            "reason": reason,
            "btc_price": round_px(btc_price, 2),
            "strike": (mp or {}).get("strike") or (position or {}).get("strike"),
            "yes_ask": (mp or {}).get("yes_ask"),
            "no_ask": (mp or {}).get("no_ask"),
            "side": (position or {}).get("side") or sig.side,
            "contracts": (position or {}).get("contracts"),
            "entry_price": (position or {}).get("entry_price"),
            "cash": round_money(cash),
        }
    )
    checks = checks[-40:]

    start_bal = float(sess.get("starting_balance") or SESSION_STARTING)
    position = decorate_position(position, yes_bid, no_bid)
    trades = [decorate_trade(t, yes_bid, no_bid) for t in trades]
    amount_bought, contracts_total = book_totals(trades, position)
    equity = mark_equity(cash, position, yes_bid, no_bid)
    stats = trade_stats(trades)
    remaining = int((end - now).total_seconds())
    if remaining > 0:
        status = "LIVE"
        mm = remaining // 60
        status_label = f"LIVE · {mm}m left"
    elif position:
        status = "SETTLING"
        status_label = "SETTLING"
        remaining = 0
    else:
        status = "COMPLETE"
        status_label = "COMPLETE"
        remaining = 0
        equity = round_money(cash)

    out = {
        "starting_balance": start_bal,
        "stake": float(sess.get("stake") or SESSION_STAKE),
        "press_next": bool(press_next),
        "last_win_stake": last_win_stake,
        "next_stake_label": next_stake_label(press_next, last_win_stake, cash),
        "cash": round_money(cash),
        "position": position,
        "trades": trades,
        "amount_bought": amount_bought,
        "contracts_total": contracts_total,
        "checks": checks,
        "start": sess["start"],
        "start_pt": sess.get("start_pt") or fmt_pt(sess["start"]),
        "end": sess["end"],
        "end_pt": sess.get("end_pt") or fmt_pt(sess["end"]),
        "window_label": sess.get("window_label") or session_window_label(sess["start"], sess["end"]),
        "status": status,
        "status_label": status_label,
        "remaining_sec": max(0, remaining),
        "equity": equity,
        "pnl": round_money(equity - start_bal),
        "pnl_pct": round(((equity - start_bal) / start_bal) * 100.0, 2) if start_bal else 0.0,
        "trade_count": stats["trade_count"],
        "wins": stats["wins"],
        "losses": stats["losses"],
        "win_rate": stats["win_rate"],
        "net_pnl": stats["net_pnl"] if not position else round_money(equity - start_bal),
        "final_balance": sess.get("final_balance"),
        "completed_at": sess.get("completed_at"),
        "completed_at_pt": sess.get("completed_at_pt"),
        "created_at": sess.get("created_at") or sess["start"],
        "updated_at": iso_utc(now),
        "updated_at_pt": fmt_pt(now),
        "events": events,
    }
    if status == "COMPLETE" and out["final_balance"] is None:
        out["final_balance"] = round_money(cash)
        out["equity"] = round_money(cash)
        out["pnl"] = round_money(cash - start_bal)
        out["pnl_pct"] = round(((cash - start_bal) / start_bal) * 100.0, 2) if start_bal else 0.0
        out["net_pnl"] = out["pnl"]
        out["completed_at"] = iso_utc(now)
        out["completed_at_pt"] = fmt_pt(now)
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Head-to-head $100 race: Version 1 (multi-trade) vs Version 2 (strict rules)
# ---------------------------------------------------------------------------

def tradable_v1_ask(ask: Optional[float]) -> bool:
    return ask is not None and V1_ASK_MIN - 1e-12 <= float(ask) <= V1_ASK_MAX + 1e-12


def dist_phrase(btc_price: float, strike: Optional[float]) -> str:
    if strike is None:
        return "the strike isn’t posted yet"
    dist = float(btc_price) - float(strike)
    if dist >= 0:
        return f"BTC is +${dist:,.0f} above strike"
    return f"BTC is −${abs(dist):,.0f} below strike"


def score_side(
    side: str,
    ind: Optional[Indicators],
    trend: Optional[Indicators],
    btc_price: float,
    strike: Optional[float],
    ask: Optional[float],
) -> tuple[float, int]:
    """Return (score, alignment_count 0–6) for Yes or No. Higher score wins."""
    score = 0.0
    aligns = 0
    dist = (float(btc_price) - float(strike)) if strike is not None else 0.0
    favored = (side == "yes" and dist > 0) or (side == "no" and dist < 0)
    if strike is not None:
        if favored:
            score += 2.0 + min(abs(dist) / 25.0, 3.0)
            aligns += 1
        else:
            score -= 1.0 + min(abs(dist) / 25.0, 3.0)
    if ind and ind.ready:
        ema_up = (ind.ema9 or 0) > (ind.ema21 or 0)
        if (side == "yes" and ema_up) or (side == "no" and not ema_up):
            score += 2.0
            aligns += 1
            if abs((ind.ema9 or 0) - (ind.ema21 or 0)) >= 15:
                score += 0.5
        else:
            score -= 2.0
        if ind.ema50 is not None and ind.close is not None:
            above50 = ind.close > ind.ema50
            if (side == "yes" and above50) or (side == "no" and not above50):
                score += 1.5
                aligns += 1
            else:
                score -= 1.0
        macd_up = (ind.macd_hist or 0) > 0
        if (side == "yes" and macd_up) or (side == "no" and not macd_up):
            score += 1.5
            aligns += 1
        else:
            score -= 1.0
        rsi = ind.rsi14
        if rsi is not None:
            if side == "yes":
                if rsi >= 55:
                    score += 1.5
                    aligns += 1
                elif rsi >= 50:
                    score += 0.4
                else:
                    score -= 1.2
            else:
                if rsi <= 45:
                    score += 1.5
                    aligns += 1
                elif rsi <= 50:
                    score += 0.4
                else:
                    score -= 1.2
    if trend and trend.ready:
        t_up = (trend.ema9 or 0) >= (trend.ema21 or 0)
        if (side == "yes" and t_up) or (side == "no" and not t_up):
            score += 1.5
            aligns += 1
        else:
            score -= 1.0
    if tradable_v1_ask(ask):
        score += (0.70 - float(ask)) * 3.0
    elif ask is not None:
        score -= 8.0
    return score, aligns


def v1_strength(aligns: int, margin: float) -> tuple[str, float]:
    if aligns >= 5 and margin >= 2.0:
        return "top", STAKE_TOP
    if aligns >= 3 and margin >= 1.0:
        return "strong", STAKE_STRONG
    return "normal", STAKE_NORMAL


def _trend_bits(side: str, ind: Optional[Indicators], trend: Optional[Indicators]) -> list[str]:
    bits: list[str] = []
    if ind and ind.ready:
        if side == "yes" and (ind.ema9 or 0) > (ind.ema21 or 0):
            bits.append("1m trending up")
        elif side == "no" and (ind.ema9 or 0) < (ind.ema21 or 0):
            bits.append("1m trending down")
        if ind.rsi14 is not None:
            if side == "no" and ind.rsi14 <= 48:
                bits.append(f"RSI cooled to {ind.rsi14:.0f}")
            elif side == "yes" and ind.rsi14 >= 52:
                bits.append(f"RSI {ind.rsi14:.0f}")
    if trend and trend.ready:
        if side == "yes" and (trend.ema9 or 0) >= (trend.ema21 or 0):
            bits.append("5m trending up")
        elif side == "no" and (trend.ema9 or 0) <= (trend.ema21 or 0):
            bits.append("5m trending down")
    return bits


def make_v1_signal(
    *,
    ind_1m: Optional[Indicators],
    ind_5m: Optional[Indicators],
    btc_price: float,
    strike: Optional[float],
    yes_ask: Optional[float],
    yes_bid: Optional[float],
    no_ask: Optional[float],
    no_bid: Optional[float],
    remaining_sec: Optional[int],
    open_positions: Optional[list] = None,
    window_trade_count: int = 0,
    last_entry: Optional[dict] = None,
    ticker: Optional[str] = None,
) -> Signal:
    """Version 1: pick a side every window; may add or flip up to 3 times per contract."""
    positions = [p for p in (open_positions or []) if p and (not ticker or p.get("ticker") == ticker)]
    if remaining_sec is not None and remaining_sec <= 0:
        return Signal("WAIT", "WAIT — Window closed", "This 15-minute window has closed. Waiting for the next contract.")
    if remaining_sec is not None and remaining_sec < V1_MIN_REMAINING_SEC:
        mm, ss = divmod(max(0, remaining_sec), 60)
        label = "HOLD" if positions else "WAIT"
        return Signal(
            "HOLD" if positions else "WAIT",
            ("HOLD open trades — " if positions else "") + "Too close to expiry",
            f"Only {mm}:{ss:02d} left in this window, so Version 1 won’t open another ticket.",
            side=(positions[-1].get("side") if positions else None),
        )
    yes_ok, no_ok = tradable_v1_ask(yes_ask), tradable_v1_ask(no_ask)
    if not yes_ok and not no_ok:
        return Signal(
            "HOLD" if positions else "WAIT",
            "WAIT — No tradable ask",
            "Neither Yes nor No is between 5¢ and 95¢, so Version 1 stands aside.",
            side=(positions[-1].get("side") if positions else None),
        )
    if strike is None:
        return Signal("WAIT", "WAIT — No Trade", "The contract strike is not posted yet.")

    yes_score, yes_al = score_side("yes", ind_1m, ind_5m, btc_price, strike, yes_ask)
    no_score, no_al = score_side("no", ind_1m, ind_5m, btc_price, strike, no_ask)
    if not yes_ok:
        yes_score = -999.0
    if not no_ok:
        no_score = -999.0

    if yes_score >= no_score:
        pick, ask, bid, score, other, aligns = "yes", yes_ask, yes_bid, yes_score, no_score, yes_al
    else:
        pick, ask, bid, score, other, aligns = "no", no_ask, no_bid, no_score, yes_score, no_al
    margin = float(score) - float(other)
    strength, stake = v1_strength(aligns, margin)
    lean = " (choppy lean)" if strength == "normal" and aligns <= 2 else ""
    dist_txt = dist_phrase(btc_price, strike)
    bits = _trend_bits(pick, ind_1m, ind_5m)
    entry_n = int(window_trade_count) + 1

    if window_trade_count >= V1_MAX_TRADES_PER_WINDOW:
        reason = f"Already have {window_trade_count} trades in this window — holding to settlement. {dist_txt}."
        if positions:
            side = positions[-1].get("side")
            return Signal("HOLD", f"HOLD {len(positions)} open · cap reached", reason, side=side, bid=bid)
        return Signal("WAIT", "WAIT — Window cap reached", reason)

    kind = "first"
    enter = True
    last_side = None
    if last_entry and last_entry.get("side"):
        last_side = last_entry.get("side")
    elif positions:
        last_side = positions[-1].get("side")

    if window_trade_count > 0 and last_side:
        flipped = pick != last_side and margin >= 0.8
        confirm = False
        if pick == last_side:
            better_ask = False
            if last_entry and last_entry.get("entry_price") is not None and ask is not None:
                better_ask = float(ask) <= float(last_entry["entry_price"]) - 0.01
            stronger = margin >= 1.0 and aligns >= 3
            confirm = better_ask or stronger or aligns >= 4
        if flipped:
            kind, enter = "flip", True
        elif confirm:
            kind, enter = "add", True
        else:
            kind, enter = "watch", False

    if not enter:
        reason = f"Watching this window — {dist_txt}, no confirmation or flip yet."
        if positions:
            side = positions[-1].get("side")
            return Signal("HOLD", f"HOLD {side.upper() if side else 'open'} · watching", reason, side=side, bid=bid)
        return Signal("WAIT", "WAIT — Watching", reason)

    with_txt = ""
    if bits and kind == "first":
        with_txt = " with " + " & ".join(bits[:2])
    if kind == "first":
        reason = f"Picked {pick.upper()} @ {cents_label(ask)}{lean} — {dist_txt}{with_txt}."
    elif kind == "add":
        broke = dist_txt.replace("BTC is ", "BTC broke ")
        reason = f"Entry #{entry_n} in window: {broke}, adding {pick.upper()} @ {cents_label(ask)}."
    else:
        reason = (
            f"Entry #{entry_n} in window: momentum flipped, buying {pick.upper()} @ {cents_label(ask)} — {dist_txt}."
        )
    action = "BUY_YES" if pick == "yes" else "BUY_NO"
    return Signal(
        action,
        f"BUY {pick.upper()} @ {cents_label(ask)}",
        reason,
        side=pick,
        ask=ask,
        bid=bid,
        strength=strength,
        base_stake=stake,
    )


def window_trade_count(trades: list[dict], ticker: Optional[str], open_positions: Optional[list] = None) -> int:
    if not ticker:
        return 0
    n = sum(1 for t in trades if t.get("ticker") == ticker)
    if n == 0 and open_positions:
        n = sum(1 for p in open_positions if p and p.get("ticker") == ticker)
    return n


def last_entry_for(trades: list[dict], ticker: Optional[str], open_positions: Optional[list] = None) -> Optional[dict]:
    if not ticker:
        return None
    for t in reversed(list(trades or [])):
        if t.get("ticker") == ticker:
            return t
    for p in reversed(list(open_positions or [])):
        if p and p.get("ticker") == ticker:
            return p
    return None


def positions_of(book: dict) -> list[dict]:
    opens = [p for p in (book.get("open_positions") or []) if p]
    if opens:
        return list(opens)
    if book.get("position"):
        return [book["position"]]
    return []


def mark_equity_multi(cash: float, positions: list[dict], yes_bid: Optional[float], no_bid: Optional[float]) -> float:
    eq = float(cash)
    for pos in positions:
        bid = yes_bid if pos.get("side") == "yes" else no_bid
        if bid is None:
            eq += float(pos.get("cost") or pos.get("total_in") or 0)
        else:
            eq += int(pos.get("contracts") or 0) * float(bid)
    return round_money(eq)


def crisp_signal_reason(sig: Signal) -> str:
    raw = " ".join(str(sig.reason or "").split())
    if not raw:
        return sig.label
    if len(raw) <= 180 and "EMA 9" not in raw and "MACD histogram" not in raw:
        return raw
    # Prefer the human sentence after the indicator dump.
    parts = [s.strip() for s in raw.split(".") if s.strip()]
    human = [s for s in parts if "EMA 9" not in s and "MACD" not in s and not s.startswith("RSI ")]
    if human:
        line = human[-1] + "."
        return line if len(line) <= 200 else line[:197] + "…"
    return raw[:177] + "…" if len(raw) > 180 else raw


def new_race_book(book_id: str, now: datetime, strategy_version: Optional[int] = None) -> dict:
    if book_id == "v1":
        name, short, badge = "Active Multi-Trade", "Version 1", "MULTI-TRADE · UP TO 3 PER WINDOW"
    else:
        ver = int(strategy_version or 2)
        name, short, badge = "Strict Rules", "Version 2", f"STRICT RULES · v{ver}"
    return {
        "id": book_id,
        "name": name,
        "short_name": short,
        "badge": badge,
        "starting_balance": RACE_STARTING,
        "cash": RACE_STARTING,
        "stake": STAKE_NORMAL,
        "press_next": False,
        "last_win_stake": None,
        "position": None,
        "open_positions": [],
        "trades": [],
        "checks": [],
        "equity": RACE_STARTING,
        "pnl": 0.0,
        "pnl_pct": 0.0,
        "amount_bought": 0.0,
        "contracts_total": 0,
        "wins": 0,
        "losses": 0,
        "trade_count": 0,
        "win_rate": None,
        "next_stake_label": "Next stake: $4–$20",
        "created_at": iso_utc(now),
        "events": [],
    }


def new_race(now: datetime, strategy_version: Optional[int] = None) -> dict:
    return {
        "starting_balance": RACE_STARTING,
        "start": iso_utc(now),
        "start_pt": fmt_pt(now),
        "v1": new_race_book("v1", now),
        "v2": new_race_book("v2", now, strategy_version),
    }


def ensure_race(state: dict, now: datetime, strategy_version: Optional[int] = None) -> dict:
    """Upgrade existing state in place. Never resets the $100 book or the 1-hour session."""
    race = state.get("race") if isinstance(state, dict) else None
    if isinstance(race, dict) and isinstance(race.get("v1"), dict) and isinstance(race.get("v2"), dict):
        lift_race(race)
        for key in ("v1", "v2"):
            book = race[key]
            ensure_press_fields(book)
            if "open_positions" not in book:
                book["open_positions"] = [book["position"]] if book.get("position") else []
            book.setdefault("checks", [])
            book.setdefault("id", key)
        return race
    race = new_race(now, strategy_version)
    print(
        f"  Starting Head-to-Head $100 Race at {race['start_pt']}: "
        "Version 1 Active Multi-Trade vs Version 2 Strict Rules",
        flush=True,
    )
    return race


def settle_open_positions(
    cash: float,
    positions: list[dict],
    trades: list[dict],
    events: list[str],
    settled: list[dict],
    extra_markets: list,
    now: datetime,
) -> tuple[float, list[dict], list[dict], list[str], list[dict], bool, Optional[float]]:
    press_next = False
    last_win_stake: Optional[float] = None
    # press is applied by the caller from the returned closed trades; we just close here.
    still: list[dict] = []
    closed: list[dict] = []
    for position in positions:
        pos_m = find_market(position["ticker"], settled, extra_markets)
        if pos_m is None:
            try:
                data = http_get_json(f"{KALSHI_API}/markets/{urllib.parse.quote(position['ticker'])}")
                pos_m = data.get("market") or data
            except Exception as exc:
                events.append(f"Could not refresh open market {position['ticker']}: {exc}")
                still.append(position)
                continue
        if pos_m and (pos_m.get("result") in ("yes", "no") or pos_m.get("status") in ("finalized", "settled", "determined")):
            if pos_m.get("result") in ("yes", "no"):
                cash, trade = close_trade(
                    cash,
                    position,
                    exit_price=None,
                    exit_fee=0.0,
                    result=pos_m["result"],
                    outcome="settlement",
                    reason=(
                        f"Settled {pos_m['result'].upper()} at BTC "
                        f"{dollars(pos_m.get('expiration_value')) or 'n/a'} vs strike {position.get('strike')}."
                    ),
                    now=parse_iso(pos_m.get("settlement_ts") or now),
                    expiration_value=dollars(pos_m.get("expiration_value")),
                )
                upsert_closed_trade(trades, trade)
                closed.append(trade)
                events.append(
                    f"Settled {position['side'].upper()} on {position['ticker']}: {trade['reason_exit']} PnL {trade['pnl']:+.2f}"
                )
                continue
        still.append(position)
    return cash, still, trades, events, closed, press_next, last_win_stake


def append_race_check(
    book: dict,
    sig: Signal,
    market: Optional[dict],
    now: datetime,
    btc_price: float,
    *,
    book_id: str,
) -> None:
    mp = market_payload(market, now) if market else None
    ticker = (mp or {}).get("ticker") or ((book.get("position") or {}) if False else None)
    ticker = (mp or {}).get("ticker")
    if not ticker:
        opens = positions_of(book)
        ticker = opens[-1]["ticker"] if opens else None
    close_time = (mp or {}).get("close_time")
    if not close_time:
        opens = positions_of(book)
        if opens:
            close_time = opens[-1].get("close_time")
    opens = positions_of(book)
    last_pos = opens[-1] if opens else None
    check = {
        "t": iso_utc(now),
        "t_pt": fmt_pt(now),
        "ticker": ticker,
        "close_time": close_time,
        "close_time_pt": (mp or {}).get("close_time_pt") or (fmt_pt(close_time) if close_time else None),
        "window_label": contract_window_label(close_time) if close_time else None,
        "action": sig.action,
        "label": sig.label,
        "reason": crisp_signal_reason(sig),
        "reason_full": sig.reason,
        "btc_price": round_px(btc_price, 2),
        "strike": (mp or {}).get("strike") or (last_pos or {}).get("strike"),
        "yes_ask": (mp or {}).get("yes_ask"),
        "no_ask": (mp or {}).get("no_ask"),
        "side": sig.side or (last_pos or {}).get("side"),
        "ask": sig.ask,
        "contracts": (last_pos or {}).get("contracts") if sig.action in ("HOLD", "BUY_YES", "BUY_NO") else None,
        "entry_price": (last_pos or {}).get("entry_price") if last_pos else None,
        "cash": round_money(book.get("cash") or 0),
        "book_id": book_id,
        "book_label": book.get("short_name") or book_id,
        "open_count": len(opens),
    }
    checks = list(book.get("checks") or [])
    checks.append(check)
    book["checks"] = checks[-80:]


def finalize_race_book(book: dict, yes_bid: Optional[float], no_bid: Optional[float], now: datetime, strategy_version: Optional[int] = None) -> dict:
    ensure_press_fields(book)
    opens = [decorate_position(p, yes_bid, no_bid) for p in positions_of(book)]
    opens = [p for p in opens if p]
    trades = [decorate_trade(t, yes_bid, no_bid) for t in (book.get("trades") or [])]
    book_id = book.get("id")
    book_label = book.get("short_name") or book_id
    for row in trades:
        if book_id:
            row.setdefault("book_id", book_id)
            row["book_id"] = book_id
            row["book_label"] = book_label
    for pos in opens:
        if book_id:
            pos["book_id"] = book_id
            pos["book_label"] = book_label
    cash = round_money(float(book.get("cash") or 0))
    start = float(book.get("starting_balance") or RACE_STARTING)
    equity = mark_equity_multi(cash, opens, yes_bid, no_bid)
    amount, contracts = book_totals(trades, None)
    # Include any open tickets not yet mirrored into trades.
    extra_amt, extra_ct = book_totals([], None)
    if opens:
        open_amt, open_ct = book_totals(opens, None)
        # book_totals on trades already counts open logs; use max to be safe
        if amount <= 0 and open_amt:
            amount, contracts = open_amt, open_ct
        elif contracts == 0 and open_ct:
            contracts = open_ct
    stats = trade_stats(trades)
    out = dict(book)
    if out.get("id") == "v2" and strategy_version:
        out["badge"] = f"STRICT RULES · v{int(strategy_version)}"
        out["name"] = "Strict Rules"
        out["short_name"] = "Version 2"
    out["cash"] = cash
    out["open_positions"] = opens
    out["position"] = opens[-1] if opens else None
    out["trades"] = trades
    out["amount_bought"] = amount
    out["contracts_total"] = contracts
    out["equity"] = equity
    out["pnl"] = round_money(equity - start)
    out["pnl_pct"] = round(((equity - start) / start) * 100.0, 2) if start else 0.0
    out["trade_count"] = stats["trade_count"]
    out["wins"] = stats["wins"]
    out["losses"] = stats["losses"]
    out["win_rate"] = stats["win_rate"]
    out["net_pnl"] = out["pnl"]
    out["next_stake_label"] = next_stake_label(bool(book.get("press_next")), book.get("last_win_stake"), cash)
    out["updated_at"] = iso_utc(now)
    out["updated_at_pt"] = fmt_pt(now)
    return out


def step_v1_book(
    book: dict,
    market: Optional[dict],
    settled: list[dict],
    ind_1m: Optional[Indicators],
    ind_5m: Optional[Indicators],
    btc_price: float,
    now: datetime,
) -> tuple[dict, Signal]:
    """Version 1: hold all tickets to settlement; at most one new entry per 5-minute check."""
    ensure_press_fields(book)
    events: list[str] = list(book.get("events") or [])
    cash = float(book.get("cash") or RACE_STARTING)
    trades = list(book.get("trades") or [])
    press_next = bool(book.get("press_next"))
    last_win_stake = book.get("last_win_stake")
    positions = positions_of(book)

    cash, positions, trades, events, closed, _, _ = settle_open_positions(
        cash, positions, trades, events, settled, [market] if market else [], now,
    )
    for trade in closed:
        press_next, last_win_stake = apply_press_from_trade(press_next, last_win_stake, trade)

    mp = market_payload(market, now) if market else None
    yes_ask = mp["yes_ask"] if mp else None
    yes_bid = mp["yes_bid"] if mp else None
    no_ask = mp["no_ask"] if mp else None
    no_bid = mp["no_bid"] if mp else None
    strike = mp["strike"] if mp else None
    remaining = mp["remaining_sec"] if mp else None
    ticker = mp["ticker"] if mp else None
    count = window_trade_count(trades, ticker, positions)
    last = last_entry_for(trades, ticker, positions)

    sig = make_v1_signal(
        ind_1m=ind_1m,
        ind_5m=ind_5m,
        btc_price=btc_price,
        strike=strike,
        yes_ask=yes_ask,
        yes_bid=yes_bid,
        no_ask=no_ask,
        no_bid=no_bid,
        remaining_sec=remaining,
        open_positions=positions,
        window_trade_count=count,
        last_entry=last,
        ticker=ticker,
    )

    if market and sig.action in ("BUY_YES", "BUY_NO") and sig.ask is not None and count < V1_MAX_TRADES_PER_WINDOW:
        base = float(sig.base_stake or STAKE_NORMAL)
        entry_stake, pressed = resolve_entry_stake(base, press_next, last_win_stake, cash)
        opened = open_trade(
            cash, market, sig.side or "yes", float(sig.ask), sig.reason, now,
            stake=entry_stake, pressed=pressed, strength=sig.strength,
        )
        if opened:
            cash, position, log = opened
            log["window_entry"] = count + 1
            position["window_entry"] = count + 1
            trades.append(log)
            positions.append(position)
            press_tag = " 2× PRESS" if pressed else ""
            events.append(
                f"Opened {position['side'].upper()} {position['contracts']}x {position['ticker']} @ {cents_label(position['entry_price'])} "
                f"(bought ${position['total_in']:.2f}, ${position['stake']:.0f} ticket{press_tag}, entry #{count + 1} in window)"
            )
            sig = Signal(
                "HOLD",
                f"BOUGHT {position['side'].upper()} @ {cents_label(position['entry_price'])}",
                sig.reason,
                side=position["side"],
                ask=sig.ask,
                bid=yes_bid if position["side"] == "yes" else no_bid,
                strength=sig.strength,
                base_stake=sig.base_stake,
            )
        else:
            events.append("Version 1 wanted a ticket but there wasn’t enough paper cash after fees.")
            if positions:
                sig = Signal("HOLD", "HOLD — not enough cash", sig.reason + " Not enough paper cash to add another ticket.", side=positions[-1].get("side"))
            else:
                sig = Signal("WAIT", "WAIT — No Trade", sig.reason + " Not enough paper cash to size the ticket after fees.")

    book["cash"] = round_money(cash)
    book["open_positions"] = positions
    book["position"] = positions[-1] if positions else None
    book["trades"] = trades
    book["press_next"] = bool(press_next)
    book["last_win_stake"] = last_win_stake
    book["events"] = events[-20:]
    append_race_check(book, sig, market, now, btc_price, book_id="v1")
    return finalize_race_book(book, yes_bid, no_bid, now), sig


def step_v2_book(
    book: dict,
    market: Optional[dict],
    settled: list[dict],
    ind_1m: Optional[Indicators],
    ind_5m: Optional[Indicators],
    btc_price: float,
    now: datetime,
    params: Optional[StrategyParams],
    features: Optional[dict],
    strategy_version: Optional[int] = None,
) -> tuple[dict, Signal]:
    """Version 2: strict learned rules, max one trade per 15-minute window."""
    ensure_press_fields(book)
    events: list[str] = []
    cash, position, trades, events, sig, yes_bid, no_bid, press_next, last_win_stake = step_paper_book(
        float(book.get("cash") or RACE_STARTING),
        book.get("position") or (positions_of(book)[-1] if positions_of(book) else None),
        list(book.get("trades") or []),
        events,
        market,
        settled,
        ind_1m,
        ind_5m,
        btc_price,
        now,
        allow_new_entries=True,
        stake=float(book.get("stake") or STAKE_NORMAL),
        press_next=bool(book.get("press_next")),
        last_win_stake=book.get("last_win_stake"),
        params=params,
        features=features,
        max_trades_per_window=1,
    )
    book["cash"] = round_money(cash)
    book["position"] = position
    book["open_positions"] = [position] if position else []
    book["trades"] = trades
    book["press_next"] = bool(press_next)
    book["last_win_stake"] = last_win_stake
    book["events"] = events[-20:]
    # Keep the check reason to one crisp line for the split-screen feed.
    sig = Signal(
        sig.action,
        sig.label,
        crisp_signal_reason(sig),
        side=sig.side,
        ask=sig.ask,
        bid=sig.bid,
        strength=sig.strength,
        base_stake=sig.base_stake,
    )
    append_race_check(book, sig, market, now, btc_price, book_id="v2")
    return finalize_race_book(book, yes_bid, no_bid, now, strategy_version=strategy_version), sig


def leader_payload(v1: dict, v2: dict) -> dict:
    e1 = float(v1.get("equity") or v1.get("cash") or RACE_STARTING)
    e2 = float(v2.get("equity") or v2.get("cash") or RACE_STARTING)
    margin = round_money(e1 - e2)
    if abs(margin) < 0.005:
        return {"leader": "tied", "label": "Tied", "margin": 0.0, "text": "Tied"}
    if margin > 0:
        return {
            "leader": "v1",
            "label": "Version 1 ahead",
            "margin": margin,
            "text": f"Version 1 ahead by +${margin:.2f}",
        }
    return {
        "leader": "v2",
        "label": "Version 2 ahead",
        "margin": round_money(-margin),
        "text": f"Version 2 ahead by +${-margin:.2f}",
    }


def run_race(
    state: dict,
    market: Optional[dict],
    settled: list[dict],
    ind_1m: Optional[Indicators],
    ind_5m: Optional[Indicators],
    btc_price: float,
    now: datetime,
    params: Optional[StrategyParams] = None,
    features: Optional[dict] = None,
    strategy_version: Optional[int] = None,
) -> tuple[dict, Signal, Signal]:
    race = ensure_race(state, now, strategy_version)
    v1, sig1 = step_v1_book(dict(race["v1"]), market, settled, ind_1m, ind_5m, btc_price, now)
    v2, sig2 = step_v2_book(
        dict(race["v2"]), market, settled, ind_1m, ind_5m, btc_price, now,
        params=params, features=features, strategy_version=strategy_version,
    )
    lead = leader_payload(v1, v2)
    out = {
        "starting_balance": RACE_STARTING,
        "start": race.get("start") or iso_utc(now),
        "start_pt": race.get("start_pt") or fmt_pt(now),
        "v1": v1,
        "v2": v2,
        "leader": lead["leader"],
        "leader_label": lead["label"],
        "leader_margin": lead["margin"],
        "leader_text": lead["text"],
        "choice": {
            "v1": {
                "action": sig1.action,
                "label": sig1.label,
                "reason": sig1.reason,
                "side": sig1.side,
                "ask": sig1.ask,
                "strength": sig1.strength,
                "base_stake": sig1.base_stake,
            },
            "v2": {
                "action": sig2.action,
                "label": sig2.label,
                "reason": sig2.reason,
                "side": sig2.side,
                "ask": sig2.ask,
                "strength": sig2.strength,
                "base_stake": sig2.base_stake,
            },
        },
        "updated_at": iso_utc(now),
        "updated_at_pt": fmt_pt(now),
    }
    return out, sig1, sig2


def rules_summary_from_params(p: StrategyParams) -> dict:
    yes = (
        f"1m EMA 9 > 21, {'close > EMA 50, ' if p.require_ema50_align else ''}"
        f"MACD hist > 0, RSI {p.rsi_bull_min:.0f}–{p.rsi_bull_max:.0f}"
        f"{', 5m trend agrees' if p.require_5m_align else ''}, "
        f"BTC at least ${p.min_strike_gap_usd:.0f} above the strike, "
        f"Yes ask {p.min_entry_cents}¢–{p.max_entry_cents}¢, and {p.min_mins_left}–{p.max_mins_left} minutes left."
    )
    no = (
        f"The bearish mirror (EMA 9 < 21, "
        f"{'close < EMA 50, ' if p.require_ema50_align else ''}"
        f"MACD hist < 0, RSI {p.rsi_bear_min:.0f}–{p.rsi_bear_max:.0f}"
        f"{', 5m trend agrees' if p.require_5m_align else ''}, "
        f"BTC at least ${p.min_strike_gap_usd:.0f} below the strike, "
        f"No ask {p.min_entry_cents}¢–{p.max_entry_cents}¢)."
    )
    return {
        "buy_yes": yes,
        "buy_no": no,
        "exits": (
            "Sell early if the bid reaches 80¢ or +15¢ from entry (take profit), drops 12¢ from entry, "
            "or momentum and BTC-vs-strike both flip against the position. Otherwise hold to official settlement."
        ),
        "sizing": (
            "Normal qualifying signal $4, strong $6, top-conviction $8. After a win, the next trade is 2× that "
            "winning stake, hard-capped at $20 and never more than cash on hand. A $4 win presses to $8, a $6 win to $12, "
            "an $8 win to $16, and further wins cap at $20. A loss resets to $4–$8. Every ticket’s total cost stays in the $4–$20 range. "
            "The $100 race books, the archived 1-hour $40 run, and the ongoing $100 book each track this separately."
        ),
        "learning": (
            "Every hour a GitHub Action studies the last 7–14 days of settled 15-minute windows, walk-forward "
            "tests new parameter sets on unseen hours, and only promotes a change when validation P&L is clearly "
            "better. That run is free — it does not use Grok Bot."
        ),
        "race": (
            "A live Head-to-Head $100 Race compares two books that start together at $100. "
            "Version 1 (Active Multi-Trade) can take up to 3 tickets in the same 15-minute window — one new entry "
            "per 5-minute check — adding to its side on confirmation or flipping if momentum turns. "
            "Version 2 (Strict Rules) follows the learned strategy config and takes at most one trade per window, "
            "only when every trend, strike-gap, and price filter passes. Both use $4–$20 sizing and a 2× press, hard-capped at $20."
        ),
    }


def load_existing_dashboard() -> dict:
    if not DASHBOARD_PATH.exists():
        return {}
    try:
        with DASHBOARD_PATH.open() as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    now = utc_now()
    strategy_cfg = load_strategy_config()
    if not STRATEGY_CONFIG_PATH.exists():
        save_json(STRATEGY_CONFIG_PATH, strategy_cfg)
        print(f"  Wrote default strategy config v{strategy_cfg.get('version', 1)}", flush=True)
    params = params_from_dict(strategy_cfg.get("params"))
    print(f"[{fmt_pt(now)}] Fetching Kalshi {SERIES} + BTC candles… (strategy v{strategy_cfg.get('version', 1)})", flush=True)

    open_markets = fetch_kalshi_markets("open", limit=50, max_pages=1)
    # Also grab recently closed / settled for grading + backtest.
    settled = fetch_kalshi_markets("settled", limit=200, max_pages=8)
    print(f"  Open/active markets: {len(open_markets)}; settled fetched: {len(settled)}", flush=True)

    # Recent window start for candle download (backtest days + warmup).
    if settled:
        oldest = min(to_ts(m["open_time"]) for m in settled if m.get("open_time"))
    else:
        oldest = int((now - timedelta(days=BACKTEST_DAYS)).timestamp())
    candle_start = oldest - BACKTEST_CANDLE_PAD_HOURS * 3600
    candle_end = int(now.timestamp()) + 60

    candles_1m, src_1m = fetch_btc_candles(candle_start, candle_end, 60)
    candles_5m, src_5m = fetch_btc_candles(candle_start, candle_end, 300)
    series_1m = compute_indicators(candles_1m)
    series_5m = compute_indicators(candles_5m)
    btc_price, price_src = fetch_live_price()
    print(
        f"  1m candles: {len(candles_1m)} ({src_1m}); 5m candles: {len(candles_5m)} ({src_5m}); "
        f"live BTC ${btc_price:,.2f} ({price_src})",
        flush=True,
    )

    # Historical Kalshi quotes at the 1-minute mark for an honest backtest.
    # Limit to the most recent BACKTEST_DAYS of settled windows to stay inside rate limits.
    cutoff = int((now - timedelta(days=BACKTEST_DAYS)).timestamp())
    settled_window = [m for m in settled if m.get("open_time") and to_ts(m["open_time"]) >= cutoff]
    print(f"  Backtest windows in last {BACKTEST_DAYS} days: {len(settled_window)}. Fetching 1-minute Kalshi quotes…", flush=True)
    sticks = fetch_candlesticks_for_markets(settled_window, first_minutes=3)
    print(f"  Candlesticks for {sum(1 for v in sticks.values() if v)} / {len(settled_window)} markets", flush=True)

    backtest = run_backtest(settled_window, sticks, candles_1m, series_1m, candles_5m, series_5m, params=params)
    backtest["generated_at"] = iso_utc(now)
    backtest["generated_at_pt"] = fmt_pt(now)
    backtest["candle_source_1m"] = src_1m
    backtest["candle_source_5m"] = src_5m
    save_json(BACKTEST_PATH, backtest)
    wr = backtest.get("win_rate")
    print(
        f"  Backtest {backtest.get('start_pt')} → {backtest.get('end_pt')}: "
        f"{backtest['windows_traded']} trades / {backtest['windows_scanned']} windows, "
        f"win rate {wr if wr is not None else '—'}%, "
        f"PnL ${backtest['net_pnl']:+.2f} ({backtest['return_pct']:+.2f}%), "
        f"max DD {backtest['max_drawdown_pct']:.2f}%",
        flush=True,
    )

    market = pick_live_market(open_markets, settled, now)
    last_closed_1 = None
    last_closed_5 = None
    now_ts = int(now.timestamp())
    # Use last fully closed 1-min / 5-min bars (not the in-progress one).
    snap1 = indicators_at(candles_1m, series_1m, now_ts - 60)
    snap5 = indicators_at(candles_5m, series_5m, now_ts - 300)
    ind_1m = snap1[1] if snap1 else None
    ind_5m = snap5[1] if snap5 else None
    if snap1:
        last_closed_1 = snap1[0]
    if snap5:
        last_closed_5 = snap5[0]

    state = load_state()
    # If an older hourly-spot state is still on disk, start a fresh $100 Kalshi book.
    if float(state.get("starting_balance") or 0) != STARTING_BALANCE or "stake" not in state:
        print("  Starting a fresh $100 Kalshi paper book (previous state was a different bot).", flush=True)
        state = default_state()
    else:
        ensure_press_fields(state)

    vol, chop = window_chop_features(candles_1m, now_ts - 60)
    features = {"volatility_usd": vol, "chop_ratio": chop}
    new_state, sig, events = run_live(state, market, settled, ind_1m, ind_5m, btc_price, now, params=params, features=features)
    session = run_session(state, market, settled, ind_1m, ind_5m, btc_price, now, params=params, features=features)
    race, sig_v1, sig_v2 = run_race(
        state, market, settled, ind_1m, ind_5m, btc_price, now,
        params=params, features=features, strategy_version=strategy_cfg.get("version", 2),
    )
    new_state["session"] = session
    new_state["race"] = race
    save_json(STATE_PATH, new_state)

    mp = market_payload(market, now)
    yes_bid = mp["yes_bid"] if mp else None
    no_bid = mp["no_bid"] if mp else None
    new_state["position"] = decorate_position(new_state.get("position"), yes_bid, no_bid)
    live_trades = [decorate_trade(t, yes_bid, no_bid) for t in (new_state.get("trades") or [])]
    new_state["trades"] = live_trades
    stats = trade_stats(live_trades)
    start = float(new_state["starting_balance"])
    equity = mark_equity(
        new_state["cash"],
        new_state["position"],
        yes_bid,
        no_bid,
    )
    pnl = round_money(equity - start)
    book_bought, book_contracts = book_totals(live_trades, new_state.get("position"))
    strike = mp["strike"] if mp else None
    dist = (btc_price - strike) if strike is not None else None

    dashboard = {
        "generated_at": iso_utc(now),
        "generated_at_pt": fmt_pt(now),
        "series_ticker": SERIES,
        "price_source": price_src,
        "candle_source_1m": src_1m,
        "candle_source_5m": src_5m,
        "polling": {
            "coinbase_cors": True,
            "kalshi_cors": False,
            "note": (
                "The dashboard polls Coinbase’s public API every 20 seconds for a live BTC price and 1-minute "
                "indicators (CORS is open). Kalshi’s public API blocks browser CORS, so contract quotes, the "
                "official Buy/Sell/Wait call, and paper-trade grading refresh with each GitHub Actions run (every 5 minutes)."
            ),
        },
        "live": {
            "btc_price": round_px(btc_price, 2),
            "strike": strike,
            "distance": None if dist is None else round_px(dist, 2),
            "call": sig.action,
            "call_label": sig.label,
            "reason": sig.reason,
            "side": sig.side,
            "ask": sig.ask,
            "bid": sig.bid,
            "market": mp,
            "indicators_1m": ind_payload(ind_1m),
            "indicators_5m": ind_payload(ind_5m),
            "last_1m_close": last_closed_1["close"] if last_closed_1 else None,
            "last_1m_time_pt": fmt_pt(last_closed_1["time"]) if last_closed_1 else None,
            "cash": new_state["cash"],
            "equity": equity,
            "pnl": pnl,
            "pnl_pct": round((pnl / start) * 100.0, 2) if start else 0.0,
            "starting_balance": start,
            "stake": STAKE,
            "press_next": bool(new_state.get("press_next")),
            "last_win_stake": new_state.get("last_win_stake"),
            "next_stake_label": next_stake_label(bool(new_state.get("press_next")), new_state.get("last_win_stake"), new_state["cash"]),
            "amount_bought": book_bought,
            "contracts_total": book_contracts,
            "position": new_state["position"],
            "equity_history": new_state["equity_history"],
            "trades": list(reversed(new_state["trades"] or [])),
            **stats,
            "events": events,
        },
        "session": session,
        "race": race,
        "backtest": backtest,
        "strategy": {
            "version": strategy_cfg.get("version", 1),
            "updated_at": strategy_cfg.get("updated_at"),
            "updated_at_pt": strategy_cfg.get("updated_at_pt"),
            "source": strategy_cfg.get("source"),
            "params": params.as_dict(),
            "plain_english": strategy_cfg.get("plain_english") or plain_english_params(params),
            "metrics": strategy_cfg.get("metrics") or {},
        },
        "learning": load_existing_dashboard().get("learning"),
        "fee_note": "Every paper trade uses Kalshi’s taker fee, ceil(0.07 × contracts × price × (1 − price)), rounded up to the next cent. The Head-to-Head $100 Race starts two books at $100 together (Version 1 Active Multi-Trade vs Version 2 Strict Rules). Existing $40 race books are lifted to $100 by adding the cash delta, so prior trades and P&L stay accurate. The completed 1-hour $40 run and the ongoing $100 book stay in the Trades tab. All books use a $4–$8 base stake by signal strength, then 2× after a win, hard-capped at $20. Every ticket costs $4–$20 (including fees).",
        "rules_summary": rules_summary_from_params(params),
    }
    save_json(DASHBOARD_PATH, dashboard)

    print(f"  Contract: {mp['ticker'] if mp else 'none'} | strike {strike} | BTC ${btc_price:,.2f}", flush=True)
    print(f"  CALL: {sig.label}", flush=True)
    print(f"  {sig.reason}", flush=True)
    print(f"  $100 book equity ${equity:,.2f} (cash ${new_state['cash']:,.2f}) | {stats['wins']}W-{stats['losses']}L", flush=True)
    print(
        f"  $40 session: {session['status_label']} | {session['window_label']} | "
        f"equity ${session['equity']:,.2f} | {session.get('wins', 0)}W-{session.get('losses', 0)}L | {session.get('events') or ['no new session trades']}",
        flush=True,
    )
    print(
        f"  RACE {race.get('leader_text')}: "
        f"V1 {sig_v1.label} · equity ${race['v1']['equity']:,.2f} · {race['v1'].get('wins', 0)}W-{race['v1'].get('losses', 0)}L | "
        f"V2 {sig_v2.label} · equity ${race['v2']['equity']:,.2f} · {race['v2'].get('wins', 0)}W-{race['v2'].get('losses', 0)}L",
        flush=True,
    )
    print(f"  V1: {sig_v1.reason}", flush=True)
    print(f"  V2: {sig_v2.reason}", flush=True)
    for ev in events:
        print(f"  EVENT {ev}", flush=True)
    for ev in session.get("events") or []:
        print(f"  SESSION {ev}", flush=True)
    print(f"  Wrote {STATE_PATH.relative_to(ROOT)}, {BACKTEST_PATH.relative_to(ROOT)}, {DASHBOARD_PATH.relative_to(ROOT)}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
