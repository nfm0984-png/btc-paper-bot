#!/usr/bin/env python3
"""Unit/sanity checks for $2-$4 sizing and the 2x press rule."""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import (
    Indicators,
    LEGACY_FLAT_STAKE,
    STAKE_NORMAL,
    STAKE_PRESS_CAP,
    STAKE_STRONG,
    STAKE_TOP,
    apply_press_from_trade,
    book_totals,
    close_trade,
    dedupe_trades,
    ensure_press_fields,
    grade_signal,
    inferred_stake,
    load_state,
    next_stake_label,
    open_trade,
    resolve_entry_stake,
    utc_now,
)


def ind(**kw) -> Indicators:
    base = dict(ema9=60450, ema21=60400, ema50=60380, rsi14=60, macd_hist=10, close=60480, ready=True)
    base.update(kw)
    return Indicators(**base)


def test_grade_normal_strong_top():
    # Barely qualifying: tight EMAs, small MACD, RSI on the edge, short of strike cushion, expensive ask.
    g, stake, _ = grade_signal(
        "yes", 0.70,
        ind(ema9=60420, ema21=60410, macd_hist=2, rsi14=53),
        ind(ema9=60410, ema21=60400, ready=True),  # 5m spread only $10
        60440, 60420,  # $20 from strike — below the $50 bonus
    )
    assert (g, stake) == ("normal", STAKE_NORMAL), (g, stake)

    # Two quality points: wide EMA + mid RSI (ask is pricey, strike cushion small, weak MACD, weak 5m).
    g, stake, notes = grade_signal(
        "yes", 0.70,
        ind(ema9=60500, ema21=60400, macd_hist=3, rsi14=62),  # spread $100 + RSI heart
        ind(ema9=60410, ema21=60400, ready=True),
        60440, 60420,
    )
    assert (g, stake) == ("strong", STAKE_STRONG), (g, stake, notes)

    # Three+ points: wide EMA, strong MACD, mid RSI, $80 from strike, cheap ask, clear 5m.
    g, stake, notes = grade_signal(
        "no", 0.45,
        ind(ema9=60300, ema21=60400, ema50=60450, rsi14=38, macd_hist=-12, close=60280),
        ind(ema9=60340, ema21=60420, ready=True),
        60280, 60400,
    )
    assert (g, stake) == ("top", STAKE_TOP), (g, stake, notes)


def test_press_ladder_and_reset():
    # $2 win -> $4 next; $3 win -> $6; $4 win -> $8; legacy $5 win -> cap $8
    for won, nxt in ((2, 4), (3, 6), (4, 8), (5, 8)):
        press, last = apply_press_from_trade(False, None, {"pnl": 1.50, "stake": won})
        assert press is True and last == float(won)
        stake, is_press = resolve_entry_stake(STAKE_NORMAL, press, last, cash=40)
        assert is_press is True
        assert stake == nxt, (won, stake, nxt)

    # After a pressed win, keep pressing 2x capped at $8
    press, last = apply_press_from_trade(True, 4.0, {"pnl": 3.0, "stake": 8.0})
    stake, is_press = resolve_entry_stake(STAKE_NORMAL, press, last, cash=40)
    assert is_press and stake == 8.0

    # A loss resets to $2-$4
    press, last = apply_press_from_trade(True, 8.0, {"pnl": -4.7, "stake": 8.0})
    assert press is False and last is None
    stake, is_press = resolve_entry_stake(STAKE_STRONG, press, last, cash=40)
    assert is_press is False and stake == STAKE_STRONG

    # Never more than cash
    stake, is_press = resolve_entry_stake(STAKE_NORMAL, True, 4.0, cash=3.25)
    assert is_press and stake == 3.25

    assert next_stake_label(True, 3.0, 40).startswith("Next trade: 2× press ($6)")
    assert next_stake_label(False, None) == "Next stake: $2–$4"


def test_legacy_state_upgrades_without_reset():
    raw = json.loads((ROOT / "data" / "state.json").read_text())
    # The live files should still have the original $40 session and $100 book.
    assert raw["starting_balance"] == 100
    assert raw["session"]["starting_balance"] == 40
    assert "press_next" not in raw or raw.get("press_next") in (False, True)
    book = ensure_press_fields(dict(raw))
    sess = ensure_press_fields(dict(raw["session"]))
    assert book["cash"] == raw["cash"]
    assert sess["cash"] == raw["session"]["cash"]
    # Open or already-settled books both keep their cash and ticket history.
    if raw.get("position"):
        assert book["position"]["ticker"] == raw["position"]["ticker"]
        if "stake" not in (raw["position"] or {}):
            assert inferred_stake(raw["position"]) == LEGACY_FLAT_STAKE
    amount, contracts = book_totals(raw["session"]["trades"], raw["session"]["position"])
    assert contracts == 8
    assert amount == 4.7
    # A legacy $5 ticket that wins still arms a press capped at $8 (not $10).
    press, last = apply_press_from_trade(False, None, {"pnl": 3.3, "total_in": 4.7, "stake": 5})
    assert last == 5.0
    stake, _ = resolve_entry_stake(2, press, last, cash=35.3)
    assert stake == 8.0


def test_dedupe_open_then_closed_and_open_trade_records_stake():
    open_row = {"status": "open", "ticker": "T", "entry_time": "a", "side": "no", "contracts": 8, "total_in": 4.7}
    closed = {"status": "closed", "ticker": "T", "entry_time": "a", "side": "no", "contracts": 8, "total_in": 4.7, "pnl": -4.7}
    rows = dedupe_trades([open_row, closed])
    assert len(rows) == 1 and rows[0]["status"] == "closed"
    amount, contracts = book_totals([open_row, closed])
    assert amount == 4.7 and contracts == 8

    market = {"ticker": "KXBTC15M-X", "event_ticker": "E", "title": "t", "close_time": None, "floor_strike": 100}
    opened = open_trade(40, market, "no", 0.50, "test", utc_now(), stake=3, pressed=False, strength="strong")
    assert opened is not None
    cash, pos, log = opened
    assert pos["stake"] == 3.0 and pos["pressed"] is False and pos["strength"] == "strong"
    assert pos["amount_bought"] == pos["total_in"]
    assert log["amount_bought"] == pos["total_in"]
    # $3 at 50c -> 6 contracts + fee, still under $40
    assert pos["contracts"] == 6
    cash2, closed_t = close_trade(cash, pos, exit_price=None, exit_fee=0, result="no", outcome="settlement", reason="win", now=utc_now())
    assert closed_t["pressed"] is False and closed_t["stake"] == 3.0
    assert closed_t["pnl"] > 0


if __name__ == "__main__":
    tests = [test_grade_normal_strong_top, test_press_ladder_and_reset, test_legacy_state_upgrades_without_reset, test_dedupe_open_then_closed_and_open_trade_records_stake]
    for fn in tests:
        fn()
        print("OK", fn.__name__)
    print("All sizing/press checks passed.")
