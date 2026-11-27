#!/usr/bin/env python3
"""Version 1.2 (chart-confirmed active) / Version 2.2 gates, smart exits, and the Race 2 reset."""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot  # noqa: E402
from tests.test_race import bear, bull  # noqa: E402

V12 = bot.default_v1_config_params()


def _sig(**kw):
    base = dict(
        ind_1m=bull(), ind_5m=bull(ema9=60510, ema21=60460, rsi14=58, macd_hist=6, close=60520),
        btc_price=60540, strike=60400, yes_ask=0.58, yes_bid=0.56, no_ask=0.43, no_bid=0.41,
        remaining_sec=12 * 60, open_positions=[], window_trade_count=0, v1=V12,
        features={"fair_yes": 0.70, "ind_15m": bull(), "ind_1h": bull()},
    )
    base.update(kw)
    return bot.make_v1_signal(**base)


def test_v12_buys_when_charts_and_fair_value_agree():
    sig = _sig()
    assert sig.action == "BUY_YES", sig


def test_v12_blocks_when_15m_points_against():
    sig = _sig(features={"fair_yes": 0.70, "ind_15m": bear(), "ind_1h": bull()})
    assert sig.action == "WAIT" and "15-minute" in sig.reason, sig


def test_v12_blocks_rich_ask_vs_fair_value():
    sig = _sig(features={"fair_yes": 0.50, "ind_15m": bull(), "ind_1h": bull()})
    assert sig.action == "WAIT" and "fair value" in sig.reason, sig


def test_v12_band_and_gap():
    assert _sig(yes_ask=0.68).action != "BUY_YES"  # above the 65¢ cap
    assert _sig(btc_price=60420).action != "BUY_YES"  # inside the $30 gap


def test_v12_one_per_window_and_no_opposite_side():
    held = {"ticker": "T", "side": "yes", "entry_price": 0.58, "contracts": 8}
    capped = _sig(open_positions=[held], window_trade_count=1, ticker="T")
    assert capped.action == "HOLD", capped
    loose = bot.V1Params(**{**V12.as_dict(), "max_entries_per_window": 3})
    flip = _sig(
        v1=loose, ind_1m=bear(), ind_5m=bear(), btc_price=60290, yes_ask=0.40, no_ask=0.61,
        open_positions=[held], window_trade_count=1, ticker="T",
        features={"fair_yes": 0.2, "ind_15m": bear(), "ind_1h": bear()},
    )
    assert flip.action == "HOLD" and "opposite" in flip.reason, flip


def test_smart_reversal_exit_uses_bid():
    pos = {"side": "yes", "entry_price": 0.60, "contracts": 8}
    ind = bot.Indicators(ema9=100, ema21=110, ema50=110, rsi14=40, macd_hist=-3, close=95, ready=True)
    sig = bot.smart_exit_signal(pos, 0.30, ind, 60380, 60400, 300, "reversal")
    assert sig and sig.action == "CUT_LOSS" and sig.bid == 0.30
    assert bot.smart_exit_signal(pos, 0.30, ind, 60420, 60400, 300, "reversal") is None  # strike not broken
    assert bot.smart_exit_signal(pos, 0.30, ind, 60380, 60400, 300, "hold") is None


def test_race_reset_archives_previous_race():
    now = bot.utc_now()
    old = bot.new_race(now - timedelta(hours=5), 8)
    old["v1"]["cash"] = 48.49
    old["v1"]["equity"] = 48.49
    old["v1"]["trades"] = [{"ticker": "A", "status": "closed", "pnl": -5.0}]
    old["v2"]["cash"] = 84.83
    old["v2"]["equity"] = 84.83
    state = {"race": old}
    race = bot.ensure_race(state, now, 9)
    assert race["v1"]["cash"] == 100.0 and race["v2"]["cash"] == 100.0
    assert race["v1"]["trades"] == [] and race["v2"]["open_positions"] == []
    assert race["reset_id"] == bot.RACE_RESET_ID and race["race_number"] == 2
    arc = state["race_archive"][-1]
    assert arc["race_number"] == 1
    assert arc["books"]["v1"]["equity"] == 48.49 and arc["books"]["v2"]["equity"] == 84.83
    assert "Race 1, ended" in arc["label"] and "V1 $48.49" in arc["label"]
    # Idempotent: a second tick does not reset again.
    state["race"]["v1"]["cash"] = 97.0
    again = bot.ensure_race(state, now + timedelta(minutes=5), 9)
    assert again["v1"]["cash"] == 97.0 and len(state["race_archive"]) == 1


def test_simulate_v1_runs():
    m = bot.simulate_prepared_windows([], None, v1=V12)
    assert m["trade_count"] == 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("OK", name)
    print("All v1.2/v2.2 checks passed.")
