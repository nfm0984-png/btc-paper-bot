#!/usr/bin/env python3
"""Head-to-head $100 race: Version 1 multi-trade vs Version 2 strict rules."""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import (
    Indicators,
    RACE_STARTING,
    STAKE_NORMAL,
    STAKE_MAX,
    V1_ASK_MAX,
    V1_ASK_MIN,
    V1_MIN_STRIKE_GAP,
    apply_press_from_trade,
    ensure_race,
    make_v1_signal,
    new_race_book,
    resolve_entry_stake,
    score_side,
    step_v1_book,
    step_v2_book,
    utc_now,
    window_trade_count,
)


def bull(**kw) -> Indicators:
    base = dict(ema9=60520, ema21=60440, ema50=60380, rsi14=62, macd_hist=12, close=60540, ready=True)
    base.update(kw)
    return Indicators(**base)


def bear(**kw) -> Indicators:
    base = dict(ema9=60320, ema21=60420, ema50=60480, rsi14=38, macd_hist=-11, close=60290, ready=True)
    base.update(kw)
    return Indicators(**base)


def market(now, *, ticker="KXBTC15M-TEST-A", strike=60400, yes=0.58, no=0.43, remaining=12 * 60, result=None):
    close = now + timedelta(seconds=remaining)
    open_ = close - timedelta(minutes=15)
    m = {
        "ticker": ticker,
        "event_ticker": "E",
        "title": "BTC price up in next 15 mins?",
        "status": "settled" if result else "open",
        "floor_strike": strike,
        "open_time": open_,
        "close_time": close,
        "yes_ask_dollars": yes,
        "yes_bid_dollars": max(0.01, yes - 0.02),
        "no_ask_dollars": no,
        "no_bid_dollars": max(0.01, no - 0.02),
        "result": result,
    }
    if result:
        m["expiration_value"] = strike + (40 if result == "yes" else -40)
        m["settlement_ts"] = close
    return m


def test_v1_scores_yes_vs_no_and_writes_choice_reason():
    now = utc_now()
    sig = make_v1_signal(
        ind_1m=bull(),
        ind_5m=bull(ema9=60510, ema21=60460, rsi14=58, macd_hist=6, close=60520),
        btc_price=60540,
        strike=60400,
        yes_ask=0.58,
        yes_bid=0.56,
        no_ask=0.43,
        no_bid=0.41,
        remaining_sec=12 * 60,
        open_positions=[],
        window_trade_count=0,
    )
    assert sig.action == "BUY_YES", sig
    assert sig.ask == 0.58
    assert sig.base_stake in (4.0, 6.0, 8.0)
    assert "Picked YES @ 58¢" in sig.reason
    assert "above strike" in sig.reason
    # Bearish tape should prefer NO even if Yes is cheap-ish.
    sig_n = make_v1_signal(
        ind_1m=bear(),
        ind_5m=bear(ema9=60340, ema21=60400, rsi14=40, macd_hist=-6, close=60300),
        btc_price=60290,
        strike=60400,
        yes_ask=0.40,
        yes_bid=0.38,
        no_ask=0.61,
        no_bid=0.59,
        remaining_sec=12 * 60,
        open_positions=[],
        window_trade_count=0,
    )
    assert sig_n.action == "BUY_NO", sig_n
    assert "Picked NO" in sig_n.reason
    assert "below strike" in sig_n.reason


def test_v1_can_add_and_flip_up_to_three_then_caps():
    now = utc_now()
    ticker = "KXBTC15M-TEST-MULTI"
    book = new_race_book("v1", now)
    m1 = market(now, ticker=ticker, strike=60400, yes=0.58, no=0.43, remaining=12 * 60)
    book, sig1 = step_v1_book(book, m1, [], bull(), bull(), 60540, now)
    assert sig1.action in ("HOLD", "BUY_YES")
    assert len(book["open_positions"]) == 1
    assert book["open_positions"][0]["side"] == "yes"
    assert book["open_positions"][0]["window_entry"] == 1
    assert "Picked YES" in (book["checks"][-1]["reason"] or "")
    # Confirmation / better price a few minutes later — add YES.
    now2 = now + timedelta(minutes=5)
    m2 = market(now2, ticker=ticker, strike=60400, yes=0.52, no=0.49, remaining=7 * 60)
    book, sig2 = step_v1_book(book, m2, [], bull(close=60580, ema9=60560), bull(), 60590, now2)
    assert len(book["open_positions"]) == 2, (sig2, book["events"])
    assert all(p["side"] == "yes" for p in book["open_positions"])
    assert "Entry #2" in sig2.reason
    assert "adding YES" in sig2.reason
    # Momentum flips — enter NO as a third ticket.
    now3 = now + timedelta(minutes=10)
    m3 = market(now3, ticker=ticker, strike=60400, yes=0.70, no=0.42, remaining=4 * 60)
    book, sig3 = step_v1_book(book, m3, [], bear(), bear(), 60280, now3)
    assert len(book["open_positions"]) == 3, (sig3.reason, book["events"])
    assert book["open_positions"][-1]["side"] == "no"
    assert "Entry #3" in sig3.reason
    assert "flipped" in sig3.reason.lower()
    # Fourth check cannot add another ticket.
    now4 = now + timedelta(minutes=11)
    m4 = market(now4, ticker=ticker, strike=60400, yes=0.30, no=0.71, remaining=3 * 60)
    book, sig4 = step_v1_book(book, m4, [], bear(), bear(), 60250, now4)
    assert len(book["open_positions"]) == 3
    assert "3 trades" in sig4.reason or "cap" in sig4.label.lower() or "cap" in sig4.reason.lower() or "Already" in sig4.reason
    assert window_trade_count(book["trades"], ticker, book["open_positions"]) == 3


def test_v1_settles_all_open_positions_and_respects_flat_sizing_and_cash_cap():
    now = utc_now()
    ticker = "KXBTC15M-TEST-SETTLE"
    book = new_race_book("v1", now)
    m = market(now, ticker=ticker, strike=60400, yes=0.50, no=0.51, remaining=12 * 60)
    book, _ = step_v1_book(book, m, [], bull(), bull(), 60540, now)
    # Force a second open ticket at 50¢ (confirmation).
    now2 = now + timedelta(minutes=5)
    m2 = market(now2, ticker=ticker, strike=60400, yes=0.48, no=0.53, remaining=7 * 60)
    book, _ = step_v1_book(book, m2, [], bull(close=60600), bull(), 60620, now2)
    assert len(book["open_positions"]) == 2
    cash_before_settle = book["cash"]
    bought = book["amount_bought"]
    assert bought > 0 and cash_before_settle < RACE_STARTING
    settled = [market(now2, ticker=ticker, strike=60400, yes=0.48, no=0.53, remaining=0, result="yes")]
    now3 = now + timedelta(minutes=16)
    # No live market needed to settle.
    book, _ = step_v1_book(book, None, settled, bull(), bull(), 60620, now3)
    assert book["open_positions"] == []
    assert book["position"] is None
    closed = [t for t in book["trades"] if t.get("status") == "closed"]
    assert len(closed) == 2
    assert all(t["pnl"] > 0 for t in closed)
    assert book["wins"] == 2
    assert book["cash"] > cash_before_settle
    assert book["press_next"] is False
    # Flat $4–$8 sizing: a win does not arm a 2× press.
    last = closed[-1]["stake"]
    stake, pressed = resolve_entry_stake(STAKE_NORMAL, True, last, book["cash"])
    assert pressed is False
    assert stake == STAKE_NORMAL
    assert 4.0 - 1e-9 <= stake <= STAKE_MAX + 1e-9
    # Cash cap: a nearly empty book cannot open a ticket.
    broke = new_race_book("v1", now)
    broke["cash"] = 0.40
    broke["starting_balance"] = 40
    broke, sig = step_v1_book(broke, m, [], bull(), bull(), 60540, now)
    assert broke["open_positions"] == []
    assert "cash" in sig.reason.lower() or sig.action == "WAIT"


def test_v2_is_strict_one_trade_and_waits_without_edge():
    now = utc_now()
    ticker = "KXBTC15M-TEST-STRICT"
    book = new_race_book("v2", now)
    # Qualifying Yes: 35–75¢ (v1 defaults / v2 similar), $20+ from strike, bullish tape.
    good = market(now, ticker=ticker, strike=60400, yes=0.57, no=0.44, remaining=12 * 60)
    book, sig = step_v2_book(book, good, [], bull(), bull(), 60540, now, params=None, features=None, strategy_version=2)
    assert sig.action in ("HOLD", "BUY_YES"), sig
    assert len(book["open_positions"]) == 1
    assert book["open_positions"][0]["side"] == "yes"
    # Same window, later check — must NOT open a second ticket.
    now2 = now + timedelta(minutes=5)
    again = market(now2, ticker=ticker, strike=60400, yes=0.50, no=0.51, remaining=7 * 60)
    book, sig2 = step_v2_book(book, again, [], bull(), bull(), 60580, now2, params=None, features=None, strategy_version=2)
    assert len(book["open_positions"]) == 1
    assert sig2.action in ("HOLD", "WAIT")
    # A choppy / expensive window should WAIT on a fresh book.
    fresh = new_race_book("v2", now)
    pricey = market(now, ticker="KXBTC15M-TEST-WAIT", strike=60400, yes=0.88, no=0.14, remaining=12 * 60)
    fresh, wait = step_v2_book(fresh, pricey, [], bull(), bull(), 60540, now, params=None, features=None)
    assert wait.action == "WAIT"
    assert fresh["open_positions"] == []
    assert "WAIT" in wait.label or "pricey" in wait.reason.lower() or "expensive" in wait.reason.lower() or "edge" in wait.label.lower() or "too" in wait.reason.lower()


def test_ensure_race_does_not_reset_existing_books():
    now = utc_now()
    state = {
        "cash": 95.30,
        "starting_balance": 100.0,
        "stake": 2.0,
        "press_next": False,
        "last_win_stake": None,
        "position": None,
        "trades": [{"ticker": "OLD", "status": "closed", "pnl": -4.7, "contracts": 8, "amount_bought": 4.7}],
        "session": {
            "status": "COMPLETE",
            "final_balance": 35.30,
            "starting_balance": 40.0,
            "cash": 35.30,
            "equity": 35.30,
            "pnl": -4.7,
            "wins": 0,
            "losses": 1,
            "trades": [{"ticker": "KXBTC15M-26NOV261415-15", "status": "closed", "pnl": -4.7}],
        },
    }
    race = ensure_race(state, now, strategy_version=2)
    assert race["v1"]["cash"] == RACE_STARTING
    assert race["v2"]["cash"] == RACE_STARTING
    assert race["v1"]["open_positions"] == []
    assert race["start_pt"]
    # Existing paper books stay put.
    assert state["cash"] == 95.30
    assert state["starting_balance"] == 100.0
    assert state["session"]["status"] == "COMPLETE"
    assert state["session"]["final_balance"] == 35.30
    assert state["trades"][0]["pnl"] == -4.7
    # A second call must not reset a live race that already has a ticket.
    race["v1"]["cash"] = 36.10
    race["v1"]["trades"] = [{"ticker": "X", "status": "open"}]
    state["race"] = race
    again = ensure_race(state, now + timedelta(hours=1), strategy_version=2)
    assert again["v1"]["cash"] == 36.10
    assert again["v1"]["trades"][0]["ticker"] == "X"
    assert again["start"] == race["start"]
    # An existing $40 race is lifted to $100 by adding the cash delta; trades stay put.
    state40 = {
        "cash": 95.30,
        "starting_balance": 100.0,
        "session": state["session"],
        "trades": state["trades"],
        "race": {
            "starting_balance": 40.0,
            "start": race["start"],
            "start_pt": race["start_pt"],
            "v1": {"id": "v1", "starting_balance": 40.0, "cash": 36.60, "equity": 40.58, "pnl": 0.58, "trades": [{"ticker": "KEEP", "status": "open", "amount_bought": 3.4}], "open_positions": []},
            "v2": {"id": "v2", "starting_balance": 40.0, "cash": 40.0, "equity": 40.0, "pnl": 0.0, "trades": [], "open_positions": []},
        },
    }
    lifted = ensure_race(state40, now + timedelta(hours=2), strategy_version=2)
    assert lifted["starting_balance"] == 100.0
    assert lifted["v1"]["starting_balance"] == 100.0
    assert lifted["v1"]["cash"] == 96.60
    assert abs(lifted["v1"]["pnl"] - 0.58) < 1e-9
    assert lifted["v1"]["trades"][0]["ticker"] == "KEEP"
    assert lifted["v1"]["trades"][0]["amount_bought"] == 3.4
    assert lifted["v2"]["cash"] == 100.0
    assert lifted["start"] == race["start"]
    assert state40["cash"] == 95.30
    assert state40["session"]["final_balance"] == 35.30

def test_v1_rejects_expensive_favorite_close_strike_and_fade():
    now = utc_now()
    # 84¢ Yes while BTC is well above the strike — the favorite leak.
    expensive = make_v1_signal(
        ind_1m=bull(),
        ind_5m=bull(),
        btc_price=60540,
        strike=60400,
        yes_ask=0.84,
        yes_bid=0.82,
        no_ask=0.17,
        no_bid=0.15,
        remaining_sec=12 * 60,
        open_positions=[],
        window_trade_count=0,
    )
    assert expensive.action == "WAIT", expensive
    assert "expensive" in (expensive.reason or "").lower() or "35" in (expensive.reason or "")
    assert V1_ASK_MAX == 0.75 and V1_ASK_MIN == 0.35

    # Coin-flip: only $12 from the strike.
    tight = make_v1_signal(
        ind_1m=bull(),
        ind_5m=bull(),
        btc_price=60412,
        strike=60400,
        yes_ask=0.64,
        yes_bid=0.62,
        no_ask=0.37,
        no_bid=0.35,
        remaining_sec=8 * 60,
        open_positions=[],
        window_trade_count=0,
    )
    assert tight.action == "WAIT", tight
    assert "strike" in (tight.label or "").lower() or "band" in (tight.reason or "").lower()
    assert V1_MIN_STRIKE_GAP == 20.0

    # Fade leak: never buy No at 35¢ while BTC is already +$45 above the strike.
    fade = make_v1_signal(
        ind_1m=bear(),
        ind_5m=bear(),
        btc_price=60527,
        strike=60482,
        yes_ask=0.66,
        yes_bid=0.64,
        no_ask=0.35,
        no_bid=0.33,
        remaining_sec=12 * 60,
        open_positions=[],
        window_trade_count=0,
    )
    assert fade.action != "BUY_NO", fade
    # Same window with an untradable Yes — must WAIT rather than fade with No.
    fade_wait = make_v1_signal(
        ind_1m=bear(),
        ind_5m=bear(),
        btc_price=60527,
        strike=60482,
        yes_ask=0.84,
        yes_bid=0.82,
        no_ask=0.35,
        no_bid=0.33,
        remaining_sec=12 * 60,
        open_positions=[],
        window_trade_count=0,
    )
    assert fade_wait.action == "WAIT", fade_wait
    assert "fade" in (fade_wait.reason or "").lower() or "expensive" in (fade_wait.reason or "").lower() or "35" in (fade_wait.reason or "")



if __name__ == "__main__":
    tests = [
        test_v1_scores_yes_vs_no_and_writes_choice_reason,
        test_v1_can_add_and_flip_up_to_three_then_caps,
        test_v1_settles_all_open_positions_and_respects_flat_sizing_and_cash_cap,
        test_v2_is_strict_one_trade_and_waits_without_edge,
        test_ensure_race_does_not_reset_existing_books,
        test_v1_rejects_expensive_favorite_close_strike_and_fade,
    ]
    for fn in tests:
        fn()
        print("OK", fn.__name__)
    print("All race checks passed.")
