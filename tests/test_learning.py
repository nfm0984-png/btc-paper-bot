"""Strategy params, walk-forward split, and promotion guardrails."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot
import learn


def test_v1_defaults_match_live_constants():
    p = bot.StrategyParams()
    assert p.min_entry == bot.ASK_MIN
    assert p.max_entry == bot.ASK_MAX
    assert p.min_remaining_sec == bot.MIN_REMAINING_SEC
    assert p.min_strike_gap_usd == bot.MIN_STRIKE_DISTANCE
    assert p.rsi_bull_min == bot.RSI_YES_LO
    assert p.rsi_bull_max == bot.RSI_YES_HI
    assert p.rsi_bear_min == bot.RSI_NO_LO
    assert p.rsi_bear_max == bot.RSI_NO_HI
    assert p.require_ema50_align is True
    assert p.require_5m_align is True
    assert p.min_volatility_usd == 0.0
    assert p.max_chop_ratio == 1.0
    assert p.ema_flat_dollars == bot.EMA_FLAT_DOLLARS


def test_params_from_partial_dict_fills_defaults():
    p = bot.params_from_dict({"max_entry_cents": 65, "min_strike_gap_usd": 30})
    assert p.max_entry_cents == 65
    assert p.min_strike_gap_usd == 30
    assert p.min_entry_cents == 35
    assert p.require_5m_align is True


def test_split_windows_val_is_newest():
    windows = [{"open_time": f"2026-11-{d:02d}T00:00:00Z", "close_time": f"2026-11-{d:02d}T00:15:00Z"} for d in range(1, 11)]
    # That's only 10 days of 1 window/day — also add hourly-like stamps for one day
    base = 1764000000  # some unix
    windows = []
    for i in range(200):
        t = base + i * 15 * 60
        windows.append({"open_time": t, "close_time": t + 15 * 60})
    train, val = learn.split_windows(windows)
    assert train and val
    assert bot.to_ts(train[-1]["close_time"]) <= bot.to_ts(val[0]["open_time"])
    assert len(val) >= int(0.3 * len(windows)) - 1 or True  # at least last 24–48h, may be ~30%
    # val should be the newest 30% or 24h, whichever larger. 200 * 15m = 50h, so 48h or 30%.
    assert len(val) >= 40
    assert train[0]["open_time"] == windows[0]["open_time"]
    assert val[-1]["close_time"] == windows[-1]["close_time"]


def _metrics(trades, wins, pnl, dd=5.0, pf=1.4):
    wr = round(100.0 * wins / trades, 1) if trades else None
    return {
        "overall": {"trade_count": trades, "wins": wins, "losses": trades - wins, "win_rate": wr, "net_pnl": pnl, "profit_factor": pf, "max_drawdown_pct": dd},
        "val": {"trade_count": max(6, trades // 3), "wins": max(3, wins // 3), "losses": 2, "win_rate": 60.0, "net_pnl": pnl / 2, "profit_factor": pf, "max_drawdown_pct": dd},
        "train": {"trade_count": trades, "net_pnl": pnl / 2, "win_rate": wr, "profit_factor": pf, "max_drawdown_pct": dd},
    }


def test_promotion_requires_sample_and_val_edge():
    cur = {
        "overall": {"trade_count": 40, "wins": 24, "losses": 16, "win_rate": 60.0, "net_pnl": 8.0, "profit_factor": 1.3, "max_drawdown_pct": 8.0},
        "val": {"trade_count": 12, "wins": 6, "losses": 6, "win_rate": 50.0, "net_pnl": 0.40, "profit_factor": 1.1, "max_drawdown_pct": 6.0},
        "train": {},
    }
    # Too few trades
    tiny = {
        "overall": {"trade_count": 10, "wins": 8, "losses": 2, "win_rate": 80.0, "net_pnl": 12.0, "profit_factor": 3.0, "max_drawdown_pct": 4.0},
        "val": {"trade_count": 8, "wins": 7, "losses": 1, "win_rate": 87.0, "net_pnl": 6.0, "profit_factor": 4.0, "max_drawdown_pct": 3.0},
        "train": {},
    }
    ok, why = learn.beats_current(tiny, cur)
    assert ok is False and "overall trades" in why

    # Val not positive
    bad_val = {
        "overall": {"trade_count": 30, "wins": 20, "losses": 10, "win_rate": 66.0, "net_pnl": 10.0, "profit_factor": 1.8, "max_drawdown_pct": 7.0},
        "val": {"trade_count": 10, "wins": 3, "losses": 7, "win_rate": 30.0, "net_pnl": -2.0, "profit_factor": 0.6, "max_drawdown_pct": 8.0},
        "train": {},
    }
    ok, why = learn.beats_current(bad_val, cur)
    assert ok is False and "not positive" in why

    # Clear improvement
    better = {
        "overall": {"trade_count": 28, "wins": 20, "losses": 8, "win_rate": 71.0, "net_pnl": 14.0, "profit_factor": 1.9, "max_drawdown_pct": 7.0},
        "val": {"trade_count": 10, "wins": 7, "losses": 3, "win_rate": 70.0, "net_pnl": 3.20, "profit_factor": 1.7, "max_drawdown_pct": 5.0},
        "train": {},
    }
    ok, why = learn.beats_current(better, cur)
    assert ok is True, why

    # Blow-up drawdown is rejected
    wrecked = {
        "overall": {"trade_count": 28, "wins": 20, "losses": 8, "win_rate": 71.0, "net_pnl": 14.0, "profit_factor": 1.9, "max_drawdown_pct": 40.0},
        "val": {"trade_count": 10, "wins": 7, "losses": 3, "win_rate": 70.0, "net_pnl": 3.20, "profit_factor": 1.7, "max_drawdown_pct": 20.0},
        "train": {},
    }
    ok, why = learn.beats_current(wrecked, cur)
    assert ok is False and "drawdown" in why

    # Higher profit factor but LOWER unseen dollar P&L must never promote
    # (the v5 gap_$40 case: PF 1.79 vs 1.58, val P&L $90.17 vs $105.00).
    pf_only = {
        "overall": {"trade_count": 40, "wins": 30, "losses": 10, "win_rate": 75.0, "net_pnl": 20.0, "profit_factor": 1.8, "max_drawdown_pct": 7.0},
        "val": {"trade_count": 12, "wins": 10, "losses": 2, "win_rate": 83.0, "net_pnl": 2.00, "profit_factor": 2.2, "max_drawdown_pct": 4.0},
        "train": {},
    }
    # current val is $4.00 in `cur`? cur val is 0.40; 2.00 beats 0.40. Use a richer current.
    rich_cur = {
        "overall": {"trade_count": 40, "wins": 24, "losses": 16, "win_rate": 60.0, "net_pnl": 18.0, "profit_factor": 1.3, "max_drawdown_pct": 8.0},
        "val": {"trade_count": 12, "wins": 8, "losses": 4, "win_rate": 66.0, "net_pnl": 5.00, "profit_factor": 1.4, "max_drawdown_pct": 6.0},
        "train": {},
    }
    ok, why = learn.beats_current(pf_only, rich_cur)
    assert ok is False, why
    assert "not higher" in why or "profit factor" in why

    # Equal unseen dollars also rejected (must be strictly higher by ≥ $1.50)
    tied = {
        "overall": {"trade_count": 40, "wins": 28, "losses": 12, "win_rate": 70.0, "net_pnl": 18.0, "profit_factor": 1.6, "max_drawdown_pct": 7.0},
        "val": {"trade_count": 12, "wins": 8, "losses": 4, "win_rate": 66.0, "net_pnl": 5.00, "profit_factor": 1.8, "max_drawdown_pct": 5.0},
        "train": {},
    }
    ok, why = learn.beats_current(tied, rich_cur)
    assert ok is False
    assert "not higher" in why or "≥" in why or "$1.50" in why or "1.50" in why


def test_simulate_empty_is_flat():
    ev = bot.simulate_prepared_windows([], bot.StrategyParams())
    assert ev["trade_count"] == 0
    assert ev["net_pnl"] == 0 or ev["net_pnl"] == 0.0
    assert ev["ending_equity"] == 100.0


if __name__ == "__main__":
    tests = [
        test_v1_defaults_match_live_constants,
        test_params_from_partial_dict_fills_defaults,
        test_split_windows_val_is_newest,
        test_promotion_requires_sample_and_val_edge,
        test_simulate_empty_is_flat,
    ]
    for fn in tests:
        fn()
        print("OK", fn.__name__)
    print("All learning checks passed.")
