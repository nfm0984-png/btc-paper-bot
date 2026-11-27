#!/usr/bin/env python3
"""Hourly self-learning tuner for the Kalshi 15-minute BTC paper bot.

Walk-forward tests candidate parameter sets on a rolling 7–14 day history of
settled KXBTC15M windows. Promotes a new strategy version only when unseen
validation P&L is clearly better, so the live 5-minute bot can improve without
overfitting. Runs on GitHub Actions — $0 Grok Bot usage.

Does not touch data/state.json or reset any live paper session.
"""

from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Optional

import bot
import market_reader

LOG_MAX_ENTRIES = 200
VAL_MARGIN_USD = 1.50
VAL_PF_IMPROVE = 1.10  # unused: promotions require higher unseen dollar P&L, not PF alone
DD_SLACK_PCT = 4.0
DD_SLACK_MULT = 1.25
MAX_DD_HARD = 25.0


def signed_money(n: Optional[float]) -> str:
    if n is None:
        return "n/a"
    n = float(n)
    if n > 0:
        return f"+${n:.2f}"
    if n < 0:
        return f"−${abs(n):.2f}"
    return "$0.00"


def wr_label(m: Optional[dict]) -> str:
    if not m or m.get("win_rate") is None or not m.get("trade_count"):
        return "no trades"
    return f"{m['win_rate']:.1f}% WR on {m['trade_count']} trades"


def compact_metrics(m: dict) -> dict:
    return {
        "trade_count": m.get("trade_count", 0),
        "wins": m.get("wins", 0),
        "losses": m.get("losses", 0),
        "win_rate": m.get("win_rate"),
        "net_pnl": m.get("net_pnl"),
        "profit_factor": m.get("profit_factor"),
        "max_drawdown_pct": m.get("max_drawdown_pct"),
        "ending_equity": m.get("ending_equity"),
    }


def load_json(path, default):
    if not path.exists():
        return deepcopy(default)
    try:
        with path.open() as f:
            data = json.load(f)
        return data if data is not None else deepcopy(default)
    except Exception:
        return deepcopy(default)


def strategy_snapshot(cfg: dict) -> dict:
    params = bot.params_from_dict((cfg or {}).get("params"))
    return {
        "version": cfg.get("version", 1),
        "updated_at": cfg.get("updated_at"),
        "updated_at_pt": cfg.get("updated_at_pt"),
        "source": cfg.get("source"),
        "params": params.as_dict(),
        "plain_english": cfg.get("plain_english") or bot.plain_english_params(params),
        "metrics": cfg.get("metrics") or {},
        "chart_scorecard": cfg.get("chart_scorecard") or {},
        "last_promoted_at": cfg.get("last_promoted_at"),
        "last_promoted_at_pt": cfg.get("last_promoted_at_pt"),
        "v1": cfg.get("v1") or {},
    }


def merge_dashboard(cfg: dict, learning: Optional[dict]) -> bool:
    """Patch strategy + learning onto docs/data.json. Never touch session/live/state."""
    if not bot.DASHBOARD_PATH.exists() and learning is None:
        return False
    dash = bot.load_existing_dashboard()
    if not dash and not bot.DASHBOARD_PATH.exists():
        # Don't invent a dashboard from scratch — the 5-min bot owns that file.
        # Still write docs/learning.json from the caller.
        return False
    dash["strategy"] = strategy_snapshot(cfg)
    if learning is not None:
        dash["learning"] = learning
    bot.save_json(bot.DASHBOARD_PATH, dash)
    return True


def write_learning_payload(cfg: dict, learning: dict) -> None:
    payload = {
        "generated_at": learning.get("generated_at"),
        "generated_at_pt": learning.get("generated_at_pt"),
        "strategy": strategy_snapshot(cfg),
        "learning": learning,
    }
    bot.DOCS_DIR.mkdir(parents=True, exist_ok=True)
    bot.save_json(bot.DOCS_DIR / "learning.json", payload)
    merge_dashboard(cfg, learning)


PARAM_LABELS = {
    "min_entry_cents": ("min entry", lambda v: f"{int(v)}¢"),
    "max_entry_cents": ("max entry", lambda v: f"{int(v)}¢"),
    "min_strike_gap_usd": ("strike gap", lambda v: f"${float(v):.0f}"),
    "min_mins_left": ("min minutes left", lambda v: str(int(v))),
    "max_mins_left": ("max minutes left", lambda v: str(int(v))),
    "rsi_bull_min": ("bull RSI floor", lambda v: f"{float(v):.0f}"),
    "rsi_bull_max": ("bull RSI cap", lambda v: f"{float(v):.0f}"),
    "rsi_bear_min": ("bear RSI floor", lambda v: f"{float(v):.0f}"),
    "rsi_bear_max": ("bear RSI cap", lambda v: f"{float(v):.0f}"),
    "require_ema50_align": ("EMA-50 filter", lambda v: "on" if v else "off"),
    "require_5m_align": ("5-minute filter", lambda v: "on" if v else "off"),
    "min_volatility_usd": ("min 15m range", lambda v: f"${float(v):.0f}"),
    "max_chop_ratio": ("max chop", lambda v: f"{float(v):.2f}"),
    "ema_flat_dollars": ("flat-EMA band", lambda v: f"${float(v):.0f}"),
    "chart_mode": ("chart mode", lambda v: str(v)),
    "require_15m_align": ("15-minute filter", lambda v: "on" if v else "off"),
    "require_1h_align": ("1-hour filter", lambda v: "on" if v else "off"),
    "min_fair_edge": ("min fair edge", lambda v: f"{float(v)*100:.0f}¢"),
    "max_spread_cents": ("max spread", lambda v: f"{float(v):.0f}¢"),
    "enable_early_exits": ("early exits", lambda v: "on" if v else "off"),
    "tp_bid": ("take-profit bid", lambda v: f"{int(round(float(v)*100))}¢"),
    "tp_gain": ("take-profit gain", lambda v: f"+{int(round(float(v)*100))}¢"),
    "sl_drop": ("stop-loss drop", lambda v: f"{int(round(float(v)*100))}¢"),
    "tp_require_stall": ("TP needs stall", lambda v: "on" if v else "off"),
    "sl_require_reversal": ("SL needs reversal", lambda v: "on" if v else "off"),
    "fair_giveback": ("fair-value giveback", lambda v: "on" if v else "off"),
    "exit_mode": ("exit rule", lambda v: str(v)),
    "htf_gate": ("higher-timeframe gate", lambda v: str(v)),
    "require_vwap_side": ("VWAP-side filter", lambda v: "on" if v else "off"),
}

V1_PARAM_LABELS = {
    "min_ask_cents": ("min ask", lambda v: f"{int(v)}¢"),
    "max_ask_cents": ("max ask", lambda v: f"{int(v)}¢"),
    "min_strike_gap_usd": ("strike gap", lambda v: f"${float(v):.0f}"),
    "min_mins_left": ("min minutes left", lambda v: str(int(v))),
    "require_5m_confirm": ("5m confirm", lambda v: "on" if v else "off"),
    "htf_gate": ("higher-timeframe gate", lambda v: str(v)),
    "min_fair_edge": ("fair-value gate", lambda v: "off" if float(v) <= -0.999 else f"{float(v)*100:+.0f}¢"),
    "max_chop_ratio": ("max chop", lambda v: f"{float(v):.2f}"),
    "require_vwap_side": ("VWAP-side filter", lambda v: "on" if v else "off"),
    "min_aligns": ("min chart checks", lambda v: str(int(v))),
    "exit_mode": ("exit rule", lambda v: str(v)),
}


def describe_v1_diff(old: "bot.V1Params", new: "bot.V1Params") -> str:
    a, b = old.as_dict(), new.as_dict()
    parts = [f"{name} from {fmt(a[k])} to {fmt(b[k])}" for k, (name, fmt) in V1_PARAM_LABELS.items() if a.get(k) != b.get(k)]
    return ", ".join(parts) if parts else "the same rules"


def v1_candidate_grid(current: "bot.V1Params") -> list[tuple[str, "bot.V1Params"]]:
    """Tune Version 1.2 inside its method: one ticket per window, no opposite-side buys."""
    cands: list[tuple[str, bot.V1Params]] = [("current", current)]
    seen = {tuple(sorted(current.as_dict().items()))}

    def add(name: str, **overrides) -> None:
        d = current.as_dict()
        d.update(overrides)
        d["max_entries_per_window"] = 1
        d["allow_flip"] = False
        if d["min_ask_cents"] >= d["max_ask_cents"]:
            return
        p = bot.v1_params_from_dict(d)
        key = tuple(sorted(p.as_dict().items()))
        if key in seen:
            return
        seen.add(key)
        cands.append((name, p))

    for g in (20, 25, 30, 35, 40, 50):
        add(f"v1_gap_{g}", min_strike_gap_usd=g)
    for mx in (60, 62, 65, 68, 70, 75):
        add(f"v1_max_ask_{mx}", max_ask_cents=mx)
    for mn in (35, 40, 45):
        add(f"v1_min_ask_{mn}", min_ask_cents=mn)
    add("v1_5m_confirm", require_5m_confirm=not current.require_5m_confirm)
    for g in ("off", "15m", "1h", "both", "either"):
        add(f"v1_htf_{g}", htf_gate=g)
    for e in (-1.0, -0.03, 0.0, 0.02, 0.03):
        add(f"v1_fair_{e:+.2f}", min_fair_edge=e)
    for c in (1.0, 0.8, 0.7):
        add(f"v1_chop_{c:.2f}", max_chop_ratio=c)
    add("v1_vwap", require_vwap_side=not current.require_vwap_side)
    for a in (0, 3, 4, 5):
        add(f"v1_aligns_{a}", min_aligns=a)
    for m in ("hold", "reversal", "tp90_stall", "tp95"):
        add(f"v1_exit_{m}", exit_mode=m)
    return cands


def evaluate_v1(windows: list[dict], v1p: "bot.V1Params") -> dict:
    train, val = split_windows(windows)
    return {
        "overall": compact_metrics(bot.simulate_prepared_windows(windows, None, v1=v1p)),
        "train": compact_metrics(bot.simulate_prepared_windows(train, None, v1=v1p)),
        "val": compact_metrics(bot.simulate_prepared_windows(val, None, v1=v1p)),
        "train_windows": len(train),
        "val_windows": len(val),
    }


def learn_v1(cfg: dict, windows: list[dict], now) -> tuple[dict, dict]:
    """Walk-forward tune Version 1.2. Same promotion bar: strictly higher unseen dollars."""
    sec = dict(cfg.get("v1") or {})
    current = bot.v1_params_from_dict(sec.get("params")) if sec.get("params") else bot.default_v1_config_params()
    version = int(sec.get("version_n") or 1)
    scored = [(n, p, evaluate_v1(windows, p)) for n, p in v1_candidate_grid(current)]
    cur_ev = next(ev for n, _, ev in scored if n == "current")
    ranked = sorted(
        scored,
        key=lambda it: (
            float(it[2]["val"].get("net_pnl") or -9999),
            float(it[2]["overall"].get("net_pnl") or -9999),
            -float(it[2]["overall"].get("max_drawdown_pct") or 99),
        ),
        reverse=True,
    )
    best_name, best_params, best_ev = ranked[0]
    eligible = [(n, p, ev, why) for n, p, ev in ranked if n != "current" for ok, why in [beats_current(ev, cur_ev)] if ok]
    last_promo = sec.get("last_promoted_at")
    cooldown = bool(last_promo) and (int(now.timestamp()) - bot.to_ts(last_promo)) < bot.PROMOTION_COOLDOWN_SEC
    promoted = False
    if eligible and not cooldown:
        name, params, ev, why = eligible[0]
        promoted = True
        note = f"Version 1.2 learner promoted {describe_v1_diff(current, params)}: {why}."
        sec = {
            "label": f"{bot.V1_VERSION_LABEL} · {bot.V1_METHOD_NAME}",
            "version_n": version + 1,
            "params": params.as_dict(),
            "metrics": {k: ev[k] for k in ("overall", "train", "val", "train_windows", "val_windows")},
            "candidate": name,
            "rules": bot.v1_rules_text(params),
            "last_promoted_at": bot.iso_utc(now),
            "last_promoted_at_pt": bot.fmt_pt(now),
        }
    else:
        why = "promotion cooldown still active" if (eligible and cooldown) else (
            beats_current(best_ev, cur_ev)[1] if best_name != "current" else "no alternate beat the live Version 1.2 rules on unseen windows"
        )
        note = f"Kept Version 1.2 rules ({why})."
        sec.update({
            "label": f"{bot.V1_VERSION_LABEL} · {bot.V1_METHOD_NAME}",
            "version_n": version,
            "params": current.as_dict(),
            "metrics": {k: cur_ev[k] for k in ("overall", "train", "val", "train_windows", "val_windows")},
            "rules": bot.v1_rules_text(current),
        })
    sec["metrics_updated_at"] = bot.iso_utc(now)
    sec["metrics_updated_at_pt"] = bot.fmt_pt(now)
    report = {
        "promoted": promoted,
        "takeaway": note,
        "live": {k: cur_ev[k] for k in ("train", "val", "overall")},
        "best_candidate": {"name": best_name, "params": best_params.as_dict(), **{k: best_ev[k] for k in ("train", "val", "overall")}},
        "candidates_tested": len(scored),
    }
    return sec, report


def describe_diff(old: bot.StrategyParams, new: bot.StrategyParams) -> str:
    parts = []
    a, b = old.as_dict(), new.as_dict()
    for key, (name, fmt) in PARAM_LABELS.items():
        if a.get(key) != b.get(key):
            parts.append(f"{name} from {fmt(a[key])} to {fmt(b[key])}")
    if not parts:
        return "the same rules"
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return f"{parts[0]}, " + ", ".join(parts[1:-1]) + f", and {parts[-1]}"


def candidate_grid(current: bot.StrategyParams) -> list[tuple[str, bot.StrategyParams]]:
    cands: list[tuple[str, bot.StrategyParams]] = [("current", current)]
    seen = {tuple(sorted(current.as_dict().items()))}

    def add(name: str, **overrides) -> None:
        d = current.as_dict()
        d.update(overrides)
        # Keep entry band valid.
        if d["min_entry_cents"] >= d["max_entry_cents"]:
            return
        if d["min_mins_left"] >= d["max_mins_left"]:
            return
        p = bot.params_from_dict(d)
        key = tuple(sorted(p.as_dict().items()))
        if key in seen:
            return
        seen.add(key)
        cands.append((name, p))

    for mx in (62, 65, 68, 70, 72, 75):
        add(f"max_entry_{mx}", max_entry_cents=mx)
    for mn in (30, 32, 35, 38, 40):
        add(f"min_entry_{mn}", min_entry_cents=mn)
    for g in (15, 20, 25, 30, 40, 50):
        add(f"gap_{int(g)}", min_strike_gap_usd=g)
    for mn in (2, 3, 4):
        add(f"min_mins_{mn}", min_mins_left=mn)
    for mx in (10, 12, 14, 15):
        add(f"max_mins_{mx}", max_mins_left=mx)
    for lo, hi in ((50, 75), (52, 75), (54, 72), (55, 70)):
        add(f"rsi_bull_{lo}_{hi}", rsi_bull_min=lo, rsi_bull_max=hi)
    for lo, hi in ((25, 48), (25, 50), (28, 46), (30, 45)):
        add(f"rsi_bear_{lo}_{hi}", rsi_bear_min=lo, rsi_bear_max=hi)
    add("no_ema50", require_ema50_align=False)
    add("no_5m", require_5m_align=False)
    for v in (15, 25, 35):
        add(f"min_vol_{v}", min_volatility_usd=v)
    for c in (0.80, 0.70, 0.60):
        add(f"max_chop_{c:.2f}", max_chop_ratio=c)
    add("tighter_quality", max_entry_cents=65, min_strike_gap_usd=30, min_mins_left=3)
    add("avoid_chop", min_volatility_usd=20, max_chop_ratio=0.75, max_entry_cents=68)
    add("patient", min_mins_left=3, max_mins_left=12, min_strike_gap_usd=30)
    add("selective_rsi", rsi_bull_min=54, rsi_bear_max=46, max_entry_cents=68)
    add("cheap_only", max_entry_cents=62, min_strike_gap_usd=25)
    add("wider_entry", max_entry_cents=75, min_entry_cents=32)
    add(
        "strict",
        max_entry_cents=65,
        min_strike_gap_usd=40,
        min_mins_left=3,
        require_5m_align=True,
        require_ema50_align=True,
        max_chop_ratio=0.70,
        min_volatility_usd=20,
    )
    add("chart_1m", chart_mode="1m", require_5m_align=False, require_15m_align=False, require_1h_align=False)
    add("chart_1m_5m", chart_mode="1m_5m", require_5m_align=True, require_15m_align=False, require_1h_align=False)
    add("chart_1m_15m", chart_mode="1m_15m", require_5m_align=False, require_15m_align=True, require_1h_align=False)
    add("chart_1m_1h", chart_mode="1m_1h", require_5m_align=False, require_15m_align=False, require_1h_align=True)
    add("chart_mtf", chart_mode="mtf", require_5m_align=False, require_15m_align=False, require_1h_align=False)
    for edge in (0.03, 0.05, 0.08):
        add(f"fair_edge_{int(edge*100)}", min_fair_edge=edge)
    for sp in (3, 4, 6):
        add(f"spread_{sp}", max_spread_cents=sp)
    add("hold_to_expiry", enable_early_exits=False, exit_mode="hold")
    for m in ("reversal", "tp90_stall", "tp95", "reversal_tp90"):
        add(f"exit_{m}", exit_mode=m, enable_early_exits=False)
    for g in ("off", "15m", "1h", "either"):
        add(f"htf_{g}", htf_gate=g)
    add("vwap_side", require_vwap_side=not current.require_vwap_side)
    for edge in (0.0, 0.001, 0.01):
        add(f"fair_edge_{edge*100:.1f}c", min_fair_edge=edge)
    add("tp85_gain25", tp_bid=0.85, tp_gain=0.25, enable_early_exits=True)
    add("tp85_stall", tp_bid=0.85, tp_gain=0.25, tp_require_stall=True, enable_early_exits=True)
    add("tp90_gain35", tp_bid=0.90, tp_gain=0.35, enable_early_exits=True)
    add("sl_reversal", sl_require_reversal=True, enable_early_exits=True)
    add("fair_giveback", fair_giveback=True, enable_early_exits=True)
    add(
        "protective_exits",
        enable_early_exits=True,
        tp_bid=0.85,
        tp_gain=0.25,
        tp_require_stall=True,
        sl_require_reversal=True,
        sl_drop=0.12,
        fair_giveback=True,
    )
    add(
        "reader_quality",
        chart_mode="mtf",
        min_fair_edge=0.03,
        max_spread_cents=6,
        enable_early_exits=True,
        tp_bid=0.85,
        tp_gain=0.25,
        tp_require_stall=True,
        sl_require_reversal=True,
        fair_giveback=True,
    )
    return cands


def split_windows(windows: list[dict]) -> tuple[list[dict], list[dict]]:
    if not windows:
        return [], []
    if len(windows) < 8:
        # Too small to split meaningfully — treat newest 30% as val, rest train.
        cut = max(1, int(len(windows) * 0.70))
        cut = min(cut, len(windows) - 1)
        return windows[:cut], windows[cut:]
    times = [bot.to_ts(w.get("close_time") or w.get("open_time")) for w in windows]
    newest, oldest = max(times), min(times)
    cut_idx = int(len(windows) * 0.70)
    hours = 48 if (newest - oldest) >= 4 * 24 * 3600 else 24
    horizon = newest - hours * 3600
    time_idx = next((i for i, t in enumerate(times) if t >= horizon), len(windows) - 1)
    # Val is the newer slice: at least last 24–48h, and at least ~30% if that's larger.
    val_start = min(cut_idx, time_idx)
    val_start = max(1, min(val_start, len(windows) - 1))
    return windows[:val_start], windows[val_start:]


def prepare_window(
    market: dict,
    sticks: dict[str, list[dict]],
    candles_1m: list[dict],
    series_1m: list,
    candles_5m: list[dict],
    series_5m: list,
    candles_15m: Optional[list[dict]] = None,
    series_15m: Optional[list] = None,
    candles_1h: Optional[list[dict]] = None,
    series_1h: Optional[list] = None,
    include_exit_path: bool = True,
) -> Optional[dict]:
    if market.get("result") not in ("yes", "no"):
        return None
    if bot.dollars(market.get("floor_strike")) is None or not market.get("open_time") or not market.get("close_time"):
        return None
    open_ts = bot.to_ts(market["open_time"])
    decision_ts = open_ts + bot.DECISION_OFFSET_SEC
    q = bot.quote_at_decision(sticks.get(market["ticker"]) or [], open_ts)
    snap1 = bot.indicators_at(candles_1m, series_1m, decision_ts - 1)
    snap5 = bot.indicators_at(candles_5m, series_5m, decision_ts - 1)
    if not q or not snap1 or not snap1[1].ready:
        return None
    vol, chop = bot.window_chop_features(candles_1m, decision_ts - 1)
    close_ts = market["close_time"]
    remaining = bot.to_ts(close_ts) - decision_ts
    w = {
        "ticker": market["ticker"],
        "open_time": bot.iso_utc(market["open_time"]),
        "close_time": bot.iso_utc(close_ts),
        "open_time_pt": bot.fmt_pt(market["open_time"]),
        "close_time_pt": bot.fmt_pt(close_ts),
        "window_label": bot.contract_window_label(close_ts),
        "strike": bot.dollars(market["floor_strike"]),
        "result": market["result"],
        "expiration_value": bot.dollars(market.get("expiration_value")),
        "decision_ts": decision_ts,
        "btc_price": snap1[0]["close"],
        "yes_ask": q.get("yes_ask"),
        "yes_bid": q.get("yes_bid"),
        "no_ask": q.get("no_ask"),
        "no_bid": q.get("no_bid"),
        "remaining_sec": remaining,
        "ind_1m": bot.ind_payload(snap1[1]),
        "ind_5m": bot.ind_payload(snap5[1]) if snap5 else bot.ind_payload(None),
        "volatility_usd": vol,
        "chop_ratio": chop,
    }
    return enrich_window_features(
        w,
        candles_1m,
        series_1m,
        candles_15m,
        series_15m,
        candles_1h,
        series_1h,
        sticks=sticks.get(market["ticker"]) if include_exit_path else None,
    )


def window_incomplete(w: dict) -> bool:
    if not isinstance(w, dict):
        return True
    if not (w.get("ind_15m") or {}).get("ready"):
        return True
    if not (w.get("ind_1h") or {}).get("ready"):
        return True
    if w.get("fair_yes") is None:
        return True
    return False


def enrich_window_features(
    w: dict,
    candles_1m,
    series_1m,
    candles_15m,
    series_15m,
    candles_1h,
    series_1h,
    sticks: Optional[list] = None,
) -> dict:
    ts = int(w.get("decision_ts") or 0) - 1
    if candles_15m and series_15m:
        snap15 = bot.indicators_at(candles_15m, series_15m, ts)
        w["ind_15m"] = bot.ind_payload(snap15[1] if snap15 else None)
    if candles_1h and series_1h:
        snap1h = bot.indicators_at(candles_1h, series_1h, ts)
        w["ind_1h"] = bot.ind_payload(snap1h[1] if snap1h else None)
    sigma = market_reader.realized_vol_per_sqrt_min(candles_1m, ts) if candles_1m else w.get("sigma_usd")
    minutes = (w.get("remaining_sec") or 840) / 60.0
    fair = market_reader.fair_yes_probability(w.get("btc_price"), w.get("strike"), sigma, minutes)
    w["sigma_usd"] = None if sigma is None else round(float(sigma), 3)
    w["fair_yes"] = None if fair is None else round(float(fair), 4)
    w["yes_edge"] = market_reader.side_edge(fair, w.get("yes_ask"))
    w["no_edge"] = market_reader.side_edge(None if fair is None else 1.0 - float(fair), w.get("no_ask"))
    if w.get("yes_ask") is not None and w.get("yes_bid") is not None:
        w["spread_cents"] = round((float(w["yes_ask"]) - float(w["yes_bid"])) * 100.0, 1)
    if sticks is not None and candles_1m and series_1m:
        path = bot.build_exit_path(
            sticks,
            candles_1m,
            series_1m,
            bot.to_ts(w["open_time"]),
            bot.to_ts(w["close_time"]),
        )
        if path:
            w["exit_path"] = path
    return w


EXIT_PATH_BACKFILL = 500


def scorecard_rows(windows: list[dict], current: bot.StrategyParams) -> dict:
    """Walk-forward comparison of chart timeframes, market-reader gates, and exit rules."""
    studies: list[tuple[str, str, dict]] = [
        ("1m", "chart", {"chart_mode": "1m", "require_5m_align": False, "require_15m_align": False, "require_1h_align": False}),
        ("5m", "chart", {"chart_mode": "1m_5m", "require_5m_align": True, "require_15m_align": False, "require_1h_align": False}),
        ("15m", "chart", {"chart_mode": "1m_15m", "require_5m_align": False, "require_15m_align": True, "require_1h_align": False}),
        ("1h", "chart", {"chart_mode": "1m_1h", "require_5m_align": False, "require_15m_align": False, "require_1h_align": True}),
        ("multi-TF", "chart", {"chart_mode": "mtf", "require_5m_align": False, "require_15m_align": False, "require_1h_align": False}),
        ("no fair-edge gate", "reader", {"min_fair_edge": 0.0}),
        ("fair edge 3¢", "reader", {"min_fair_edge": 0.03}),
        ("fair edge 5¢", "reader", {"min_fair_edge": 0.05}),
        ("fair edge 8¢", "reader", {"min_fair_edge": 0.08}),
        ("spread ≤4¢", "reader", {"max_spread_cents": 4}),
        ("spread ≤6¢", "reader", {"max_spread_cents": 6}),
        ("hold to expiry", "exit", {"enable_early_exits": False}),
        ("current TP/SL", "exit", {"enable_early_exits": True, "tp_bid": 0.80, "tp_gain": 0.15, "sl_drop": 0.12, "tp_require_stall": False, "sl_require_reversal": False, "fair_giveback": False}),
        ("TP 85¢ / +25¢ + stall", "exit", {"enable_early_exits": True, "tp_bid": 0.85, "tp_gain": 0.25, "tp_require_stall": True}),
        ("TP 90¢ / +35¢", "exit", {"enable_early_exits": True, "tp_bid": 0.90, "tp_gain": 0.35}),
        ("SL only on reversal", "exit", {"enable_early_exits": True, "sl_require_reversal": True}),
        ("fair giveback", "exit", {"enable_early_exits": True, "fair_giveback": True}),
        ("protective exits", "exit", {"enable_early_exits": True, "tp_bid": 0.85, "tp_gain": 0.25, "tp_require_stall": True, "sl_require_reversal": True, "fair_giveback": True}),
    ]
    rows = []
    path_n = sum(1 for w in windows if w.get("exit_path"))
    for name, kind, overrides in studies:
        d = current.as_dict()
        d.update(overrides)
        ev = evaluate(windows, bot.params_from_dict(d))
        val = ev["val"]
        rows.append({
            "name": name,
            "kind": kind,
            "train": ev["train"],
            "val": val,
            "overall": ev["overall"],
        })
    def val_key(row):
        v = row["val"]
        return (
            float(v.get("net_pnl") or -9999),
            -float(row["overall"].get("max_drawdown_pct") or 99),
            float(v.get("win_rate") or 0),
        )

    def quality_ok(row):
        o = row["overall"]
        v = row["val"]
        if (o.get("trade_count") or 0) < 8:
            return False
        if (v.get("trade_count") or 0) < 6 and row["kind"] != "reader":
            return False
        if float(o.get("net_pnl") or 0) <= 0:
            return False
        if float(o.get("max_drawdown_pct") or 0) > 40:
            return False
        return True

    def pick(kind):
        group = [r for r in rows if r["kind"] == kind]
        qualified = [r for r in group if quality_ok(r)]
        pool = qualified or group
        return max(pool, key=val_key) if pool else None

    charts = [r for r in rows if r["kind"] == "chart"]
    exits = [r for r in rows if r["kind"] == "exit"]
    readers = [r for r in rows if r["kind"] == "reader"]
    best_chart = pick("chart")
    best_exit = pick("exit")
    best_reader = pick("reader")
    return {
        "windows": len(windows),
        "windows_with_exit_path": path_n,
        "rows": rows,
        "best_chart": None if not best_chart else best_chart["name"],
        "best_exit": None if not best_exit else best_exit["name"],
        "best_reader": None if not best_reader else best_reader["name"],
        "takeaway": (
            f"Best unseen chart is {best_chart['name']} "
            f"({signed_money(best_chart['val'].get('net_pnl'))}, {wr_label(best_chart['val'])}). "
            f"Best market-reader gate is {best_reader['name']} "
            f"({signed_money(best_reader['val'].get('net_pnl'))}). "
            f"Best exit rule is {best_exit['name']} "
            f"({signed_money(best_exit['val'].get('net_pnl'))}, {wr_label(best_exit['val'])})"
            + (f" on {path_n} windows with a full bid path." if path_n else ".")
        ) if best_chart and best_exit and best_reader else "Not enough windows to score charts yet.",
    }


def update_history(now: datetime) -> tuple[list[dict], dict]:
    existing = load_json(bot.HISTORY_WINDOWS_PATH, {"windows": [], "updated_at": None})
    by_ticker: dict[str, dict] = {}
    for w in existing.get("windows") or []:
        if isinstance(w, dict) and w.get("ticker"):
            by_ticker[w["ticker"]] = w

    cutoff = now - timedelta(days=bot.HISTORY_DAYS)
    cutoff_ts = int(cutoff.timestamp())
    print(f"  Fetching settled {bot.SERIES} markets for the last {bot.HISTORY_DAYS} days…", flush=True)
    settled = bot.fetch_kalshi_markets("settled", limit=200, max_pages=12)
    markets = [
        m
        for m in settled
        if m.get("result") in ("yes", "no")
        and m.get("open_time")
        and bot.to_ts(m["open_time"]) >= cutoff_ts
        and bot.dollars(m.get("floor_strike")) is not None
    ]
    markets.sort(key=lambda m: bot.to_ts(m["open_time"]))
    print(f"  Settled windows in range: {len(markets)}", flush=True)

    missing = [m for m in markets if m["ticker"] not in by_ticker]
    print(f"  Already stored: {len(by_ticker)}; new to feature: {len(missing)}", flush=True)

    incomplete = [
        m for m in markets
        if m["ticker"] in by_ticker and window_incomplete(by_ticker[m["ticker"]])
    ]
    missing_paths = [
        m for m in markets
        if m["ticker"] in by_ticker and len(by_ticker[m["ticker"]].get("exit_path") or []) < 5
    ]
    need_candles = bool(missing or incomplete or missing_paths or not by_ticker)
    src_1m = src_5m = src_15m = src_1h = "cached"
    if need_candles:
        oldest = min(bot.to_ts(m["open_time"]) for m in markets) if markets else int((now - timedelta(days=bot.HISTORY_DAYS)).timestamp())
        candle_start = oldest - bot.BACKTEST_CANDLE_PAD_HOURS * 3600
        candle_end = int(now.timestamp()) + 60
        print("  Downloading Coinbase/Kraken 1m, 5m, 15m, and 1h candles…", flush=True)
        candles_1m, src_1m = bot.fetch_btc_candles(candle_start, candle_end, 60)
        candles_5m, src_5m = bot.fetch_btc_candles(candle_start, candle_end, 300)
        candles_15m, src_15m = bot.fetch_btc_candles(candle_start, candle_end, 900)
        candles_1h, src_1h = bot.fetch_btc_candles(candle_start, candle_end, 3600)
        series_1m = bot.compute_indicators(candles_1m, vwap_lookback=30)
        series_5m = bot.compute_indicators(candles_5m, vwap_lookback=24)
        series_15m = bot.compute_indicators(candles_15m, vwap_lookback=20)
        series_1h = bot.compute_indicators(candles_1h, vwap_lookback=24)
        print(
            f"  1m: {len(candles_1m)} ({src_1m}); 5m: {len(candles_5m)} ({src_5m}); "
            f"15m: {len(candles_15m)} ({src_15m}); 1h: {len(candles_1h)} ({src_1h})",
            flush=True,
        )
        fetch_list = missing if by_ticker else markets
        path_targets = []
        if fetch_list:
            path_targets.extend(fetch_list)
        # Backfill exit paths for older windows, newest first, capped so the hourly job stays inside its timeout.
        extra_paths = [m for m in reversed(missing_paths) if m["ticker"] not in {x["ticker"] for x in path_targets}]
        path_targets.extend(extra_paths[:EXIT_PATH_BACKFILL])
        print(
            f"  Fetching Kalshi 15-minute quote paths for {len(path_targets)} windows "
            f"({len(fetch_list)} new, {len(path_targets) - len(fetch_list)} backfill)…",
            flush=True,
        )
        sticks = bot.fetch_candlesticks_for_markets(path_targets, first_minutes=15) if path_targets else {}
        built = 0
        for m in fetch_list:
            w = prepare_window(
                m, sticks, candles_1m, series_1m, candles_5m, series_5m,
                candles_15m, series_15m, candles_1h, series_1h, include_exit_path=True,
            )
            if w:
                by_ticker[w["ticker"]] = w
                built += 1
        print(f"  Featured {built} / {len(fetch_list)} new windows", flush=True)
        refreshed = 0
        for m in markets:
            w = by_ticker.get(m["ticker"])
            if not w:
                continue
            path_sticks = sticks.get(m["ticker"]) if m["ticker"] in {x["ticker"] for x in path_targets} else None
            if window_incomplete(w) or path_sticks:
                by_ticker[m["ticker"]] = enrich_window_features(
                    w, candles_1m, series_1m, candles_15m, series_15m, candles_1h, series_1h,
                    sticks=path_sticks,
                )
                refreshed += 1
        print(f"  Refreshed chart/fair/exit features on {refreshed} stored windows", flush=True)

    windows = [w for w in by_ticker.values() if bot.to_ts(w.get("close_time") or w.get("open_time")) >= cutoff_ts]
    windows.sort(key=lambda w: bot.to_ts(w.get("open_time")))
    # Hard cap so the repo stays small (~14 days of 15-minute windows).
    max_windows = bot.HISTORY_DAYS * 96 + 20
    if len(windows) > max_windows:
        windows = windows[-max_windows:]
    payload = {
        "updated_at": bot.iso_utc(now),
        "updated_at_pt": bot.fmt_pt(now),
        "days": bot.HISTORY_DAYS,
        "window_count": len(windows),
        "candle_source_1m": src_1m,
        "candle_source_5m": src_5m,
        "candle_source_15m": src_15m,
        "candle_source_1h": src_1h,
        "windows": windows,
    }
    bot.save_json(bot.HISTORY_WINDOWS_PATH, payload)
    print(f"  History now holds {len(windows)} settled windows", flush=True)
    return windows, payload


def evaluate(windows: list[dict], params: bot.StrategyParams) -> dict:
    train, val = split_windows(windows)
    overall = bot.simulate_prepared_windows(windows, params)
    train_m = bot.simulate_prepared_windows(train, params)
    val_m = bot.simulate_prepared_windows(val, params)
    return {
        "overall": compact_metrics(overall),
        "train": compact_metrics(train_m),
        "val": compact_metrics(val_m),
        "train_windows": len(train),
        "val_windows": len(val),
    }


def drawdown_ok(cand_dd: float, cur_dd: float) -> bool:
    cap = max(cur_dd * DD_SLACK_MULT, cur_dd + DD_SLACK_PCT, 8.0)
    if cand_dd <= cap:
        return True
    if cand_dd <= MAX_DD_HARD and cand_dd <= cur_dd + 8.0:
        return True
    return False


def beats_current(cand: dict, cur: dict) -> tuple[bool, str]:
    """Promote only when unseen validation *dollar* P&L is strictly better.

    A higher profit factor on fewer (or smaller) dollars is not enough. The
    candidate must: have enough trades, show positive unseen P&L, beat the
    live config's unseen dollar P&L by at least VAL_MARGIN_USD, have
    positive overall P&L, and keep drawdown acceptable.
    """
    o, v = cand["overall"], cand["val"]
    co, cv = cur["overall"], cur["val"]
    if (o.get("trade_count") or 0) < bot.MIN_PROMOTION_TRADES:
        return False, f"only {o.get('trade_count') or 0} overall trades (need {bot.MIN_PROMOTION_TRADES})"
    if (v.get("trade_count") or 0) < bot.MIN_PROMOTION_VAL_TRADES:
        return False, f"only {v.get('trade_count') or 0} unseen trades (need {bot.MIN_PROMOTION_VAL_TRADES})"
    val_pnl = float(v.get("net_pnl") or 0)
    cur_val_pnl = float(cv.get("net_pnl") or 0)
    if val_pnl <= 0:
        return False, f"unseen P&L is {signed_money(val_pnl)}, not positive"
    if val_pnl <= cur_val_pnl:
        return False, (
            f"unseen P&L {signed_money(val_pnl)} is not higher than live {signed_money(cur_val_pnl)} "
            "(will not promote a lower-dollar candidate because profit factor improved)"
        )
    margin = val_pnl - cur_val_pnl
    if margin < VAL_MARGIN_USD:
        return False, (
            f"unseen P&L {signed_money(val_pnl)} does not beat live {signed_money(cur_val_pnl)} "
            f"by ≥{signed_money(VAL_MARGIN_USD)}"
        )
    overall_pnl = float(o.get("net_pnl") or 0)
    if overall_pnl <= 0:
        return False, f"overall P&L is {signed_money(overall_pnl)}, not positive"
    cand_dd = float(o.get("max_drawdown_pct") or 0)
    cur_dd = float(co.get("max_drawdown_pct") or 0)
    if not drawdown_ok(cand_dd, cur_dd):
        return False, f"overall drawdown {cand_dd:.1f}% blows up vs live {cur_dd:.1f}%"
    return True, f"unseen P&L {signed_money(val_pnl)} vs live {signed_money(cur_val_pnl)}"


def takeaway(
    *,
    promoted: bool,
    version: int,
    new_version: int,
    current: bot.StrategyParams,
    best_params: bot.StrategyParams,
    best_name: str,
    cand: dict,
    cur: dict,
    reason: str,
    cooldown_block: bool,
) -> str:
    val = cand["val"]
    cur_val = cur["val"]
    delta = float(val.get("net_pnl") or 0) - float(cur_val.get("net_pnl") or 0)
    diff = describe_diff(current, best_params)
    if promoted:
        return (
            f"Changing {diff} improved unseen P&L by {signed_money(delta)} after fees "
            f"({wr_label(val)}). Promoted v{version} → v{new_version}."
        )
    if best_name == "current" or diff == "the same rules":
        return f"Current v{version} still leads on unseen windows — kept."
    if cooldown_block:
        return (
            f"A candidate that changed {diff} looked better on unseen windows "
            f"({signed_money(val.get('net_pnl'))} vs live {signed_money(cur_val.get('net_pnl'))}), "
            f"but a promotion landed too recently — kept v{version}."
        )
    return f"Current v{version} still leads on unseen windows — kept. Best alternate ({diff}) {reason}."


def run_learn() -> int:
    bot.DATA_DIR.mkdir(parents=True, exist_ok=True)
    bot.DOCS_DIR.mkdir(parents=True, exist_ok=True)
    now = bot.utc_now()
    print(f"[{bot.fmt_pt(now)}] Hourly learner starting…", flush=True)

    cfg = bot.load_strategy_config()
    if not bot.STRATEGY_CONFIG_PATH.exists():
        bot.save_json(bot.STRATEGY_CONFIG_PATH, cfg)
    current = bot.params_from_dict(cfg.get("params"))
    version = int(cfg.get("version") or 1)
    print(f"  Live strategy: v{version}", flush=True)

    windows, hist = update_history(now)
    train, val = split_windows(windows)
    print(
        f"  Walk-forward split: {len(train)} train / {len(val)} validation windows "
        f"(dataset {len(windows)})",
        flush=True,
    )

    print("  Scoring chart timeframes, market-reader gates, and exit rules…", flush=True)
    chart_scorecard = scorecard_rows(windows, current)
    print(f"  {chart_scorecard.get('takeaway')}", flush=True)

    grid = candidate_grid(current)
    print(f"  Testing {len(grid)} parameter sets (fees + flat $4–$8, no press)…", flush=True)

    scored: list[tuple[str, bot.StrategyParams, dict]] = []
    for name, params in grid:
        ev = evaluate(windows, params)
        scored.append((name, params, ev))

    current_ev = next(ev for name, _, ev in scored if name == "current")

    print("  Tuning Version 1.2 (chart-confirmed active) on the same walk-forward split…", flush=True)
    try:
        v1_section, v1_report = learn_v1(cfg, windows, now)
        print(f"  {v1_report['takeaway']}", flush=True)
    except Exception as exc:  # never let the V1 tuner break the V2 learner
        print(f"  Version 1.2 tuner skipped: {exc}", flush=True)
        v1_section, v1_report = (cfg.get("v1") if isinstance(cfg.get("v1"), dict) else None), None
    # Best by validation P&L, then overall P&L, then lower drawdown, then more val trades.
    def sort_key(item):
        name, params, ev = item
        v, o = ev["val"], ev["overall"]
        return (
            float(v.get("net_pnl") or -9999),
            float(o.get("net_pnl") or -9999),
            -float(o.get("max_drawdown_pct") or 99),
            int(v.get("trade_count") or 0),
        )

    ranked = sorted(scored, key=sort_key, reverse=True)
    best_name, best_params, best_ev = ranked[0]

    eligible = []
    reject_notes = []
    for name, params, ev in ranked:
        if name == "current":
            continue
        ok, why = beats_current(ev, current_ev)
        if ok:
            eligible.append((name, params, ev, why))
        elif name == best_name:
            reject_notes.append(why)

    last_promo = cfg.get("last_promoted_at")
    cooldown_block = False
    if last_promo:
        elapsed = int(now.timestamp()) - bot.to_ts(last_promo)
        if elapsed < bot.PROMOTION_COOLDOWN_SEC:
            cooldown_block = True

    promoted = False
    decision = f"KEPT v{version}"
    new_version = version
    reason = reject_notes[0] if reject_notes else "no alternate beat the live rules on unseen windows"
    pick_name, pick_params, pick_ev = best_name, best_params, best_ev

    if eligible and not cooldown_block:
        pick_name, pick_params, pick_ev, reason = eligible[0]
        new_version = version + 1
        promoted = True
        decision = f"PROMOTED v{version} → v{new_version}"
        cfg = {
            "version": new_version,
            "updated_at": bot.iso_utc(now),
            "updated_at_pt": bot.fmt_pt(now),
            "source": "hourly-learner",
            "params": pick_params.as_dict(),
            "metrics": {
                "overall": pick_ev["overall"],
                "train": pick_ev["train"],
                "val": pick_ev["val"],
                "windows": len(windows),
                "train_windows": pick_ev["train_windows"],
                "val_windows": pick_ev["val_windows"],
                "promoted_from": version,
                "candidate": pick_name,
            },
            "plain_english": bot.plain_english_params(pick_params),
            "chart_scorecard": chart_scorecard,
            "last_promoted_at": bot.iso_utc(now),
            "last_promoted_at_pt": bot.fmt_pt(now),
        }
        if v1_section is not None:
            cfg["v1"] = v1_section
        bot.save_json(bot.STRATEGY_CONFIG_PATH, cfg)
        print(f"  {decision}: {reason}", flush=True)
    else:
        if cooldown_block and eligible:
            pick_name, pick_params, pick_ev, reason = eligible[0]
            reason = "promotion cooldown still active"
        # Keep live params, but always refresh metrics so a sizing change
        # (e.g. flat $4–$8, no press) shows up even when we don't promote.
        prev_metrics = cfg.get("metrics") if isinstance(cfg.get("metrics"), dict) else {}
        cfg.setdefault("version", version)
        cfg["params"] = current.as_dict()
        cfg["plain_english"] = bot.plain_english_params(current)
        cfg.setdefault("updated_at", bot.iso_utc(now))
        cfg.setdefault("updated_at_pt", bot.fmt_pt(now))
        cfg.setdefault("source", "hourly-learner")
        cfg["metrics"] = {
            "overall": current_ev["overall"],
            "train": current_ev["train"],
            "val": current_ev["val"],
            "windows": len(windows),
            "train_windows": current_ev["train_windows"],
            "val_windows": current_ev["val_windows"],
            "promoted_from": prev_metrics.get("promoted_from"),
            "candidate": prev_metrics.get("candidate") or "current",
            "metrics_updated_at": bot.iso_utc(now),
            "metrics_updated_at_pt": bot.fmt_pt(now),
        }
        cfg["chart_scorecard"] = chart_scorecard
        if v1_section is not None:
            cfg["v1"] = v1_section
        bot.save_json(bot.STRATEGY_CONFIG_PATH, cfg)
        print(f"  {decision}: {reason}", flush=True)

    note = takeaway(
        promoted=promoted,
        version=version,
        new_version=new_version,
        current=current,
        best_params=pick_params if promoted else best_params,
        best_name=pick_name if promoted else best_name,
        cand=pick_ev if promoted else best_ev,
        cur=current_ev,
        reason=reason,
        cooldown_block=cooldown_block and not promoted,
    )
    print(f"  Takeaway: {note}", flush=True)
    print(
        f"  Live v{version} train {signed_money(current_ev['train'].get('net_pnl'))} / "
        f"val {signed_money(current_ev['val'].get('net_pnl'))} ({wr_label(current_ev['val'])})",
        flush=True,
    )
    print(
        f"  Best tested ({best_name}) train {signed_money(best_ev['train'].get('net_pnl'))} / "
        f"val {signed_money(best_ev['val'].get('net_pnl'))} ({wr_label(best_ev['val'])})",
        flush=True,
    )

    entry = {
        "t": bot.iso_utc(now),
        "t_pt": bot.fmt_pt(now),
        "windows": len(windows),
        "train_windows": current_ev["train_windows"],
        "val_windows": current_ev["val_windows"],
        "candidates_tested": len(grid),
        "live_version": version,
        "live": {
            "train": current_ev["train"],
            "val": current_ev["val"],
            "overall": current_ev["overall"],
        },
        "best_candidate": {
            "name": best_name,
            "params": best_params.as_dict(),
            "train": best_ev["train"],
            "val": best_ev["val"],
            "overall": best_ev["overall"],
        },
        "promoted_to": new_version if promoted else None,
        "promoted_params": pick_params.as_dict() if promoted else None,
        "decision": decision,
        "takeaway": note,
        "reason": reason,
        "chart_scorecard": {
            "best_chart": chart_scorecard.get("best_chart"),
            "best_exit": chart_scorecard.get("best_exit"),
            "best_reader": chart_scorecard.get("best_reader"),
            "takeaway": chart_scorecard.get("takeaway"),
            "windows_with_exit_path": chart_scorecard.get("windows_with_exit_path"),
        },
    }

    if v1_report:
        entry["v1"] = {"takeaway": v1_report.get("takeaway"), "promoted": v1_report.get("promoted"), "live_val": v1_report["live"]["val"], "best": v1_report["best_candidate"]["name"]}
    log = load_json(bot.LEARNING_LOG_PATH, {"runs": []})
    runs = list(log.get("runs") or [])
    runs.append(entry)
    runs = runs[-LOG_MAX_ENTRIES:]
    log_payload = {
        "updated_at": bot.iso_utc(now),
        "updated_at_pt": bot.fmt_pt(now),
        "runs": runs,
    }
    bot.save_json(bot.LEARNING_LOG_PATH, log_payload)

    live_cfg = bot.load_strategy_config()
    learning = {
        "generated_at": bot.iso_utc(now),
        "generated_at_pt": bot.fmt_pt(now),
        "windows": len(windows),
        "history_days": bot.HISTORY_DAYS,
        "train_windows": current_ev["train_windows"],
        "val_windows": current_ev["val_windows"],
        "live_version": live_cfg.get("version", new_version if promoted else version),
        "decision": decision,
        "takeaway": note,
        "chart_scorecard": chart_scorecard,
        "v1": v1_report,
        "last_run": entry,
        "runs": list(reversed(runs[-48:])),  # newest first for the dashboard
    }
    write_learning_payload(live_cfg, learning)
    print("  Wrote strategy_config, learning_log, docs/learning.json, and merged docs/data.json", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Hourly Kalshi 15m strategy learner")
    parser.add_argument(
        "--merge-dashboard",
        action="store_true",
        help="Re-apply strategy + learning onto docs/data.json without fetching or retraining.",
    )
    args = parser.parse_args()
    if args.merge_dashboard:
        cfg = bot.load_strategy_config() if bot.STRATEGY_CONFIG_PATH.exists() else bot.default_strategy_config()
        log = load_json(bot.LEARNING_LOG_PATH, {})
        existing = bot.load_existing_dashboard().get("learning")
        learning_file = load_json(bot.DOCS_DIR / "learning.json", {})
        learning = (learning_file or {}).get("learning") or existing
        if learning is None and (log.get("runs")):
            last = log["runs"][-1]
            learning = {
                "generated_at": last.get("t"),
                "generated_at_pt": last.get("t_pt"),
                "windows": last.get("windows"),
                "history_days": bot.HISTORY_DAYS,
                "train_windows": last.get("train_windows"),
                "val_windows": last.get("val_windows"),
                "live_version": cfg.get("version"),
                "decision": last.get("decision"),
                "takeaway": last.get("takeaway"),
                "last_run": last,
                "runs": list(reversed(log["runs"][-48:])),
            }
        if learning is None and not bot.STRATEGY_CONFIG_PATH.exists():
            print("  No learning artifacts to merge.", flush=True)
            return 0
        write_learning_payload(cfg, learning or existing or {
            "generated_at": cfg.get("updated_at"),
            "generated_at_pt": cfg.get("updated_at_pt"),
            "windows": 0,
            "history_days": bot.HISTORY_DAYS,
            "live_version": cfg.get("version", 1),
            "decision": f"KEPT v{cfg.get('version', 1)}",
            "takeaway": "Learner has not completed a full hourly run yet.",
            "runs": [],
        })
        print("  Merged strategy + learning into the dashboard.", flush=True)
        return 0
    return run_learn()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr, flush=True)
        raise
