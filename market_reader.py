#!/usr/bin/env python3
"""Multi-timeframe BTC chart reading and live market-reader snapshots.

Public data only (Coinbase Exchange, Kraken, Kalshi). No API keys.
Designed so a missing feed never blocks the paper bot: every fetch has a
graceful fallback, and historical features that can be rebuilt from candles
(fair probability, chart bias) are separated from live-only book/flow.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

USER_AGENT = "btc-paper-bot/1.2 (+https://github.com/nfm0984-png/btc-paper-bot)"
HTTP_TIMEOUT = 15
MAX_RETRIES = 3

COINBASE = "https://api.exchange.coinbase.com"
KRAKEN = "https://api.kraken.com/0/public"
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

# Top-of-book / depth sizes kept small so GitHub Actions never pulls the
# full Coinbase level-2 book (~1MB).
BOOK_LEVELS = 10
TRADES_LIMIT = 100


def http_get_json(url: str, timeout: int = HTTP_TIMEOUT) -> Any:
    last_err: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            last_err = exc
            time.sleep(0.3 * attempt)
    raise RuntimeError(f"GET failed after {MAX_RETRIES} tries: {url} ({last_err})")


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Math / indicators
# ---------------------------------------------------------------------------

def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def realized_vol_per_sqrt_min(candles: list[dict], ts: int, lookback: int = 30) -> Optional[float]:
    """Std dev of 1-minute close-to-close dollar moves, ending at ts.

    Multiply by sqrt(minutes_left) to get a dollar sigma for the remaining window.
    """
    bars = [c for c in candles if int(c["time"]) <= int(ts)]
    if len(bars) < max(8, lookback // 2):
        return None
    bars = bars[-lookback:]
    diffs = [float(bars[i]["close"]) - float(bars[i - 1]["close"]) for i in range(1, len(bars))]
    if len(diffs) < 6:
        return None
    mean = sum(diffs) / len(diffs)
    var = sum((d - mean) ** 2 for d in diffs) / len(diffs)
    return math.sqrt(var) if var > 0 else 0.0


def fair_yes_probability(
    spot: Optional[float],
    strike: Optional[float],
    sigma_per_sqrt_min: Optional[float],
    minutes_left: Optional[float],
    min_sigma: float = 8.0,
) -> Optional[float]:
    """P(BTC finishes above the strike) under a no-drift normal model.

    sigma_per_sqrt_min is the realized dollar vol of 1-minute returns.
    A floor keeps the probability from collapsing to 0/1 when the tape is quiet.
    """
    if spot is None or strike is None:
        return None
    minutes = max(float(minutes_left or 0.0), 0.25)
    sigma_1m = float(sigma_per_sqrt_min or 0.0)
    sigma = max(sigma_1m, min_sigma) * math.sqrt(minutes)
    if sigma <= 1e-9:
        return 1.0 if float(spot) >= float(strike) else 0.0
    z = (float(spot) - float(strike)) / sigma
    p = normal_cdf(z)
    return max(0.01, min(0.99, p))


def rolling_vwap(candles: list[dict], end_idx: int, lookback: int) -> Optional[float]:
    start = max(0, end_idx - lookback + 1)
    window = candles[start : end_idx + 1]
    num = 0.0
    den = 0.0
    for c in window:
        vol = float(c.get("volume") or 0.0)
        typ = (float(c["high"]) + float(c["low"]) + float(c["close"])) / 3.0
        if vol <= 0:
            vol = 1.0
        num += typ * vol
        den += vol
    if den <= 0:
        return None
    return num / den


def wilder_atr(candles: list[dict], period: int = 14) -> list[Optional[float]]:
    n = len(candles)
    out: list[Optional[float]] = [None] * n
    if n < period + 1:
        return out
    trs: list[float] = [0.0] * n
    for i in range(1, n):
        high = float(candles[i]["high"])
        low = float(candles[i]["low"])
        prev = float(candles[i - 1]["close"])
        trs[i] = max(high - low, abs(high - prev), abs(low - prev))
    first = sum(trs[1 : period + 1]) / period
    out[period] = first
    atr = first
    for i in range(period + 1, n):
        atr = (atr * (period - 1) + trs[i]) / period
        out[i] = atr
    return out


def ema_slope_at(values: list[Optional[float]], idx: int, bars: int = 3) -> Optional[float]:
    if idx < bars or values[idx] is None or values[idx - bars] is None:
        return None
    return float(values[idx]) - float(values[idx - bars])


def momentum_at(candles: list[dict], idx: int, bars: int = 3) -> Optional[float]:
    if idx < bars:
        return None
    return float(candles[idx]["close"]) - float(candles[idx - bars]["close"])


def enrich_indicator_series(candles: list[dict], series: list, *, vwap_lookback: int = 30) -> list:
    """Fill optional ATR / VWAP / slope / momentum fields on an Indicators series."""
    atrs = wilder_atr(candles, 14)
    ema9s = [ind.ema9 for ind in series]
    for i, ind in enumerate(series):
        ind.atr = None if atrs[i] is None else round(float(atrs[i]), 2)
        vwap = rolling_vwap(candles, i, vwap_lookback)
        ind.vwap = None if vwap is None else round(float(vwap), 2)
        if ind.vwap is not None and ind.close is not None:
            ind.vwap_dist = round(float(ind.close) - float(ind.vwap), 2)
        else:
            ind.vwap_dist = None
        slope = ema_slope_at(ema9s, i, 3)
        ind.ema_slope = None if slope is None else round(float(slope), 2)
        mom = momentum_at(candles, i, 3)
        ind.momentum = None if mom is None else round(float(mom), 2)
    return series


def chart_bias(ind, flat_dollars: float = 8.0) -> str:
    """up / down / flat from EMA 9 vs 21, with a flat band and MACD as a tie-break."""
    if not ind or not getattr(ind, "ready", False):
        return "flat"
    ema9 = float(ind.ema9 or 0)
    ema21 = float(ind.ema21 or 0)
    spread = ema9 - ema21
    if abs(spread) < float(flat_dollars):
        hist = ind.macd_hist
        if hist is not None and abs(float(hist)) >= 4:
            return "up" if float(hist) > 0 else "down"
        return "flat"
    return "up" if spread > 0 else "down"


def chart_snapshot(tf: str, ind, extra: Optional[dict] = None) -> dict:
    bias = chart_bias(ind)
    payload = {
        "tf": tf,
        "bias": bias,
        "ready": bool(ind and getattr(ind, "ready", False)),
        "ema9": None if not ind or ind.ema9 is None else round(float(ind.ema9), 2),
        "ema21": None if not ind or ind.ema21 is None else round(float(ind.ema21), 2),
        "ema50": None if not ind or ind.ema50 is None else round(float(ind.ema50), 2),
        "rsi14": None if not ind or ind.rsi14 is None else round(float(ind.rsi14), 2),
        "macd_hist": None if not ind or ind.macd_hist is None else round(float(ind.macd_hist), 2),
        "ema_slope": None if not ind else getattr(ind, "ema_slope", None),
        "vwap": None if not ind else getattr(ind, "vwap", None),
        "vwap_dist": None if not ind else getattr(ind, "vwap_dist", None),
        "atr": None if not ind else getattr(ind, "atr", None),
        "momentum": None if not ind else getattr(ind, "momentum", None),
        "close": None if not ind or ind.close is None else round(float(ind.close), 2),
    }
    if extra:
        payload.update(extra)
    return payload


def tf_agrees(ind, side: str, flat_dollars: float = 8.0) -> Optional[bool]:
    if not ind or not getattr(ind, "ready", False):
        return None
    bias = chart_bias(ind, flat_dollars)
    if bias == "flat":
        return False
    if side == "yes":
        return bias == "up"
    if side == "no":
        return bias == "down"
    return None


def multi_tf_consensus(
    snaps: dict[str, dict],
    side: Optional[str] = None,
    flat_dollars: float = 8.0,
) -> dict:
    """Combine 1m/5m/15m/1h snapshots into a single regime reading."""
    order = ["1m", "5m", "15m", "1h"]
    ready = []
    up = []
    down = []
    for tf in order:
        snap = snaps.get(tf) or {}
        if not snap.get("ready"):
            continue
        ready.append(tf)
        if snap.get("bias") == "up":
            up.append(tf)
        elif snap.get("bias") == "down":
            down.append(tf)
    higher = [tf for tf in ("5m", "15m", "1h") if tf in ready]
    higher_up = [tf for tf in higher if tf in up]
    higher_down = [tf for tf in higher if tf in down]
    if len(up) > len(down) and len(up) >= 2:
        bias = "up"
    elif len(down) > len(up) and len(down) >= 2:
        bias = "down"
    elif len(up) and not down:
        bias = "up"
    elif len(down) and not up:
        bias = "down"
    else:
        bias = "mixed"
    agree_count = 0
    if side == "yes":
        agree_count = len(higher_up)
    elif side == "no":
        agree_count = len(higher_down)
    else:
        agree_count = max(len(higher_up), len(higher_down))
    aligned = higher_up if (side == "yes" or (side is None and len(higher_up) >= len(higher_down))) else higher_down
    if side is None and len(higher_up) == len(higher_down):
        aligned = []
    return {
        "bias": bias,
        "ready": ready,
        "up": up,
        "down": down,
        "aligned": aligned,
        "higher_ready": higher,
        "higher_agree": agree_count,
        "score": len(up) - len(down),
        "label": (
            "Bullish alignment" if bias == "up"
            else "Bearish alignment" if bias == "down"
            else "Mixed / choppy"
        ),
    }


# ---------------------------------------------------------------------------
# Live BTC book + flow
# ---------------------------------------------------------------------------

def _depth_imbalance(bids: list[tuple[float, float]], asks: list[tuple[float, float]], levels: int = BOOK_LEVELS) -> dict:
    b = bids[:levels]
    a = asks[:levels]
    bid_depth = sum(sz for _, sz in b)
    ask_depth = sum(sz for _, sz in a)
    tot = bid_depth + ask_depth
    imb = ((bid_depth - ask_depth) / tot) if tot > 0 else 0.0
    best_bid = b[0][0] if b else None
    best_ask = a[0][0] if a else None
    spread = (best_ask - best_bid) if best_bid is not None and best_ask is not None else None
    mid = ((best_bid + best_ask) / 2.0) if best_bid is not None and best_ask is not None else None
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "mid": mid,
        "bid_depth": round(bid_depth, 6),
        "ask_depth": round(ask_depth, 6),
        "imbalance": round(imb, 4),
        "bias": "buy" if imb >= 0.12 else "sell" if imb <= -0.12 else "neutral",
        "levels": min(len(b), len(a), levels),
    }


def fetch_coinbase_l1() -> dict:
    data = http_get_json(f"{COINBASE}/products/BTC-USD/book?level=1")
    bids = [(float(r[0]), float(r[1])) for r in (data.get("bids") or [])]
    asks = [(float(r[0]), float(r[1])) for r in (data.get("asks") or [])]
    out = _depth_imbalance(bids, asks, levels=1)
    out["source"] = "coinbase"
    return out


def fetch_coinbase_trades(limit: int = TRADES_LIMIT) -> list[dict]:
    data = http_get_json(f"{COINBASE}/products/BTC-USD/trades?limit={int(limit)}")
    if not isinstance(data, list):
        return []
    return data


def fetch_kraken_depth(count: int = 25) -> dict:
    data = http_get_json(f"{KRAKEN}/Depth?pair=XBTUSD&count={int(count)}")
    if data.get("error"):
        raise RuntimeError(f"Kraken depth error: {data['error']}")
    result = data.get("result") or {}
    key = next(k for k in result)
    raw = result[key]
    bids = [(float(r[0]), float(r[1])) for r in (raw.get("bids") or [])]
    asks = [(float(r[0]), float(r[1])) for r in (raw.get("asks") or [])]
    out = _depth_imbalance(bids, asks, levels=BOOK_LEVELS)
    out["source"] = "kraken"
    return out


def fetch_kraken_trades() -> list[list]:
    data = http_get_json(f"{KRAKEN}/Trades?pair=XBTUSD")
    if data.get("error"):
        raise RuntimeError(f"Kraken trades error: {data['error']}")
    result = data.get("result") or {}
    key = next(k for k in result if k != "last")
    return list(result.get(key) or [])


def taker_delta_coinbase(trades: list[dict], last_n: int = 80) -> dict:
    """Coinbase Exchange `side` is the *maker* side, so taker buy = maker sell."""
    rows = list(trades or [])[:last_n]
    buy = 0.0
    sell = 0.0
    for t in rows:
        size = float(t.get("size") or 0)
        side = str(t.get("side") or "").lower()
        if side == "sell":
            buy += size
        elif side == "buy":
            sell += size
    tot = buy + sell
    delta = (buy - sell) / tot if tot > 0 else 0.0
    return {
        "source": "coinbase",
        "trades": len(rows),
        "buy_volume": round(buy, 6),
        "sell_volume": round(sell, 6),
        "delta": round(delta, 4),
        "bias": "buy" if delta >= 0.12 else "sell" if delta <= -0.12 else "neutral",
    }


def taker_delta_kraken(rows: list[list], last_n: int = 80) -> dict:
    """Kraken trade[3] is the aggressor: 'b' = taker buy, 's' = taker sell."""
    use = list(rows or [])[-last_n:]
    buy = 0.0
    sell = 0.0
    for t in use:
        try:
            size = float(t[1])
            side = str(t[3]).lower()
        except (IndexError, TypeError, ValueError):
            continue
        if side == "b":
            buy += size
        elif side == "s":
            sell += size
    tot = buy + sell
    delta = (buy - sell) / tot if tot > 0 else 0.0
    return {
        "source": "kraken",
        "trades": len(use),
        "buy_volume": round(buy, 6),
        "sell_volume": round(sell, 6),
        "delta": round(delta, 4),
        "bias": "buy" if delta >= 0.12 else "sell" if delta <= -0.12 else "neutral",
    }


def fetch_btc_book_and_flow() -> dict:
    """Live BTC order-book + taker-flow snapshot with Coinbase/Kraken fallbacks."""
    l1 = _safe(fetch_coinbase_l1)
    depth = _safe(fetch_kraken_depth)
    if depth is None and l1 is not None:
        depth = dict(l1)
        depth["source"] = l1.get("source") or "coinbase-l1"
    elif depth is None:
        depth = {}
    flow = None
    cb_trades = _safe(fetch_coinbase_trades)
    if cb_trades:
        flow = taker_delta_coinbase(cb_trades)
    if flow is None:
        kr_trades = _safe(fetch_kraken_trades)
        if kr_trades:
            flow = taker_delta_kraken(kr_trades)
    flow = flow or {"source": None, "trades": 0, "buy_volume": 0, "sell_volume": 0, "delta": 0.0, "bias": "neutral"}
    book_bias = depth.get("bias") or "neutral"
    flow_bias = flow.get("bias") or "neutral"
    if book_bias == flow_bias and book_bias in ("buy", "sell"):
        combined = book_bias
    elif book_bias in ("buy", "sell") and flow_bias == "neutral":
        combined = book_bias
    elif flow_bias in ("buy", "sell") and book_bias == "neutral":
        combined = flow_bias
    elif book_bias in ("buy", "sell") and flow_bias in ("buy", "sell") and book_bias != flow_bias:
        combined = "mixed"
    else:
        combined = "neutral"
    spread = depth.get("spread") if depth.get("spread") is not None else (l1 or {}).get("spread")
    return {
        "book": depth,
        "top": l1,
        "flow": flow,
        "bias": combined,
        "spread_usd": None if spread is None else round(float(spread), 2),
        "imbalance": depth.get("imbalance"),
        "ok": bool(depth or l1 or flow.get("trades")),
    }


# ---------------------------------------------------------------------------
# Live Kalshi book
# ---------------------------------------------------------------------------

def fetch_kalshi_orderbook(ticker: str, depth: int = 20) -> dict:
    url = (
        f"{KALSHI_API}/markets/{urllib.parse.quote(ticker)}/orderbook"
        f"?depth={int(depth)}"
    )
    return http_get_json(url)


def kalshi_book_metrics(orderbook: Optional[dict], yes_bid: Optional[float] = None, yes_ask: Optional[float] = None) -> dict:
    fp = (orderbook or {}).get("orderbook_fp") or orderbook or {}
    yes_levels = fp.get("yes_dollars") or fp.get("yes") or []
    no_levels = fp.get("no_dollars") or fp.get("no") or []

    def _pairs(rows):
        out = []
        for row in rows or []:
            try:
                if isinstance(row, (list, tuple)) and len(row) >= 2:
                    out.append((float(row[0]), float(row[1])))
                elif isinstance(row, dict):
                    out.append((float(row.get("price") or row.get("px")), float(row.get("size") or row.get("qty") or 0)))
            except (TypeError, ValueError):
                continue
        return out

    yes = _pairs(yes_levels)
    no = _pairs(no_levels)
    yes_depth = sum(sz for _, sz in yes)
    no_depth = sum(sz for _, sz in no)
    tot = yes_depth + no_depth
    imb = ((yes_depth - no_depth) / tot) if tot > 0 else 0.0
    best_yes_bid = max((px for px, _ in yes), default=yes_bid)
    best_no_bid = max((px for px, _ in no), default=None)
    if yes_bid is None:
        yes_bid = best_yes_bid
    if yes_ask is None and best_no_bid is not None:
        yes_ask = max(0.0, min(1.0, 1.0 - best_no_bid))
    spread = None
    if yes_bid is not None and yes_ask is not None:
        spread = max(0.0, float(yes_ask) - float(yes_bid))
    return {
        "yes_depth": round(yes_depth, 2),
        "no_depth": round(no_depth, 2),
        "imbalance": round(imb, 4),
        "bias": "yes" if imb >= 0.12 else "no" if imb <= -0.12 else "neutral",
        "yes_bid": None if yes_bid is None else round(float(yes_bid), 4),
        "yes_ask": None if yes_ask is None else round(float(yes_ask), 4),
        "spread": None if spread is None else round(float(spread), 4),
        "spread_cents": None if spread is None else round(float(spread) * 100.0, 1),
        "yes_levels": len(yes),
        "no_levels": len(no),
        "ok": bool(yes or no or yes_bid is not None),
    }


def fetch_kalshi_book_metrics(ticker: Optional[str], yes_bid: Optional[float] = None, yes_ask: Optional[float] = None) -> dict:
    if not ticker:
        return kalshi_book_metrics(None, yes_bid, yes_ask)
    try:
        raw = fetch_kalshi_orderbook(ticker, depth=20)
        return kalshi_book_metrics(raw, yes_bid, yes_ask)
    except Exception:
        return kalshi_book_metrics(None, yes_bid, yes_ask)


# ---------------------------------------------------------------------------
# Combined snapshot used by the live bot + dashboard
# ---------------------------------------------------------------------------

def side_edge(fair: Optional[float], ask: Optional[float]) -> Optional[float]:
    if fair is None or ask is None:
        return None
    return round(float(fair) - float(ask), 4)


def build_reader_payload(
    *,
    snaps: dict[str, dict],
    consensus: dict,
    best_chart: Optional[str],
    scorecard: Optional[dict],
    fair_yes: Optional[float],
    yes_ask: Optional[float],
    no_ask: Optional[float],
    yes_bid: Optional[float],
    no_bid: Optional[float],
    btc_flow: Optional[dict],
    kalshi_book: Optional[dict],
    sigma: Optional[float],
    minutes_left: Optional[float],
    buy_rules: str,
    sell_rules: str,
) -> dict:
    fair_no = None if fair_yes is None else round(1.0 - float(fair_yes), 4)
    yes_edge = side_edge(fair_yes, yes_ask)
    no_edge = side_edge(fair_no, no_ask)
    implied_yes = yes_ask
    return {
        "charts": {
            "1m": snaps.get("1m") or chart_snapshot("1m", None),
            "5m": snaps.get("5m") or chart_snapshot("5m", None),
            "15m": snaps.get("15m") or chart_snapshot("15m", None),
            "1h": snaps.get("1h") or chart_snapshot("1h", None),
        },
        "consensus": consensus,
        "best_chart": best_chart,
        "scorecard": scorecard or {},
        "fair_yes": None if fair_yes is None else round(float(fair_yes), 4),
        "fair_no": fair_no,
        "implied_yes": None if implied_yes is None else round(float(implied_yes), 4),
        "yes_edge": yes_edge,
        "no_edge": no_edge,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "sigma_usd_per_sqrt_min": None if sigma is None else round(float(sigma), 2),
        "minutes_left": None if minutes_left is None else round(float(minutes_left), 2),
        "btc_book": (btc_flow or {}).get("book"),
        "btc_flow": (btc_flow or {}).get("flow"),
        "btc_bias": (btc_flow or {}).get("bias") or "neutral",
        "btc_spread_usd": (btc_flow or {}).get("spread_usd"),
        "kalshi_book": kalshi_book or {},
        "buy_rules": buy_rules,
        "sell_rules": sell_rules,
    }
