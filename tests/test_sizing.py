#!/usr/bin/env python3
"""Unit/sanity checks for $4-$20 sizing and the 2x press rule (cap $20)."""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import (
    Indicators,
    LEGACY_FLAT_STAKE,
    STAKE_MIN,
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
    kalshi_taker_fee,
    load_state,
    next_stake_label,
    open_trade,
    resolve_entry_stake,
    size_ticket,
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
    # $4 win -> $8; $6 -> $12; $8 -> $16; $10 -> $20; $16 or $20 stay capped at $20.
    for won, nxt in ((4, 8), (6, 12), (8, 16), (10, 20), (16, 20), (20, 20)):
        press, last = apply_press_from_trade(False, None, {"pnl": 1.50, "stake": won})
        assert press is True
        assert last == min(float(won), STAKE_PRESS_CAP)
        stake, is_press = resolve_entry_stake(STAKE_NORMAL, press, last, cash=100)
        assert is_press is True
        assert stake == nxt, (won, stake, nxt)

    # After a pressed $16 win, the next press caps at $20
    press, last = apply_press_from_trade(True, 16.0, {"pnl": 8.0, "stake": 16.0})
    stake, is_press = resolve_entry_stake(STAKE_NORMAL, press, last, cash=100)
    assert is_press and stake == 20.0

    # Stored press values are clamped into [$4, $20] on load
    book = ensure_press_fields({"press_next": True, "last_win_stake": 2.0, "press_stake": 3.0, "cash": 100})
    assert book["last_win_stake"] == 4.0
    assert book["press_stake"] == 4.0
    high = ensure_press_fields({"press_next": True, "last_win_stake": 40.0, "press_stake": 40.0, "cash": 100})
    assert high["last_win_stake"] == 20.0
    assert high["press_stake"] == 20.0
    stake, is_press = resolve_entry_stake(STAKE_NORMAL, True, 40.0, cash=100)
    assert is_press and stake == 20.0

    # A loss resets to $4–$8
    press, last = apply_press_from_trade(True, 16.0, {"pnl": -8.0, "stake": 16.0})
    assert press is False and last is None
    stake, is_press = resolve_entry_stake(STAKE_STRONG, press, last, cash=100)
    assert is_press is False and stake == STAKE_STRONG

    # Cash between $4 and $20 still allows a ticket, limited to cash
    stake, is_press = resolve_entry_stake(STAKE_NORMAL, True, 8.0, cash=10.25)
    assert is_press and stake == 10.25

    # Cash under $4 skips rather than placing a sub-$4 ticket
    stake, is_press = resolve_entry_stake(STAKE_NORMAL, False, None, cash=3.50)
    assert stake == 0 and is_press is False

    assert next_stake_label(True, 4.0, 100).startswith("Next trade: 2× press ($8)")
    assert next_stake_label(True, 6.0, 100).startswith("Next trade: 2× press ($12)")
    assert next_stake_label(True, 8.0, 100).startswith("Next trade: 2× press ($16)")
    assert next_stake_label(True, 16.0, 100).startswith("Next trade: 2× press ($20)")
    assert next_stake_label(False, None) == "Next stake: $4–$20"


def test_tickets_stay_in_4_to_20_across_prices():
    market = {"ticker": "KXBTC15M-X", "event_ticker": "E", "title": "t", "close_time": None, "floor_strike": 100}
    prices = [round(p / 100.0, 2) for p in range(10, 91, 5)] + [0.75, 0.90]
    for ask in prices:
        for target in (4.0, 6.0, 8.0, 20.0):
            sized = size_ticket(ask, target, cash=100)
            assert sized is not None, (ask, target)
            n, cost, fee, total = sized
            assert n >= 1
            assert STAKE_MIN - 1e-9 <= total <= STAKE_PRESS_CAP + 1e-9, (ask, target, n, total, fee)
            assert abs(fee - kalshi_taker_fee(n, ask)) < 1e-9
            opened = open_trade(100, market, "yes", ask, "test", utc_now(), stake=target)
            assert opened is not None, (ask, target)
            cash, pos, log = opened
            assert STAKE_MIN - 1e-9 <= pos["amount_bought"] <= STAKE_PRESS_CAP + 1e-9, (ask, target, pos["amount_bought"])
            assert pos["amount_bought"] == total
            assert pos["contracts"] == n
            assert abs(100 - cash - total) < 1e-9
    # 90¢: 4 contracts + fee is under $4, so we bump until the ticket is ≥ $4
    four_cost = 4 * 0.90 + kalshi_taker_fee(4, 0.90)
    assert four_cost < 4.0
    n, _, _, total = size_ticket(0.90, 4.0, cash=100)
    assert n >= 5 and total >= 4.0
    # Cash under $4: skip
    assert size_ticket(0.50, 4.0, cash=3.99) is None
    assert open_trade(3.50, market, "no", 0.50, "test", utc_now(), stake=4) is None
    # Plenty of cash, $20 target never exceeds $20
    n, _, _, total = size_ticket(0.40, 20.0, cash=100)
    assert total <= 20.0 + 1e-9
    # $4 floor still holds at a cheap 10¢ ask
    n, _, _, total = size_ticket(0.10, 4.0, cash=100)
    assert total >= 4.0 and total <= 20.0


def test_lift_race_bankroll_preserves_pnl_and_trades():
    from bot import lift_race, lift_race_bankroll, RACE_STARTING
    trade = {"status": "open", "ticker": "T", "side": "no", "contracts": 4, "amount_bought": 3.4, "pnl": -0.08, "entry_time_pt": "Nov 26, 2026, 12:22 PM PT"}
    book = {
        "id": "v1",
        "starting_balance": 40.0,
        "cash": 36.6,
        "equity": 40.58,
        "pnl": 0.58,
        "amount_bought": 3.4,
        "contracts_total": 4,
        "wins": 0,
        "losses": 0,
        "trades": [dict(trade)],
        "open_positions": [dict(trade)],
    }
    lift_race_bankroll(book)
    assert book["starting_balance"] == 100.0
    assert book["cash"] == 96.6
    assert book["equity"] == 100.58
    assert abs(book["pnl"] - 0.58) < 1e-9
    assert book["amount_bought"] == 3.4
    assert book["contracts_total"] == 4
    assert book["trades"][0]["amount_bought"] == 3.4
    # Idempotent
    lift_race_bankroll(book)
    assert book["cash"] == 96.6 and book["starting_balance"] == 100.0
    race = {"starting_balance": 40.0, "v1": book, "v2": {"starting_balance": 40.0, "cash": 40.0, "equity": 40.0, "pnl": 0.0, "trades": []}}
    lift_race(race)
    assert race["starting_balance"] == RACE_STARTING
    assert race["v2"]["starting_balance"] == 100.0
    assert race["v2"]["cash"] == 100.0
    assert race["v2"]["equity"] == 100.0
    assert race["v2"]["pnl"] == 0.0


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
    # A legacy $5 ticket that wins arms a 2× press ($10), inside the $4–$20 band.
    press, last = apply_press_from_trade(False, None, {"pnl": 3.3, "total_in": 4.7, "stake": 5})
    assert last == 5.0
    stake, _ = resolve_entry_stake(4, press, last, cash=95.3)
    assert stake == 10.0


def test_dedupe_open_then_closed_and_open_trade_records_stake():
    open_row = {"status": "open", "ticker": "T", "entry_time": "a", "side": "no", "contracts": 8, "total_in": 4.7}
    closed = {"status": "closed", "ticker": "T", "entry_time": "a", "side": "no", "contracts": 8, "total_in": 4.7, "pnl": -4.7}
    rows = dedupe_trades([open_row, closed])
    assert len(rows) == 1 and rows[0]["status"] == "closed"
    amount, contracts = book_totals([open_row, closed])
    assert amount == 4.7 and contracts == 8

    market = {"ticker": "KXBTC15M-X", "event_ticker": "E", "title": "t", "close_time": None, "floor_strike": 100}
    opened = open_trade(100, market, "no", 0.50, "test", utc_now(), stake=6, pressed=False, strength="strong")
    assert opened is not None
    cash, pos, log = opened
    assert pos["stake"] == 6.0 and pos["pressed"] is False and pos["strength"] == "strong"
    assert pos["amount_bought"] == pos["total_in"]
    assert log["amount_bought"] == pos["total_in"]
    # $6 target at 50¢: stay inside $4–$6 after fees.
    assert 4.0 <= pos["amount_bought"] <= 6.0 + 1e-9
    assert pos["contracts"] >= 1
    cash2, closed_t = close_trade(cash, pos, exit_price=None, exit_fee=0, result="no", outcome="settlement", reason="win", now=utc_now())
    assert closed_t["pressed"] is False and closed_t["stake"] == 6.0
    assert closed_t["pnl"] > 0


if __name__ == "__main__":
    tests = [
        test_grade_normal_strong_top,
        test_press_ladder_and_reset,
        test_tickets_stay_in_4_to_20_across_prices,
        test_lift_race_bankroll_preserves_pnl_and_trades,
        test_legacy_state_upgrades_without_reset,
        test_dedupe_open_then_closed_and_open_trade_records_stake,
    ]
    for fn in tests:
        fn()
        print("OK", fn.__name__)
    print("All sizing/press checks passed.")
