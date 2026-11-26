#!/usr/bin/env python3
"""BTC/USD paper-trading bot: EMA + momentum trend-following.

Uses only public market data and simulated money. No API keys required.
"""

from __future__ import annotations

import json
import math
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
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

STARTING_BALANCE = 1000.00
FEE_RATE = 0.0010          # 0.10% exchange fee
SLIPPAGE_RATE = 0.0005     # 0.05% slippage
SIDE_COST = FEE_RATE + SLIPPAGE_RATE  # 0.15% per side
POSITION_FRAC = 0.95

FAST_EMA = 12
SLOW_EMA = 26
TREND_EMA = 50
RSI_PERIOD = 14
RSI_MIN = 52.0
RSI_MAX = 72.0
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

STOP_PCT = 0.03
TRAIL_ARM_PCT = 0.02
TRAIL_PCT = 0.025

GRANULARITY = 3600
BACKTEST_DAYS = 90
LIVE_LOOKBACK_HOURS = 400
USER_AGENT = "btc-paper-bot/1.0 (+https://github.com/nfm0984-png/btc-paper-bot)"
HTTP_TIMEOUT = 20
MAX_RETRIES = 3

WARMUP_BARS = TREND_EMA + MACD_SIGNAL + 5  # indicators must be valid


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def ts_to_dt(ts: int | float) -> datetime:
    return datetime.fromtimestamp(int(ts), tz=UTC)


def fmt_pt(ts: int | float | datetime) -> str:
    if isinstance(ts, datetime):
        dt = ts.astimezone(PT) if ts.tzinfo else ts.replace(tzinfo=UTC).astimezone(PT)
    else:
        dt = ts_to_dt(ts).astimezone(PT)
    hour = dt.strftime("%I").lstrip("0") or "0"
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}, {hour}:{dt.strftime('%M %p')} PT"


def iso_utc(ts: int | float | datetime) -> str:
    if isinstance(ts, datetime):
        dt = ts.astimezone(UTC) if ts.tzinfo else ts.replace(tzinfo=UTC)
    else:
        dt = ts_to_dt(ts)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def round_money(x: float) -> float:
    return round(float(x) + 0.0, 2)


def round_px(x: float) -> float:
    return round(float(x) + 0.0, 2)


def round_qty(x: float) -> float:
    return round(float(x) + 0.0, 8)


def round_ind(x: Optional[float], n: int = 2) -> Optional[float]:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(float(x) + 0.0, n)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def http_get_json(url: str) -> Any:
    last_err: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            last_err = exc
            time.sleep(0.4 * attempt)
    raise RuntimeError(f"GET failed after {MAX_RETRIES} tries: {url} ({last_err})")


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

def _norm_candle(ts: int, open_: float, high: float, low: float, close: float, volume: float) -> dict:
    return {
        "time": int(ts),
        "open": float(open_),
        "high": float(high),
        "low": float(low),
        "close": float(close),
        "volume": float(volume),
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
        errors.append(f"coinbase ticker: {exc}")
    try:
        return fetch_kraken_ticker(), "kraken"
    except Exception as exc:
        errors.append(f"kraken ticker: {exc}")
    raise RuntimeError("Could not fetch a live BTC/USD price. " + " | ".join(errors))


def fetch_coinbase_candles(start_ts: int, end_ts: int) -> list[dict]:
    candles: dict[int, dict] = {}
    max_span = GRANULARITY * 300
    cursor = int(start_ts)
    end_ts = int(end_ts)
    while cursor < end_ts:
        chunk_end = min(cursor + max_span, end_ts)
        start_iso = ts_to_dt(cursor).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = ts_to_dt(chunk_end).strftime("%Y-%m-%dT%H:%M:%SZ")
        url = (
            "https://api.exchange.coinbase.com/products/BTC-USD/candles"
            f"?granularity={GRANULARITY}&start={start_iso}&end={end_iso}"
        )
        raw = http_get_json(url)
        if not isinstance(raw, list):
            raise RuntimeError(f"Unexpected Coinbase candles payload: {raw}")
        for row in raw:
            # [time, low, high, open, close, volume]
            ts = int(row[0])
            candles[ts] = _norm_candle(ts, row[3], row[2], row[1], row[4], row[5])
        cursor = chunk_end
        time.sleep(0.15)
    return [candles[k] for k in sorted(candles)]


def fetch_kraken_candles(start_ts: int, end_ts: int) -> list[dict]:
    candles: dict[int, dict] = {}
    since = int(start_ts)
    end_ts = int(end_ts)
    for _ in range(12):
        url = f"https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=60&since={since}"
        data = http_get_json(url)
        if data.get("error"):
            raise RuntimeError(f"Kraken OHLC error: {data['error']}")
        result = data["result"]
        key = next(k for k in result if k != "last")
        rows = result[key]
        if not rows:
            break
        for row in rows:
            ts = int(row[0])
            if start_ts <= ts <= end_ts:
                # [time, open, high, low, close, vwap, volume, count]
                candles[ts] = _norm_candle(ts, row[1], row[2], row[3], row[4], row[6])
        last = int(result.get("last") or rows[-1][0])
        if last <= since or rows[-1][0] >= end_ts or len(rows) < 2:
            break
        since = last
        time.sleep(0.15)
    return [candles[k] for k in sorted(candles)]


def fetch_candles(start_ts: int, end_ts: int) -> tuple[list[dict], str]:
    errors = []
    try:
        candles = fetch_coinbase_candles(start_ts, end_ts)
        if len(candles) >= 80:
            return candles, "coinbase"
        errors.append(f"coinbase returned only {len(candles)} candles")
    except Exception as exc:
        errors.append(f"coinbase: {exc}")
    try:
        candles = fetch_kraken_candles(start_ts, end_ts)
        if len(candles) >= 80:
            return candles, "kraken"
        errors.append(f"kraken returned only {len(candles)} candles")
    except Exception as exc:
        errors.append(f"kraken: {exc}")
    raise RuntimeError("Could not fetch hourly BTC/USD candles. " + " | ".join(errors))


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------

def sma(values: list[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


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
    gains = 0.0
    losses = 0.0
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
        rs = ag / al
        return 100.0 - (100.0 / (1.0 + rs))

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
    ema12: Optional[float]
    ema26: Optional[float]
    ema50: Optional[float]
    rsi14: Optional[float]
    macd: Optional[float]
    macd_signal: Optional[float]
    macd_hist: Optional[float]

    def ready(self) -> bool:
        return None not in (self.ema12, self.ema26, self.ema50, self.rsi14, self.macd_hist)


def compute_indicator_series(candles: list[dict]) -> list[Indicators]:
    closes = [c["close"] for c in candles]
    close_opts: list[Optional[float]] = list(closes)
    ema12 = series_ema(close_opts, FAST_EMA)
    ema26 = series_ema(close_opts, SLOW_EMA)
    ema50 = series_ema(close_opts, TREND_EMA)
    rsi14 = series_rsi(closes, RSI_PERIOD)
    macd_line: list[Optional[float]] = [
        (a - b) if a is not None and b is not None else None for a, b in zip(ema12, ema26)
    ]
    macd_signal = series_ema(macd_line, MACD_SIGNAL)
    macd_hist: list[Optional[float]] = [
        (m - s) if m is not None and s is not None else None for m, s in zip(macd_line, macd_signal)
    ]
    out: list[Indicators] = []
    for i in range(len(candles)):
        out.append(
            Indicators(
                ema12=ema12[i],
                ema26=ema26[i],
                ema50=ema50[i],
                rsi14=rsi14[i],
                macd=macd_line[i],
                macd_signal=macd_signal[i],
                macd_hist=macd_hist[i],
            )
        )
    return out


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

def entry_ok(ind: Indicators, close: float) -> bool:
    if not ind.ready():
        return False
    return (
        ind.ema12 > ind.ema26  # type: ignore[operator]
        and close > ind.ema50  # type: ignore[operator]
        and ind.macd_hist > 0  # type: ignore[operator]
        and RSI_MIN <= ind.rsi14 <= RSI_MAX  # type: ignore[operator]
    )


def entry_blockers(ind: Indicators, close: float) -> list[str]:
    reasons = []
    if not ind.ready():
        return ["Indicators are still warming up (need about 60 hourly candles)."]
    if not (ind.ema12 > ind.ema26):
        reasons.append("EMA 12 is still below EMA 26, so the short-term trend is not up yet")
    if not (close > ind.ema50):
        reasons.append("price is below the 50-hour EMA trend filter")
    if not (ind.macd_hist > 0):
        reasons.append("MACD histogram is negative, so momentum is not confirming")
    if ind.rsi14 < RSI_MIN:
        reasons.append(f"RSI is {ind.rsi14:.1f}, below the {RSI_MIN:.0f} momentum threshold")
    elif ind.rsi14 > RSI_MAX:
        reasons.append(f"RSI is {ind.rsi14:.1f}, above {RSI_MAX:.0f} — the move looks overheated")
    return reasons


def explain_entry(ind: Indicators, close: float) -> str:
    return (
        f"Uptrend confirmed (EMA 12 above EMA 26, price above the 50-hour EMA) "
        f"with healthy momentum (RSI {ind.rsi14:.1f}, MACD histogram positive)."
    )


def explain_waiting(ind: Indicators, close: float) -> str:
    blockers = entry_blockers(ind, close)
    if not blockers:
        return "Entry conditions are met on the last closed hour — placing a paper buy."
    if len(blockers) == 1:
        return "Waiting: " + blockers[0] + "."
    return "Waiting: " + "; ".join(blockers) + "."


def buy_fill(price: float) -> float:
    return price * (1.0 + SLIPPAGE_RATE)


def sell_fill(price: float) -> float:
    return price * (1.0 - SLIPPAGE_RATE)


def open_long(cash: float, signal_price: float, ts: int, reason: str) -> tuple[float, dict, dict]:
    fill = buy_fill(signal_price)
    budget = cash * POSITION_FRAC
    qty = budget / (fill * (1.0 + FEE_RATE))
    fee = fill * qty * FEE_RATE
    slippage_cost = (fill - signal_price) * qty
    spent = fill * qty + fee
    cash_after = cash - spent
    stop_loss = fill * (1.0 - STOP_PCT)
    position = {
        "entry_time": int(ts),
        "entry_time_pt": fmt_pt(ts),
        "signal_price": round_px(signal_price),
        "fill_price": round_px(fill),
        "qty": round_qty(qty),
        "cost_basis": round_money(spent),
        "fee": round_money(fee),
        "slippage_cost": round_money(slippage_cost),
        "stop_loss": round_px(stop_loss),
        "high_since_entry": round_px(fill),
        "trailing_active": False,
        "trailing_stop": None,
        "reason": reason,
    }
    log = {
        "side": "buy",
        "time": int(ts),
        "time_pt": fmt_pt(ts),
        "signal_price": round_px(signal_price),
        "fill_price": round_px(fill),
        "qty": round_qty(qty),
        "fee": round_money(fee),
        "slippage_cost": round_money(slippage_cost),
        "reason": reason,
        "pnl": None,
        "pnl_pct": None,
    }
    return cash_after, position, log


def close_long(position: dict, cash: float, signal_price: float, ts: int, reason: str) -> tuple[float, dict]:
    qty = float(position["qty"])
    fill = sell_fill(signal_price)
    notional = fill * qty
    fee = notional * FEE_RATE
    slippage_cost = (signal_price - fill) * qty
    proceeds = notional - fee
    cash_after = cash + proceeds
    cost = float(position["cost_basis"])
    pnl = proceeds - cost
    pnl_pct = (pnl / cost) * 100.0 if cost else 0.0
    hold_hours = max(0, int((int(ts) - int(position["entry_time"])) / GRANULARITY))
    log = {
        "side": "sell",
        "time": int(ts),
        "time_pt": fmt_pt(ts),
        "signal_price": round_px(signal_price),
        "fill_price": round_px(fill),
        "qty": round_qty(qty),
        "fee": round_money(fee),
        "slippage_cost": round_money(slippage_cost),
        "reason": reason,
        "pnl": round_money(pnl),
        "pnl_pct": round(pnl_pct, 2),
        "hold_hours": hold_hours,
        "entry_time": int(position["entry_time"]),
        "entry_time_pt": position.get("entry_time_pt") or fmt_pt(position["entry_time"]),
        "entry_fill": position["fill_price"],
    }
    return cash_after, log


def update_stops(position: dict, high_price: float) -> dict:
    pos = dict(position)
    high = max(float(pos["high_since_entry"]), float(high_price))
    pos["high_since_entry"] = round_px(high)
    entry = float(pos["fill_price"])
    if (high / entry - 1.0) >= TRAIL_ARM_PCT:
        pos["trailing_active"] = True
        pos["trailing_stop"] = round_px(high * (1.0 - TRAIL_PCT))
    return pos


def stop_hit(position: dict, candle_open: float, candle_low: float) -> Optional[tuple[str, float]]:
    """Return (reason, exit_signal_price) if a stop would be hit on this bar."""
    hard = float(position["stop_loss"])
    trail = position.get("trailing_stop")
    candidates: list[tuple[float, str]] = [(hard, "Hard stop-loss hit (−3.0% from entry)")]
    if position.get("trailing_active") and trail is not None:
        candidates.append((float(trail), "Trailing stop hit (−2.5% off the high since entry)"))
    # The higher stop is hit first on the way down.
    candidates.sort(key=lambda x: x[0], reverse=True)
    for stop_px, reason in candidates:
        if candle_low <= stop_px:
            # Gap through: fill at the open; otherwise the stop price itself.
            exit_px = candle_open if candle_open <= stop_px else stop_px
            return reason, exit_px
    return None


def live_stop_hit(position: dict, last_price: float) -> Optional[tuple[str, float]]:
    hard = float(position["stop_loss"])
    trail = position.get("trailing_stop")
    candidates: list[tuple[float, str]] = [(hard, "Hard stop-loss hit (−3.0% from entry)")]
    if position.get("trailing_active") and trail is not None:
        candidates.append((float(trail), "Trailing stop hit (−2.5% off the high since entry)"))
    candidates.sort(key=lambda x: x[0], reverse=True)
    for stop_px, reason in candidates:
        if last_price <= stop_px:
            return reason, last_price
    return None


def mark_equity(cash: float, position: Optional[dict], last_price: float) -> float:
    if not position:
        return round_money(cash)
    return round_money(cash + float(position["qty"]) * last_price)


# ---------------------------------------------------------------------------
# Paper engine (shared by live + backtest)
# ---------------------------------------------------------------------------

@dataclass
class Book:
    cash: float = STARTING_BALANCE
    position: Optional[dict] = None
    trades: list[dict] = field(default_factory=list)
    equity_history: list[dict] = field(default_factory=list)

    @property
    def closed_round_trips(self) -> list[dict]:
        return [t for t in self.trades if t.get("side") == "sell"]


def record_equity(book: Book, ts: int, price: float) -> None:
    eq = mark_equity(book.cash, book.position, price)
    if book.equity_history and int(book.equity_history[-1]["t"]) == int(ts):
        book.equity_history[-1] = {"t": int(ts), "t_pt": fmt_pt(ts), "equity": eq, "price": round_px(price)}
        return
    book.equity_history.append({"t": int(ts), "t_pt": fmt_pt(ts), "equity": eq, "price": round_px(price)})


def process_closed_candle(book: Book, candle: dict, ind: Indicators) -> Optional[str]:
    """Apply exits then entries on one closed candle. Returns event reason or None."""
    ts = int(candle["time"])
    close = float(candle["close"])
    event = None

    if book.position:
        book.position = update_stops(book.position, float(candle["high"]))
        hit = stop_hit(book.position, float(candle["open"]), float(candle["low"]))
        if hit:
            reason, exit_px = hit
            book.cash, log = close_long(book.position, book.cash, exit_px, ts, reason)
            book.trades.append(log)
            book.position = None
            event = reason
        elif ind.ready() and ind.ema12 < ind.ema26:  # type: ignore[operator]
            reason = "Trend exit: EMA 12 crossed back below EMA 26"
            book.cash, log = close_long(book.position, book.cash, close, ts, reason)
            book.trades.append(log)
            book.position = None
            event = reason

    if book.position is None and entry_ok(ind, close):
        reason = explain_entry(ind, close)
        book.cash, book.position, log = open_long(book.cash, close, ts, reason)
        book.trades.append(log)
        # Enter at the close, so this bar's high/low already happened and must
        # not arm a trailing stop or trigger an exit we could not have caught.
        event = "Paper buy: " + reason

    record_equity(book, ts, close)
    return event


def summarize_trades(book: Book, start_ts: int, end_ts: int, start_price: float, end_price: float, start_equity: float = STARTING_BALANCE) -> dict:
    sells = book.closed_round_trips
    wins = [t for t in sells if float(t.get("pnl") or 0) > 0]
    losses = [t for t in sells if float(t.get("pnl") or 0) <= 0]
    gross_profit = sum(float(t["pnl"]) for t in wins)
    gross_loss = abs(sum(float(t["pnl"]) for t in losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (None if not sells else None if gross_profit == 0 else 999.99)
    if gross_loss == 0 and gross_profit > 0:
        profit_factor = None  # undefined / infinite; dashboard can say "n/a (no losses)"
        profit_factor_display = None
        profit_factor_note = "n/a (no losing trades)"
    elif not sells:
        profit_factor_display = None
        profit_factor_note = "n/a (no closed trades)"
    else:
        profit_factor_display = round(profit_factor, 2) if profit_factor is not None else 0.0
        profit_factor_note = None

    end_eq = book.equity_history[-1]["equity"] if book.equity_history else start_equity
    ret_pct = ((end_eq / start_equity) - 1.0) * 100.0 if start_equity else 0.0
    bh_pct = ((end_price / start_price) - 1.0) * 100.0 if start_price else 0.0

    peak = start_equity
    max_dd = 0.0
    for pt in book.equity_history:
        eq = float(pt["equity"])
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd

    # Buy-and-hold equity path, $1,000 scaled to first comparable close (no extra fees).
    bh_series = []
    for pt in book.equity_history:
        bh_eq = round_money(start_equity * (float(pt["price"]) / start_price)) if start_price else start_equity
        bh_series.append({"t": pt["t"], "t_pt": pt["t_pt"], "equity": bh_eq, "price": pt["price"]})

    win_rate = (len(wins) / len(sells) * 100.0) if sells else None
    return {
        "start": iso_utc(start_ts),
        "end": iso_utc(end_ts),
        "start_pt": fmt_pt(start_ts),
        "end_pt": fmt_pt(end_ts),
        "days": round((end_ts - start_ts) / 86400, 1),
        "bars": len(book.equity_history),
        "starting_balance": start_equity,
        "ending_equity": round_money(end_eq),
        "return_pct": round(ret_pct, 2),
        "buy_hold_pct": round(bh_pct, 2),
        "trade_count": len(sells),
        "buys": sum(1 for t in book.trades if t.get("side") == "buy"),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1) if win_rate is not None else None,
        "profit_factor": profit_factor_display,
        "profit_factor_note": profit_factor_note,
        "gross_profit": round_money(gross_profit),
        "gross_loss": round_money(gross_loss),
        "max_drawdown_pct": round(max_dd * 100.0, 2),
        "equity_history": book.equity_history,
        "buy_hold_equity": bh_series,
        "trades": book.trades,
    }


# ---------------------------------------------------------------------------
# State / dashboard I/O
# ---------------------------------------------------------------------------

def default_state() -> dict:
    return {
        "cash": STARTING_BALANCE,
        "position": None,
        "trades": [],
        "equity_history": [],
        "last_processed_ts": None,
        "starting_balance": STARTING_BALANCE,
        "created_at": iso_utc(utc_now()),
    }


def load_state() -> dict:
    if not STATE_PATH.exists():
        return default_state()
    with STATE_PATH.open() as f:
        state = json.load(f)
    state.setdefault("cash", STARTING_BALANCE)
    state.setdefault("position", None)
    state.setdefault("trades", [])
    state.setdefault("equity_history", [])
    state.setdefault("starting_balance", STARTING_BALANCE)
    return state


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    tmp.replace(path)


def book_from_state(state: dict) -> Book:
    return Book(
        cash=float(state["cash"]),
        position=state.get("position"),
        trades=list(state.get("trades") or []),
        equity_history=list(state.get("equity_history") or []),
    )


def indicators_payload(ind: Indicators, candle: dict) -> dict:
    return {
        "ema12": round_ind(ind.ema12, 2),
        "ema26": round_ind(ind.ema26, 2),
        "ema50": round_ind(ind.ema50, 2),
        "rsi14": round_ind(ind.rsi14, 2),
        "macd": round_ind(ind.macd, 4),
        "macd_signal": round_ind(ind.macd_signal, 4),
        "macd_hist": round_ind(ind.macd_hist, 4),
        "candle_close": round_px(candle["close"]),
        "candle_time": int(candle["time"]),
        "candle_time_pt": fmt_pt(candle["time"]),
        "filters": {
            "ema_cross": bool(ind.ready() and ind.ema12 > ind.ema26),
            "above_trend": bool(ind.ready() and candle["close"] > ind.ema50),
            "macd_pos": bool(ind.ready() and ind.macd_hist > 0),
            "rsi_band": bool(ind.ready() and RSI_MIN <= ind.rsi14 <= RSI_MAX),
        },
    }


def position_payload(position: Optional[dict], last_price: float) -> Optional[dict]:
    if not position:
        return None
    pos = update_stops(dict(position), last_price)
    entry = float(pos["fill_price"])
    qty = float(pos["qty"])
    mtm = qty * last_price
    # Estimated proceeds if we sold now (after fee + slippage)
    est_fill = sell_fill(last_price)
    est_proceeds = est_fill * qty * (1.0 - FEE_RATE)
    unreal = mtm - float(pos["cost_basis"])
    unreal_pct = (last_price / entry - 1.0) * 100.0
    est_pnl = est_proceeds - float(pos["cost_basis"])
    return {
        **pos,
        "last_price": round_px(last_price),
        "market_value": round_money(mtm),
        "unrealized_pnl": round_money(unreal),
        "unrealized_pnl_pct": round(unreal_pct, 2),
        "est_exit_proceeds": round_money(est_proceeds),
        "est_exit_pnl": round_money(est_pnl),
        "est_exit_pnl_pct": round((est_pnl / float(pos["cost_basis"])) * 100.0, 2) if pos["cost_basis"] else 0.0,
    }


# ---------------------------------------------------------------------------
# Backtest + live run
# ---------------------------------------------------------------------------

def split_closed_and_live(candles: list[dict], now: Optional[datetime] = None) -> tuple[list[dict], Optional[dict]]:
    """Hourly buckets that have not yet closed are excluded from signals."""
    now = now or utc_now()
    closed_end = int(now.timestamp()) - (int(now.timestamp()) % GRANULARITY)
    closed = [c for c in candles if int(c["time"]) < closed_end]
    live = None
    for c in candles:
        if int(c["time"]) == closed_end:
            live = c
    return closed, live


def run_backtest(candles: list[dict], series: list[Indicators]) -> dict:
    closed, _ = split_closed_and_live(candles)
    if len(closed) < WARMUP_BARS + 10:
        raise RuntimeError(f"Not enough closed candles for a backtest ({len(closed)}).")

    # Align indicators: series was computed on the full `candles` list (same order).
    by_time = {int(c["time"]): (c, series[i]) for i, c in enumerate(candles)}

    start_idx = WARMUP_BARS
    # Find first closed bar with ready indicators at/after warmup.
    tradeable = []
    for c in closed:
        ind = by_time[int(c["time"])][1]
        if ind.ready():
            tradeable.append((c, ind))
    if len(tradeable) < 10:
        raise RuntimeError("Not enough tradeable bars after indicator warmup.")

    start_c, _ = tradeable[0]
    end_c, _ = tradeable[-1]
    book = Book()
    # Equity starts at the first tradeable close (flat).
    record_equity(book, int(start_c["time"]), float(start_c["close"]))
    # Process from the next bar so the first point is a flat $1,000 baseline.
    for c, ind in tradeable[1:]:
        process_closed_candle(book, c, ind)
    # If still in a position at the end, mark to the last close (do not force-close;
    # report open trade separately so the historical book stays honest).
    summary = summarize_trades(
        book,
        start_ts=int(start_c["time"]),
        end_ts=int(end_c["time"]),
        start_price=float(start_c["close"]),
        end_price=float(end_c["close"]),
    )
    summary["source_bars"] = len(closed)
    summary["warmup_bars"] = WARMUP_BARS
    summary["open_at_end"] = book.position is not None
    if book.position:
        summary["open_position_note"] = (
            f"Still long at the end of the window (entry {book.position['fill_price']}). "
            "That trade is marked to the last close and is not counted in win rate until it exits."
        )
    else:
        summary["open_position_note"] = None
    summary["fee_note"] = (
        "Every simulated buy and sell includes 0.10% fee + 0.05% slippage (0.15% per side). "
        "Buy-and-hold is the BTC price change over the same window, with no fees."
    )
    return summary


def run_live(state: dict, candles: list[dict], series: list[Indicators], last_price: float) -> tuple[dict, list[str]]:
    closed, _forming = split_closed_and_live(candles)
    by_time = {int(c["time"]): (c, series[i]) for i, c in enumerate(candles)}
    book = book_from_state(state)
    events: list[str] = []

    if not closed:
        raise RuntimeError("No closed hourly candles available.")

    last_processed = state.get("last_processed_ts")
    if last_processed is None:
        # First live run: do not backfill 90 days of trades into the paper book.
        # Only evaluate the most recently closed candle, then track forward.
        last_processed = int(closed[-2]["time"]) if len(closed) >= 2 else int(closed[-1]["time"]) - GRANULARITY

    new_closed = [c for c in closed if int(c["time"]) > int(last_processed)]
    for c in new_closed:
        pair = by_time.get(int(c["time"]))
        if not pair:
            continue
        _, ind = pair
        event = process_closed_candle(book, c, ind)
        if event:
            events.append(f"{fmt_pt(c['time'])}: {event}")

    last_closed = closed[-1]
    last_ind = by_time[int(last_closed["time"])][1]

    # Mid-hour stop check against the live last price (and bump the trailing high).
    if book.position:
        book.position = update_stops(book.position, last_price)
        hit = live_stop_hit(book.position, last_price)
        if hit:
            reason, exit_px = hit
            now_ts = int(utc_now().timestamp())
            book.cash, log = close_long(book.position, book.cash, exit_px, now_ts, reason + " (live price)")
            book.trades.append(log)
            book.position = None
            events.append(f"{fmt_pt(now_ts)}: {reason} (live price)")

    record_equity(book, int(utc_now().timestamp()), last_price)

    if book.position:
        pos_view = position_payload(book.position, last_price)
        status = "LONG"
        status_label = "LONG"
        trail_bit = (
            f" Trailing stop is armed at ${pos_view['trailing_stop']:,.2f} after the price ran "
            f"+{((pos_view['high_since_entry'] / pos_view['fill_price']) - 1) * 100:.1f}% from entry."
            if pos_view and pos_view.get("trailing_active")
            else " Trailing stop is not armed yet (arms once unrealized gain reaches +2.0%)."
        )
        reason = explain_entry(last_ind, float(last_closed["close"])) + trail_bit
        reason += (
            f" Hard stop sits at ${float(book.position['stop_loss']):,.2f} "
            f"(−3.0% from the ${float(book.position['fill_price']):,.2f} entry)."
        )
    else:
        status = "CASH"
        status_label = "CASH / WAITING"
        reason = explain_waiting(last_ind, float(last_closed["close"]))

    sells = [t for t in book.trades if t.get("side") == "sell"]
    wins = [t for t in sells if float(t.get("pnl") or 0) > 0]
    equity = mark_equity(book.cash, book.position, last_price)
    start = float(state.get("starting_balance") or STARTING_BALANCE)
    pnl = equity - start
    pnl_pct = (pnl / start) * 100.0 if start else 0.0

    new_state = {
        "cash": round_money(book.cash),
        "position": book.position,
        "trades": book.trades,
        "equity_history": book.equity_history,
        "last_processed_ts": int(last_closed["time"]),
        "starting_balance": start,
        "created_at": state.get("created_at") or iso_utc(utc_now()),
        "updated_at": iso_utc(utc_now()),
    }
    live_view = {
        "btc_price": round_px(last_price),
        "status": status,
        "status_label": status_label,
        "reason": reason,
        "cash": round_money(book.cash),
        "equity": round_money(equity),
        "pnl": round_money(pnl),
        "pnl_pct": round(pnl_pct, 2),
        "win_rate": round(len(wins) / len(sells) * 100.0, 1) if sells else None,
        "wins": len(wins),
        "losses": len(sells) - len(wins),
        "trade_count": len(sells),
        "buys": sum(1 for t in book.trades if t.get("side") == "buy"),
        "indicators": indicators_payload(last_ind, last_closed),
        "position": position_payload(book.position, last_price),
        "equity_history": book.equity_history,
        "trades": list(reversed(book.trades[-50:])),
        "last_processed_candle_pt": fmt_pt(last_closed["time"]),
        "starting_balance": start,
        "events": events,
    }
    return new_state, live_view, events


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    now = utc_now()
    # Pad one extra day so pagination edges don't clip the newest/oldest bar.
    end_ts = int(now.timestamp()) + GRANULARITY
    start_ts = int((now - timedelta(days=BACKTEST_DAYS + 5)).timestamp())

    print(f"[{fmt_pt(now)}] Fetching hourly BTC/USD candles…", flush=True)
    candles, candle_source = fetch_candles(start_ts, end_ts)
    last_price, price_source = fetch_live_price()
    print(
        f"  {len(candles)} hourly candles from {candle_source} "
        f"({fmt_pt(candles[0]['time'])} → {fmt_pt(candles[-1]['time'])})",
        flush=True,
    )
    print(f"  Live BTC/USD price: ${last_price:,.2f} ({price_source})", flush=True)

    series = compute_indicator_series(candles)
    backtest = run_backtest(candles, series)
    backtest["candle_source"] = candle_source
    backtest["generated_at"] = iso_utc(now)
    backtest["generated_at_pt"] = fmt_pt(now)
    save_json(BACKTEST_PATH, backtest)
    print(
        f"  Backtest {backtest['start_pt']} → {backtest['end_pt']} "
        f"({backtest['days']} days): {backtest['return_pct']:+.2f}% vs buy-and-hold "
        f"{backtest['buy_hold_pct']:+.2f}%, {backtest['trade_count']} trades, "
        f"win rate {backtest['win_rate']}%, max DD {backtest['max_drawdown_pct']:.2f}%",
        flush=True,
    )

    state = load_state()
    new_state, live_view, events = run_live(state, candles, series, last_price)
    save_json(STATE_PATH, new_state)

    dashboard = {
        "generated_at": iso_utc(now),
        "generated_at_pt": fmt_pt(now),
        "price_source": price_source,
        "candle_source": candle_source,
        "fee_note": "Every paper buy and sell includes 0.10% fee + 0.05% slippage (0.15% per side).",
        "starting_balance": STARTING_BALANCE,
        "live": live_view,
        "backtest": {
            k: v
            for k, v in backtest.items()
            if k != "trades" or True
        },
    }
    # Keep dashboard payload reasonably sized: cap long trade lists already handled;
    # equity series for 90d hourly is ~2k points, fine for a static JSON file.
    save_json(DASHBOARD_PATH, dashboard)

    print(f"  Status: {live_view['status_label']} | Equity: ${live_view['equity']:,.2f} | PnL: ${live_view['pnl']:+,.2f}", flush=True)
    print(f"  {live_view['reason']}", flush=True)
    for ev in events:
        print(f"  EVENT {ev}", flush=True)
    print(f"  Wrote {STATE_PATH.relative_to(ROOT)}, {BACKTEST_PATH.relative_to(ROOT)}, {DASHBOARD_PATH.relative_to(ROOT)}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
