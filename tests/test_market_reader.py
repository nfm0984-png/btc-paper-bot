"""Chart reading, fair-probability edge, and early-exit simulation."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot
import market_reader


def test_fair_probability_moves_with_strike_and_time():
    close = market_reader.fair_yes_probability(61520, 61500, 25.0, 10)
    far = market_reader.fair_yes_probability(61520, 61500, 25.0, 1)
    below = market_reader.fair_yes_probability(61480, 61500, 25.0, 10)
    assert close is not None and far is not None and below is not None
    assert 0.5 < close < 1.0
    assert far > close  # less time left, already above the strike, should be more certain
    assert below < 0.5
    assert market_reader.normal_cdf(0) == 0.5


def test_chart_bias_and_mtf_consensus():
    up = bot.Indicators(ema9=100, ema21=80, ema50=70, rsi14=60, macd_hist=8, close=110, ready=True)
    down = bot.Indicators(ema9=80, ema21=100, ema50=110, rsi14=40, macd_hist=-8, close=70, ready=True)
    assert market_reader.chart_bias(up) == "up"
    assert market_reader.chart_bias(down) == "down"
    snaps = {
        "1m": market_reader.chart_snapshot("1m", up),
        "5m": market_reader.chart_snapshot("5m", up),
        "15m": market_reader.chart_snapshot("15m", up),
        "1h": market_reader.chart_snapshot("1h", down),
    }
    cons = market_reader.multi_tf_consensus(snaps, side="yes")
    assert cons["bias"] == "up"
    assert cons["higher_agree"] == 2  # 5m + 15m
    assert market_reader.tf_agrees(up, "yes") is True
    assert market_reader.tf_agrees(down, "yes") is False


def test_min_fair_edge_blocks_rich_asks():
    ind = bot.Indicators(ema9=60520, ema21=60440, ema50=60380, rsi14=62, macd_hist=12, close=60540, ready=True)
    base = dict(
        ind_1m=ind,
        ind_5m=ind,
        btc_price=60540,
        strike=60400,
        yes_ask=0.60,
        yes_bid=0.58,
        no_ask=0.42,
        no_bid=0.40,
        remaining_sec=12 * 60,
    )
    # No edge gate: this is a clean Yes buy under default 1m_5m rules.
    open_sig = bot.make_signal(**base, params=bot.StrategyParams(max_entry_cents=75, min_fair_edge=0.0), features={"fair_yes": 0.62})
    assert open_sig.action == "BUY_YES", open_sig
    # Fair 62¢ vs 60¢ ask is only 2¢ of edge, so a 5¢ gate should block.
    blocked = bot.make_signal(**base, params=bot.StrategyParams(max_entry_cents=75, min_fair_edge=0.05), features={"fair_yes": 0.62})
    assert blocked.action == "WAIT", blocked
    assert "fair" in blocked.reason.lower() or "edge" in blocked.reason.lower()


def test_15m_align_can_block():
    bull = bot.Indicators(ema9=60520, ema21=60440, ema50=60380, rsi14=62, macd_hist=12, close=60540, ready=True)
    bear = bot.Indicators(ema9=60320, ema21=60420, ema50=60480, rsi14=38, macd_hist=-11, close=60290, ready=True)
    sig = bot.make_signal(
        ind_1m=bull,
        ind_5m=bull,
        btc_price=60540,
        strike=60400,
        yes_ask=0.55,
        yes_bid=0.53,
        no_ask=0.47,
        no_bid=0.45,
        remaining_sec=12 * 60,
        params=bot.StrategyParams(chart_mode="1m_15m", require_5m_align=False, require_15m_align=True, max_entry_cents=75),
        features={"ind_15m": bear},
    )
    assert sig.action == "WAIT"
    assert "15-minute" in sig.reason


def test_hold_to_expiry_vs_take_profit_on_exit_path():
    window = {
        "ticker": "KXBTC15M-TEST-PATH",
        "open_time": "2026-11-27T01:00:00Z",
        "close_time": "2026-11-27T01:15:00Z",
        "decision_ts": "2026-11-27T01:01:00Z",
        "btc_price": 60540,
        "strike": 60400,
        "result": "no",  # would have lost if held to expiry
        "expiration_value": 60300,
        "yes_ask": 0.50,
        "yes_bid": 0.48,
        "no_ask": 0.52,
        "no_bid": 0.50,
        "remaining_sec": 840,
        "ind_1m": {"ema9": 60520, "ema21": 60440, "ema50": 60380, "rsi14": 62, "macd_hist": 12, "close": 60540, "ready": True},
        "ind_5m": {"ema9": 60510, "ema21": 60460, "ema50": 60390, "rsi14": 58, "macd_hist": 6, "close": 60520, "ready": True},
        "volatility_usd": 80,
        "chop_ratio": 0.4,
        "fair_yes": 0.72,
        "exit_path": [
            [1764205320, 0.55, 0.57, 60580, 60540, 60450, 10],
            [1764205440, 0.70, 0.72, 60620, 60580, 60480, 8],
            [1764205560, 0.86, 0.88, 60680, 60600, 60500, 5],
        ],
    }
    # Force timestamps in the path to be after decision (01:01Z = 1764205260 is wrong year).
    # decision_ts 2026-11-27T01:01:00Z
    decision = bot.to_ts(window["decision_ts"])
    window["exit_path"] = [
        [decision + 120, 0.55, 0.57, 60580, 60540, 60450, 10],
        [decision + 240, 0.70, 0.72, 60620, 60580, 60480, 8],
        [decision + 360, 0.86, 0.88, 60680, 60600, 60500, 5],
    ]
    hold = bot.simulate_prepared_windows([window], bot.StrategyParams(max_entry_cents=75, enable_early_exits=False))
    tp = bot.simulate_prepared_windows([window], bot.StrategyParams(max_entry_cents=75, enable_early_exits=True, tp_bid=0.80, tp_gain=0.25))
    assert hold["trade_count"] == 1
    assert hold["net_pnl"] < 0  # held a Yes into a No settlement
    assert tp["trade_count"] == 1
    assert tp["trades"][0]["outcome"] == "early_exit"
    assert tp["trades"][0]["pnl_kind"] == "tp"
    assert tp["net_pnl"] > hold["net_pnl"]


def test_kalshi_book_metrics_imbalance():
    raw = {
        "orderbook_fp": {
            "yes_dollars": [["0.40", "100"], ["0.49", "500"]],
            "no_dollars": [["0.40", "50"], ["0.50", "80"]],
        }
    }
    m = market_reader.kalshi_book_metrics(raw, yes_bid=0.49, yes_ask=0.50)
    assert m["ok"] is True
    assert m["spread_cents"] == 1.0
    assert m["yes_depth"] > m["no_depth"]
    assert m["bias"] == "yes"


if __name__ == "__main__":
    tests = [
        test_fair_probability_moves_with_strike_and_time,
        test_chart_bias_and_mtf_consensus,
        test_min_fair_edge_blocks_rich_asks,
        test_15m_align_can_block,
        test_hold_to_expiry_vs_take_profit_on_exit_path,
        test_kalshi_book_metrics_imbalance,
    ]
    for fn in tests:
        fn()
        print("OK", fn.__name__)
    print("All market-reader checks passed.")
