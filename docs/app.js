(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));

  const money = (n, sign = false) => {
    if (n == null || Number.isNaN(n)) return "—";
    const abs = Math.abs(n).toLocaleString("en-US", { style: "currency", currency: "USD" });
    if (!sign) return n < 0 ? "−" + abs : abs;
    if (n > 0) return "+" + abs;
    if (n < 0) return "−" + abs;
    return abs;
  };
  const pct = (n) => {
    if (n == null || Number.isNaN(n)) return "—";
    const v = Math.abs(n).toFixed(2) + "%";
    if (n > 0) return "+" + v;
    if (n < 0) return "−" + v;
    return v;
  };
  const px = (n) => n == null ? "—" : "$" + Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const cents = (n) => n == null ? "—" : Math.round(n * 100) + "¢";
  const num = (n, d = 2) => n == null || Number.isNaN(n) ? "—" : Number(n).toLocaleString("en-US", { maximumFractionDigits: d });

  function totalContracts(book) {
    const trades = (book && book.trades) || [];
    let n = trades.reduce((s, t) => s + (Number(t.contracts) || 0), 0);
    if (!trades.length && book && book.position) n = Number(book.position.contracts) || 0;
    return n;
  }
  function contractsLabel(n) {
    const c = Number(n) || 0;
    return c + (c === 1 ? " contract" : " contracts");
  }
  function dedupeTrades(trades) {
    const best = new Map();
    const order = [];
    (trades || []).forEach((t) => {
      if (!t) return;
      const key = (t.ticker || "") + "|" + (t.entry_time || "") + "|" + (t.side || "");
      const prev = best.get(key);
      if (!prev) { best.set(key, t); order.push(key); }
      else if (t.status === "closed" || (prev.status !== "closed" && t.pnl != null)) best.set(key, t);
    });
    return order.map((k) => best.get(k));
  }
  function amountBoughtOf(t) {
    if (!t) return 0;
    const n = t.amount_bought != null ? t.amount_bought : t.total_in;
    return n == null ? 0 : Number(n);
  }
  function bookAmountBought(book) {
    if (!book) return 0;
    if (book.amount_bought != null) return Number(book.amount_bought);
    const rows = dedupeTrades(book.trades || []);
    if (!rows.length && book.position) return amountBoughtOf(book.position);
    return rows.reduce((s, t) => s + amountBoughtOf(t), 0);
  }
  function bookContracts(book) {
    if (!book) return 0;
    if (book.contracts_total != null) return Number(book.contracts_total);
    const rows = dedupeTrades(book.trades || []);
    if (!rows.length && book.position) return Number(book.position.contracts || 0);
    return rows.reduce((s, t) => s + (Number(t.contracts) || 0), 0);
  }
  function tradePnl(t, market) {
    if (!t) return null;
    if (t.status !== "open" && t.pnl != null) return Number(t.pnl);
    const bid = market ? (t.side === "yes" ? market.yes_bid : market.no_bid) : null;
    const bought = amountBoughtOf(t);
    if ((t.status === "open" || t.pnl == null) && bid != null && t.contracts != null && bought) {
      return Number(t.contracts) * Number(bid) - bought;
    }
    return t.pnl == null ? null : Number(t.pnl);
  }
  function pressTag(t) {
    return t && t.pressed ? '<span class="tag">2× PRESS</span>' : "";
  }
  function openTradeLine(p, prefix, market) {
    if (!p) return "";
    const side = (p.side || "").toUpperCase();
    const bought = amountBoughtOf(p);
    const pnl = tradePnl(p, market);
    const head = prefix ? `<strong>${esc(prefix)}</strong>` : "<strong>Open trade</strong>";
    const bits = [
      side ? esc(side) : null,
      contractsLabel(p.contracts) + " @ " + cents(p.entry_price),
      p.ticker ? esc(p.ticker) : null,
      p.entry_time_pt ? ("entered " + esc(p.entry_time_pt)) : null,
    ].filter(Boolean);
    const facts = [
      bought ? `<span class="fact-bought">Bought ${esc(money(bought))}</span>` : null,
      pnl != null ? `<span class="${pnl >= 0 ? "up" : "down"}">${pnl >= 0 ? "Gain" : "Lost"} ${esc(money(pnl, true))}</span>` : null,
    ].filter(Boolean).join(" · ");
    return `${head}${pressTag(p)} · ${bits.join(" · ")}<div class="open-facts">${facts}</div>`;
  }
  function longDatePt(d) {
    try {
      return new Intl.DateTimeFormat("en-US", {
        timeZone: "America/Los_Angeles",
        weekday: "long", month: "long", day: "numeric", year: "numeric"
      }).format(d || new Date());
    } catch (e) { return ""; }
  }

  let DATA = null;
  let liveBtc = null;
  let liveInd = null;
  let closeTimer = null;
  let sessionTimer = null;

  const TABS = ["live", "trades", "learning", "backtest", "rules"];
  let TRADE_FILTER = "all";

  function parseHash() {
    const h = (location.hash || "#live").slice(1);
    if (h === "trades-v1" || h === "trades/v1") return { tab: "trades", filter: "v1" };
    if (h === "trades-v2" || h === "trades/v2") return { tab: "trades", filter: "v2" };
    if (h === "trades-all" || h === "trades") return { tab: "trades", filter: "all" };
    if (TABS.includes(h)) return { tab: h, filter: null };
    return { tab: "live", filter: null };
  }

  function tradesHash(filter) {
    if (filter === "v1") return "#trades-v1";
    if (filter === "v2") return "#trades-v2";
    return "#trades";
  }

  function setTradeFilter(filter, opts) {
    const next = (filter === "v1" || filter === "v2") ? filter : "all";
    TRADE_FILTER = next;
    ["all", "v1", "v2"].forEach((id) => {
      const btn = $("filter-" + id);
      if (btn) btn.setAttribute("aria-pressed", id === next ? "true" : "false");
    });
    if (DATA) renderRaceTradeLog((DATA.live || {}), DATA.race || {});
    const hash = tradesHash(next);
    if ((opts && opts.updateHash) !== false && location.hash !== hash) {
      history.replaceState(null, "", hash);
    }
  }

  function switchTab(id, opts) {
    opts = opts || {};
    let name = String(id || "live").replace(/^tab-/, "");
    if (name === "trades-v1" || name === "trades/v1") { name = "trades"; if (opts.filter == null) opts.filter = "v1"; }
    if (name === "trades-v2" || name === "trades/v2") { name = "trades"; if (opts.filter == null) opts.filter = "v2"; }
    if (name === "trades-all") name = "trades";
    TABS.forEach((t) => {
      const btn = $("tab-" + t);
      const panel = $("panel-" + t);
      const on = t === name;
      if (btn) btn.setAttribute("aria-selected", on ? "true" : "false");
      if (panel) panel.hidden = !on;
    });
    if (name === "trades" && opts.filter) setTradeFilter(opts.filter, { updateHash: false });
    const hash = name === "trades" ? tradesHash(TRADE_FILTER) : ("#" + name);
    if (location.hash !== hash) {
      history.replaceState(null, "", hash);
    }
    if (opts.scrollTo) {
      const el = $(String(opts.scrollTo).replace(/^#/, ""));
      if (el) {
        requestAnimationFrame(() => el.scrollIntoView({ behavior: "smooth", block: "start" }));
        return;
      }
    }
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function openFullTradeLog(filter) {
    switchTab("trades", { filter: filter || "all", scrollTo: "race-log-card" });
    setTradeFilter(filter || "all");
  }

  document.querySelectorAll("nav.tabs button").forEach((b) => {
    b.addEventListener("click", () => switchTab(b.id));
  });
  window.addEventListener("hashchange", () => {
    const parsed = parseHash();
    switchTab(parsed.tab, { filter: parsed.filter || undefined });
  });

  function ema(values, period) {
    const out = Array(values.length).fill(null);
    if (values.length < period) return out;
    const k = 2 / (period + 1);
    let seed = 0;
    for (let i = 0; i < period; i++) seed += values[i];
    out[period - 1] = seed / period;
    for (let i = period; i < values.length; i++) {
      out[i] = values[i] * k + out[i - 1] * (1 - k);
    }
    return out;
  }

  function rsi(values, period) {
    const out = Array(values.length).fill(null);
    if (values.length <= period) return out;
    let gain = 0, loss = 0;
    for (let i = 1; i <= period; i++) {
      const d = values[i] - values[i - 1];
      if (d >= 0) gain += d; else loss -= d;
    }
    gain /= period; loss /= period;
    out[period] = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss);
    for (let i = period + 1; i < values.length; i++) {
      const d = values[i] - values[i - 1];
      gain = (gain * (period - 1) + Math.max(d, 0)) / period;
      loss = (loss * (period - 1) + Math.max(-d, 0)) / period;
      out[i] = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss);
    }
    return out;
  }

  function lastInd(closes) {
    if (!closes || closes.length < 60) return null;
    const e9 = ema(closes, 9), e21 = ema(closes, 21), e50 = ema(closes, 50);
    const r = rsi(closes, 14);
    const i = closes.length - 1;
    const macd12 = ema(closes, 12);
    const macd26 = ema(closes, 26);
    const line = macd12.map((a, idx) => (a == null || macd26[idx] == null) ? null : a - macd26[idx]);
    const compact = [], idxMap = [];
    line.forEach((v, idx) => { if (v != null) { compact.push(v); idxMap.push(idx); } });
    const sigComp = ema(compact, 9);
    const signal = Array(line.length).fill(null);
    sigComp.forEach((v, j) => { if (v != null) signal[idxMap[j]] = v; });
    const macdHist = (line[i] != null && signal[i] != null) ? (line[i] - signal[i]) : null;
    return { ema9: e9[i], ema21: e21[i], ema50: e50[i], rsi14: r[i], macd_hist: macdHist, ready: e50[i] != null && r[i] != null && macdHist != null };
  }

  function remainingText(closeIso) {
    if (!closeIso) return "—";
    const sec = Math.floor((Date.parse(closeIso) - Date.now()) / 1000);
    if (Number.isNaN(sec)) return "—";
    if (sec <= 0) return "Window closed";
    const m = Math.floor(sec / 60), s = sec % 60;
    return m + ":" + String(s).padStart(2, "0") + " left";
  }

  function sessionStatusNow(sess) {
    if (!sess) return "LIVE";
    const ends = sess.ends_at || sess.end;
    const sec = ends ? Math.floor((Date.parse(ends) - Date.now()) / 1000) : (sess.remaining_sec ?? 0);
    if (sess.status === "COMPLETE") return "COMPLETE";
    if (sec <= 0) return sess.position ? "SETTLING" : (sess.final_balance != null ? "COMPLETE" : "SETTLING");
    return "LIVE";
  }

  function sessionBadgeText(sess) {
    const status = sessionStatusNow(sess);
    const ends = sess.ends_at || sess.end;
    const sec = ends ? Math.floor((Date.parse(ends) - Date.now()) / 1000) : (sess.remaining_sec ?? 0);
    if (status === "COMPLETE") return "COMPLETE";
    if (status === "SETTLING" || sec <= 0) return "SETTLING";
    const m = Math.floor(sec / 60), s = Math.max(0, sec % 60);
    return "LIVE · " + m + "m " + String(s).padStart(2, "0") + "s left";
  }

  function tickSessionBadge() {
    if (!DATA || !DATA.session) return;
    const sess = DATA.session;
    const status = sessionStatusNow(sess);
    const pill = $("session-pill");
    if (pill) {
      pill.textContent = sessionBadgeText(sess);
      pill.className = "pill " + status + (status === "COMPLETE" && sess.pnl < 0 ? " down" : "");
    }
    const card = $("session-card");
    if (card) card.className = "card session-card " + status;
  }

  function tickContract() {
    const el = $("signal-countdown");
    const close = DATA && DATA.live && DATA.live.market && DATA.live.market.close_time;
    if (el && close) el.textContent = remainingText(close);
  }

  function drawChart(el, series) {
    if (!el) return;
    el.innerHTML = "";
    const w = 1050, h = 240, padL = 58, padR = 16, padT = 16, padB = 28;
    const a = (series || []).filter((p) => p && p.equity != null);
    if (!a.length) { el.innerHTML = '<p class="empty">No equity points yet.</p>'; return; }
    let min = Math.min(...a.map((p) => p.equity)), max = Math.max(...a.map((p) => p.equity));
    if (min === max) { min -= 1; max += 1; }
    const span = max - min; min -= span * 0.08; max += span * 0.08;
    const t0 = a[0].t, t1 = a[a.length - 1].t || t0 + 1;
    const x = (t) => padL + ((t - t0) / Math.max(t1 - t0, 1)) * (w - padL - padR);
    const y = (v) => padT + (1 - (v - min) / (max - min)) * (h - padT - padB);
    const path = a.map((p, i) => (i ? "L" : "M") + x(p.t).toFixed(1) + " " + y(p.equity).toFixed(1)).join(" ");
    const area = a.length > 1 ? path + ` L ${x(a[a.length - 1].t).toFixed(1)} ${h - padB} L ${x(a[0].t).toFixed(1)} ${h - padB} Z` : "";
    let grid = "";
    for (let i = 0; i <= 4; i++) {
      const v = min + (max - min) * (i / 4), yy = y(v);
      grid += `<line x1="${padL}" x2="${w - padR}" y1="${yy.toFixed(1)}" y2="${yy.toFixed(1)}" stroke="#F5D0A9" />`;
      grid += `<text x="${padL - 8}" y="${(yy + 4).toFixed(1)}" text-anchor="end" fill="#9A7B5C" font-size="11" font-family="ui-monospace, Menlo, monospace">${money(v)}</text>`;
    }
    el.innerHTML = `<svg class="chart-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">${grid}${area ? `<path d="${area}" fill="rgba(234,88,12,0.10)" />` : ""}<path d="${path}" fill="none" stroke="#EA580C" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round" /></svg>`;
  }

  function tradeRows(trades, emptyText, market) {
    const list = dedupeTrades(trades);
    if (!list.length) return `<p class="empty">${esc(emptyText)}</p>`;
    const rows = list.map((t) => {
      const side = (t.side || "").toUpperCase();
      const pnlVal = tradePnl(t, market);
      const pnl = pnlVal == null ? "—" : `<span class="${pnlVal >= 0 ? "up" : "down"}">${pnlVal >= 0 ? "Gain" : "Lost"} ${money(pnlVal, true)}</span>`;
      const bought = amountBoughtOf(t);
      const exit = t.status === "open"
        ? "Open"
        : (t.outcome === "settlement" ? ("Settled " + String(t.result || "").toUpperCase()) : ("Exit " + cents(t.exit_price)));
      const why = t.reason_exit || t.reason || t.reason_entry || "";
      return `<tr>
        <td>${esc(t.entry_time_pt || "")}</td>
        <td>${esc(prettyWindow(t) || (t.ticker || "").replace("KXBTC15M-", ""))}${pressTag(t)}</td>
        <td>${esc(side)}</td>
        <td class="mono">${esc(contractsLabel(t.contracts))} @ ${cents(t.entry_price)}</td>
        <td class="mono">Bought ${esc(money(bought))}</td>
        <td>${esc(exit)}</td>
        <td>${pnl}</td>
        <td>${esc(why)}</td>
      </tr>`;
    }).join("");
    return `<div class="table-wrap"><table>
      <thead><tr><th>Time (PT)</th><th>Contract</th><th>Side</th><th>Contracts / entry</th><th>Amount Bought</th><th>Exit / settlement</th><th>Gain / Lost</th><th>Why</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  }

  function bookTradeList(book, bookId, label) {
    const rows = dedupeTrades((book && book.trades) || []).map((t) => Object.assign({}, t, {
      book_id: t.book_id || bookId,
      book_label: t.book_label || label,
    }));
    const opens = (book && book.open_positions && book.open_positions.length)
      ? book.open_positions
      : (book && book.position ? [book.position] : []);
    opens.forEach((p) => {
      if (!p) return;
      const key = (p.ticker || "") + "|" + (p.entry_time || "") + "|" + (p.side || "");
      const exists = rows.some((t) => ((t.ticker || "") + "|" + (t.entry_time || "") + "|" + (t.side || "")) === key);
      if (!exists) {
        rows.push(Object.assign({}, p, {
          status: p.status || "open",
          book_id: bookId,
          book_label: label,
        }));
      }
    });
    return rows;
  }

  function sortTradesNewestFirst(trades) {
    return (trades || []).slice().sort((a, b) => {
      const tb = Date.parse(b.entry_time || b.exit_time || 0);
      const ta = Date.parse(a.entry_time || a.exit_time || 0);
      return (Number.isNaN(tb) ? 0 : tb) - (Number.isNaN(ta) ? 0 : ta);
    });
  }

  function collectRaceTrades(race) {
    race = race || {};
    const v1 = bookTradeList(race.v1 || {}, "v1", "Version 1");
    const v2 = bookTradeList(race.v2 || {}, "v2", "Version 2");
    return { v1, v2, all: v1.concat(v2) };
  }

  function gainLostPill(t, market) {
    if (!t || t.status === "open" || (t.pnl == null && t.status !== "closed")) {
      return { cls: "HOLD", text: "OPEN" };
    }
    const pnlVal = tradePnl(t, market);
    if (pnlVal == null) return { cls: "HOLD", text: "OPEN" };
    if (pnlVal >= 0) return { cls: "WIN", text: "WIN " + money(pnlVal, true) };
    return { cls: "LOSS", text: "LOSS " + money(pnlVal, true) };
  }

  function choiceReason(t) {
    const raw = (t && (t.reason_entry || t.reason)) || "";
    return plainReason({ reason: raw, reason_entry: raw }) || raw || "";
  }

  function raceLogDetails(t) {
    if (!t) return "";
    const bits = [];
    if (t.strike != null) bits.push("Strike " + px(t.strike));
    if (t.expiration_value != null) bits.push("Settlement BTC " + px(t.expiration_value));
    if (t.result) bits.push("Official result " + String(t.result).toUpperCase());
    if (t.outcome === "settlement") bits.push("Held to official settlement");
    else if (t.exit_price != null) bits.push("Exited at " + cents(t.exit_price));
    if (t.exit_time_pt) bits.push("Exit " + t.exit_time_pt);
    const inds = t.indicators || t.indicators_1m;
    if (inds) {
      const parts = [];
      if (inds.ema9 != null) parts.push("EMA 9 " + num(inds.ema9, 2));
      if (inds.ema21 != null) parts.push("EMA 21 " + num(inds.ema21, 2));
      if (inds.ema50 != null) parts.push("EMA 50 " + num(inds.ema50, 2));
      if (inds.rsi14 != null) parts.push("RSI 14 " + num(inds.rsi14, 2));
      if (inds.macd_hist != null) parts.push("MACD hist " + num(inds.macd_hist, 2));
      if (parts.length) bits.push(parts.join(" · "));
    }
    const dump = indicatorDetails(t);
    if (dump) bits.push(dump);
    if (t.reason_exit && t.reason_exit !== t.reason && t.reason_exit !== t.reason_entry) bits.push(t.reason_exit);
    return bits.join(". ");
  }

  function raceLogCard(t, market) {
    const when = t.entry_time_pt || shortTimePT(t.entry_time || t.t || "");
    const window = prettyWindow(t);
    const ver = (t.book_id === "v2" || /version 2/i.test(t.book_label || "")) ? "v2" : "v1";
    const verLabel = t.book_label || (ver === "v2" ? "Version 2" : "Version 1");
    const pill = gainLostPill(t, market);
    const side = String(t.side || "").toUpperCase();
    const bought = amountBoughtOf(t);
    const reason = choiceReason(t);
    const details = raceLogDetails(t);
    const detailsHtml = details
      ? `<details class="details"><summary>Details</summary><div class="feed-details">${esc(details)}</div></details>`
      : "";
    return `<article class="feed-card log-card ${esc(ver)}">
      <div class="feed-top">
        <div>
          <div class="feed-when">${esc(when)}</div>
          ${window ? `<div class="feed-window">${esc(window)}</div>` : ""}
          <div class="feed-badges">
            <span class="pill sm ${esc(ver.toUpperCase())}">${esc(verLabel)}</span>
            ${pressTag(t)}
          </div>
        </div>
        <div class="feed-right">
          <div class="pill sm ${esc(pill.cls)}">${esc(pill.text)}</div>
        </div>
      </div>
      <div class="log-facts">
        <span><span class="k">Side</span>${esc(side || "—")} · ${esc(contractsLabel(t.contracts))} @ ${cents(t.entry_price)}</span>
        <span><span class="k">Amount Bought</span>${esc(money(bought))}</span>
        <span><span class="k">Gain / Lost</span>${esc(pill.text)}</span>
      </div>
      ${reason ? `<div class="feed-line">${esc(reason)}</div>` : ""}
      ${detailsHtml}
    </article>`;
  }

  function renderRaceTradeLog(live, race) {
    const el = $("race-trade-log");
    const title = $("race-log-title");
    if (!el && !title) return;
    const market = (live && live.market) || {};
    const bags = collectRaceTrades(race);
    const filter = TRADE_FILTER === "v1" || TRADE_FILTER === "v2" ? TRADE_FILTER : "all";
    const source = filter === "v1" ? bags.v1 : filter === "v2" ? bags.v2 : bags.all;
    const list = sortTradesNewestFirst(source);
    const label = filter === "v1" ? "Version 1 Trades" : filter === "v2" ? "Version 2 Trades" : "All Trades";
    if (title) title.textContent = label + " (" + list.length + ")";
    if (!el) return;
    if (!list.length) {
      const empty = filter === "v1"
        ? "Version 1 hasn’t taken a race trade yet."
        : filter === "v2"
          ? "Version 2 is waiting for a strict-rules setup."
          : "No race trades yet — the full log will land here.";
      el.innerHTML = `<p class="empty">${esc(empty)}</p>`;
      return;
    }
    el.innerHTML = `<div class="log-feed">${list.map((t) => raceLogCard(t, market)).join("")}</div>`;
  }

  function viewAllTradesButton(bookId, book) {
    const n = bookTradeList(book || {}, bookId, bookId === "v2" ? "Version 2" : "Version 1").length;
    const label = "View all " + n + " trade" + (n === 1 ? "" : "s") + " →";
    return `<button type="button" class="more-link view-all" data-book="${esc(bookId)}">${esc(label)}</button>`;
  }


  const MONTHS = {JAN:0,FEB:1,MAR:2,APR:3,MAY:4,JUN:5,JUL:6,AUG:7,SEP:8,OCT:9,NOV:10,DEC:11};

  function fmtClockPT(date) {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: "America/Los_Angeles", hour: "numeric", minute: "2-digit", hour12: true
    }).formatToParts(date);
    const get = (ty) => (parts.find((x) => x.type === ty) || {}).value;
    return (get("hour") + ":" + get("minute") + " " + get("dayPeriod")).replace(/\s+/g, " ");
  }

  function windowLabelFromClose(date) {
    const close = new Date(date);
    const start = new Date(+close - 15 * 60 * 1000);
    const s = fmtClockPT(start);
    const e = fmtClockPT(close);
    const sP = s.slice(-2), eP = e.slice(-2);
    const sC = s.slice(0, -3), eC = e.slice(0, -3);
    if (sP.toUpperCase() === eP.toUpperCase()) return sC + "–" + eC + " " + eP + " window";
    return s + "–" + e + " window";
  }

  function parseTickerClose(ticker) {
    const s = String(ticker || "").replace(/^KXBTC15M-/, "");
    const m = s.match(/^(\d{2})([A-Za-z]{3})(\d{2})(\d{4})/);
    if (!m) return null;
    const day = +m[1];
    const mon = MONTHS[m[2].toUpperCase()];
    const year = 2000 + +m[3];
    const hh = +m[4].slice(0, 2);
    const mm = +m[4].slice(2, 4);
    if (mon == null || Number.isNaN(day) || Number.isNaN(year)) return null;
    const iso = (off) => {
      const sign = off < 0 ? "-" : "+";
      const abs = String(Math.abs(off)).padStart(2, "0");
      return `${year}-${String(mon + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}T${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}:00${sign}${abs}:00`;
    };
    for (const off of [-5, -4]) {
      const d = new Date(iso(off));
      if (Number.isNaN(+d)) continue;
      const parts = new Intl.DateTimeFormat("en-US", {
        timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", hour12: false,
        year: "numeric", month: "2-digit", day: "2-digit"
      }).formatToParts(d);
      const get = (ty) => (parts.find((x) => x.type === ty) || {}).value;
      let hour24 = get("hour");
      if (hour24 === "24") hour24 = "00";
      if (+get("year") === year && +get("month") === mon + 1 && +get("day") === day && +hour24 === hh && +get("minute") === mm) {
        return d;
      }
    }
    return new Date(iso(-5));
  }

  function shortTimePT(raw) {
    const s = String(raw || "");
    const m = s.match(/(\d{1,2}:\d{2}\s*[AP]M)/i);
    if (m) return m[1].replace(/\s+/g, " ").toUpperCase().replace("AM", "AM").replace("PM", "PM") + (/\bPT\b/.test(s) ? " PT" : " PT");
    const d = Date.parse(s);
    if (!Number.isNaN(d)) return fmtClockPT(new Date(d)) + " PT";
    return s;
  }

  function prettyWindow(item) {
    if (!item) return "";
    const close = item.close_time || item.exit_time;
    if (close) {
      const d = Date.parse(close);
      if (!Number.isNaN(d)) return windowLabelFromClose(new Date(d));
    }
    const fromTicker = parseTickerClose(item.ticker);
    if (fromTicker) return windowLabelFromClose(fromTicker);
    const wl = String(item.window_label || "");
    const m = wl.match(/(\d{1,2}:\d{2})\s*([AP]M)\s*[–-]\s*(\d{1,2}:\d{2})\s*([AP]M)/i);
    if (m) {
      if (m[2].toUpperCase() === m[4].toUpperCase()) return `${m[1]}–${m[3]} ${m[4].toUpperCase()} window`;
      return `${m[1]} ${m[2].toUpperCase()}–${m[3]} ${m[4].toUpperCase()} window`;
    }
    return "";
  }

  function isIndicatorDump(text) {
    return /EMA 9|MACD histogram|RSI \d+|5-minute trend agrees/i.test(text || "");
  }

  function fmtUsdRound(n) {
    if (n == null || Number.isNaN(n)) return "$0";
    return "$" + Math.round(Number(n)).toLocaleString("en-US");
  }

  function plainReason(item) {
    const raw = String((item && (item.reason_exit || item.reason || item.reason_entry)) || "").replace(/\s+/g, " ").trim();
    if (!raw) return "";
    const settle = raw.match(/Settled\s+(YES|NO)\s+at\s+BTC\s+([\d,.]+)\s+vs\s+strike\s+([\d,.]+)/i);
    if (settle) {
      return `Settled ${settle[1].toUpperCase()} (${fmtUsdRound(Number(settle[2].replace(/,/g, "")))} vs ${fmtUsdRound(Number(settle[3].replace(/,/g, "")))} strike)`;
    }
    const closeExpiry = raw.match(/Only\s+(\d+:\d+)\s+left/i);
    if (closeExpiry || /Too close to expiry|stands aside under/i.test(raw)) {
      return closeExpiry
        ? `Only ${closeExpiry[1]} left in this window, so the bot stood aside.`
        : "Too close to expiry — the bot stood aside.";
    }
    const expensive = /too expensive|too pricey|bot only buys/i.test(raw);
    const choppy = /Choppy|no clean edge/i.test(raw);
    const dist = raw.match(/BTC\s+([+\-−]?)\s*\$?([\d,]+(?:\.\d+)?)\s+vs\s+(?:the\s+)?\$?([\d,]+(?:\.\d+)?)\s+strike/i);
    const sideAsk = raw.match(/\b(Yes|No)\s+(?:is already|ask is(?: only)?)\s+(\d+\s*¢)/i) || raw.match(/\b(Yes|No)\s+ask\s+(\d+\s*¢)/i);
    if (expensive && dist && sideAsk) {
      const sign = (dist[1] === "-" || dist[1] === "−") ? "−" : "+";
      const amt = "$" + Math.round(Number(dist[2].replace(/,/g, "")));
      const dir = sign === "−" ? "below" : "above";
      return `BTC ${sign}${amt} ${dir} strike, but ${sideAsk[1]} at ${sideAsk[2].replace(/\s+/g, "")} was too pricey`;
    }
    if (dist) {
      const sign = (dist[1] === "-" || dist[1] === "−") ? "−" : "+";
      const amt = "$" + Math.round(Number(String(dist[2]).replace(/,/g, "")));
      const dir = sign === "−" ? "below" : "above";
      let line = `BTC ${sign}${amt} ${dir} strike`;
      if (sideAsk) line += `, ${sideAsk[1]} ask ${sideAsk[2].replace(/\s+/g, "")}`;
      if (/Buy No/i.test(raw)) return line + " — bought No.";
      if (/Buy Yes/i.test(raw)) return line + " — bought Yes.";
      if (choppy) return "Choppy / no clean edge: " + line + ".";
      return line + ".";
    }
    if (choppy) {
      const after = raw.replace(/^Choppy\s*\/\s*no clean edge:\s*/i, "");
      const clipped = after.replace(/\(5-minute trend[^)]*\)/g, "").replace(/\s+/g, " ").trim();
      return clipped.length < 160 ? ("Choppy / no clean edge: " + clipped) : "Choppy / no clean edge on this window.";
    }
    if (!isIndicatorDump(raw) && raw.length <= 180) return raw;
    const sentences = raw.split(/(?<=\.)\s+/);
    const rest = sentences.filter((s) => !isIndicatorDump(s) && !/^Just bought/i.test(s));
    if (rest.length) {
      const line = rest.join(" ").trim();
      return line.length <= 180 ? line : line.slice(0, 177) + "…";
    }
    const last = sentences[sentences.length - 1] || raw;
    return last.length <= 180 ? last : last.slice(0, 177) + "…";
  }

  function indicatorDetails(item) {
    const raw = String((item && (item.reason_entry || item.reason || item.reason_exit)) || "").replace(/\s+/g, " ").trim();
    if (!isIndicatorDump(raw)) return "";
    const m = raw.match(/((?:Just bought[^.]*—\s*)?EMA 9[^.]+\.)/i);
    return m ? m[1].replace(/^Just bought[^.]*—\s*/i, "").trim() : raw;
  }

  function eventPill(item) {
    const action = String((item && (item.action || item.label)) || "").toUpperCase();
    const pnl = item && item.pnl;
    const settled = item && (item.status === "closed" || item.outcome === "settlement" || item.kind === "trade");
    const kind = String((item && (item.pnl_kind || item.exit_kind || item.outcome)) || "").toLowerCase();
    const early = kind === "tp" || kind === "sl" || kind === "early_exit" || kind === "take_profit" || kind === "cut_loss" || /TAKE_PROFIT|CUT_LOSS|SELL NOW/.test(action);
    if (settled && pnl != null) {
      if (kind === "tp" || /TAKE_PROFIT/.test(action)) return { cls: "TP", text: "TP " + money(pnl, true) };
      if (kind === "sl" || /CUT_LOSS/.test(action)) return { cls: "SL", text: "SL " + money(pnl, true) };
      if (early) return { cls: pnl >= 0 ? "TP" : "SL", text: "EARLY EXIT " + money(pnl, true) };
      if (pnl >= 0) return { cls: "WIN", text: "WIN " + money(pnl, true) };
      return { cls: "LOSS", text: "LOSS " + money(pnl, true) };
    }
    if (/TAKE_PROFIT|SELL NOW/.test(action)) return { cls: "TP", text: "TP / EARLY EXIT" };
    if (/CUT_LOSS/.test(action)) return { cls: "SL", text: "SL / EARLY EXIT" };
    if (/WIN/.test(action) && pnl != null) return { cls: "WIN", text: "WIN " + money(pnl, true) };
    if (/LOSS/.test(action) && pnl != null) return { cls: "LOSS", text: "LOSS " + money(pnl, true) };
    if (/BUY_NO|BOUGHT NO|BUY NO/.test(action) && !/HOLD/.test(action)) return { cls: "BOUGHT", text: "BOUGHT NO" };
    if (/BUY_YES|BOUGHT YES|BUY YES/.test(action) && !/HOLD/.test(action)) return { cls: "BOUGHT", text: "BOUGHT YES" };
    if (/HOLD/.test(action) || (item && item.status === "open")) {
      const side = String((item && item.side) || (/YES/.test(action) ? "YES" : /NO/.test(action) ? "NO" : "")).toUpperCase();
      return { cls: "HOLD", text: side ? "HOLD " + side : "HOLD" };
    }
    if (/SKIP/.test(action)) return { cls: "SKIP", text: "SKIP" };
    if (/WAIT/.test(action)) return { cls: "WAIT", text: "WAIT" };
    const short = String((item && (item.label || item.action)) || "CHECK").replace(/—.*$/, "").trim();
    return { cls: "WAIT", text: short };
  }

  function feedRightMeta(item) {
    const bits = [];
    if (item.contracts != null && item.entry_price != null) {
      bits.push(contractsLabel(item.contracts) + " @ " + cents(item.entry_price));
    } else if (item.yes_ask != null || item.no_ask != null) {
      bits.push("Yes " + cents(item.yes_ask) + " / No " + cents(item.no_ask));
    }
    const bought = item.amount_bought != null ? item.amount_bought : item.total_in;
    if (bought != null) bits.push("bought " + money(bought));
    return bits.join(" · ");
  }

  function feedCard(item) {
    const when = shortTimePT(item.t_pt || item.entry_time_pt || item.exit_time_pt || item.t || "");
    const window = prettyWindow(item);
    const pill = eventPill(item);
    const meta = feedRightMeta(item);
    const line = plainReason(item);
    const details = indicatorDetails(item);
    const detailsHtml = details
      ? `<details class="details"><summary>Details</summary><div class="feed-details">${esc(details)}</div></details>`
      : "";
    return `<article class="feed-card">
      <div class="feed-top">
        <div>
          <div class="feed-when">${esc(when)}</div>
          ${window ? `<div class="feed-window">${esc(window)}</div>` : ""}
        </div>
        <div class="feed-right">
          <div class="pill sm ${esc(pill.cls)}">${esc(pill.text)}</div>
          ${meta ? `<div class="feed-meta">${esc(meta)}</div>` : ""}
        </div>
      </div>
      ${line ? `<div class="feed-line">${esc(line)}</div>` : ""}
      ${detailsHtml}
    </article>`;
  }

  function groupFeedItems(checks, trades) {
    const groups = new Map();
    const keyOf = (it) => it.ticker || prettyWindow(it) || (it.t || "");
    const bump = (it, extra) => {
      const key = keyOf(it);
      const g = groups.get(key) || { ticker: it.ticker, checks: [], trade: null, t: it.t };
      Object.assign(g, extra || {});
      const ts = Date.parse(it.t || it.exit_time || it.entry_time || 0);
      if (!g.t || ts >= Date.parse(g.t || 0)) g.t = it.t || it.exit_time || it.entry_time;
      groups.set(key, g);
      return g;
    };
    (trades || []).forEach((t) => {
      const g = bump({
        ...t,
        t: t.exit_time || t.entry_time,
        t_pt: t.exit_time_pt || t.entry_time_pt,
        ticker: t.ticker,
        close_time: t.close_time || t.exit_time,
      });
      g.trade = t;
    });
    (checks || []).forEach((c) => {
      const g = bump(c);
      g.checks.push(c);
    });
    const cards = [];
    for (const g of groups.values()) {
      if (g.trade) {
        const entryCheck = g.checks.find((c) => /HOLD|BUY/i.test(String(c.action || c.label || "")));
        cards.push({
          kind: "trade",
          t: g.trade.exit_time || g.trade.entry_time || g.t,
          t_pt: g.trade.exit_time_pt || g.trade.entry_time_pt,
          ticker: g.trade.ticker,
          close_time: g.trade.close_time || g.trade.exit_time || (entryCheck && entryCheck.close_time),
          window_label: (entryCheck && entryCheck.window_label) || g.trade.window_label,
          status: g.trade.status || "closed",
          outcome: g.trade.outcome,
          side: g.trade.side,
          pnl: g.trade.pnl,
          contracts: g.trade.contracts,
          entry_price: g.trade.entry_price,
          amount_bought: g.trade.amount_bought != null ? g.trade.amount_bought : g.trade.total_in,
          reason: g.trade.reason_exit || g.trade.reason || "",
          reason_entry: (entryCheck && entryCheck.reason) || g.trade.reason_entry || "",
          action: (g.trade.pnl != null && g.trade.pnl >= 0) ? "WIN" : "LOSS",
        });
        continue;
      }
      const ranked = g.checks.slice().sort((a, b) => Date.parse(b.t || 0) - Date.parse(a.t || 0));
      const hold = ranked.find((c) => /HOLD|BUY_YES|BUY_NO/i.test(String(c.action || c.label || "")));
      const chosen = hold || ranked[0];
      if (chosen) cards.push(chosen);
    }
    cards.sort((a, b) => Date.parse(b.t || 0) - Date.parse(a.t || 0));
    return cards;
  }

  function checkRows(checks) {
    const list = (checks || []).filter(Boolean);
    if (!list.length) return '<p class="empty">The bot will log every 5-minute check here, including WAIT reasons.</p>';
    return `<div class="feed">${list.map(feedCard).join("")}</div>`;
  }

  function compactEventList(items, emptyText) {
    if (!items || !items.length) return `<p class="empty">${esc(emptyText)}</p>`;
    return `<div class="feed">${items.map(feedCard).join("")}</div>`;
  }

  function sessionChecks(sess) {
    const raw = (sess && ((sess.checks && sess.checks.length) ? sess.checks : sess.events)) || [];
    return raw.filter((e) => e && typeof e === "object").slice().sort((a, b) => Date.parse(b.t || 0) - Date.parse(a.t || 0));
  }

  function renderHero(data, live, sess) {
    const btc = liveBtc != null ? liveBtc : live.btc_price;
    $("btc-price").textContent = px(btc);
    const strike = (live.market && live.market.strike) || live.strike;
    const sub = $("btc-sub");
    if (btc != null && strike != null) {
      const d = btc - strike;
      sub.textContent = (d >= 0 ? "+" : "") + money(d).replace("$", "") + " vs strike " + px(strike);
      sub.className = "btc-live-sub " + (d >= 0 ? "up" : "down");
    } else {
      sub.textContent = strike != null ? ("Strike " + px(strike)) : "Live Coinbase BTC/USD";
      sub.className = "btc-live-sub";
    }
    const dateEl = $("hero-date");
    if (dateEl) dateEl.textContent = longDatePt(new Date());
    $("updated").textContent = data.generated_at_pt
      ? ("Bot refresh " + data.generated_at_pt.replace(", 2026, ", ", "))
      : "";

    const race = data.race || {};
    const v1 = race.v1 || {};
    const v2 = race.v2 || {};
    const fillBal = (id, book) => {
      const el = $(id);
      if (!el) return;
      const start = book.starting_balance != null ? book.starting_balance : 100;
      const equity = book.equity != null ? book.equity : book.cash;
      el.textContent = equity != null ? (money(start) + " → " + money(equity)) : "—";
    };
    fillBal("stat-v1-val", v1);
    fillBal("stat-v2-val", v2);
    if ($("stat-v1-sub")) $("stat-v1-sub").textContent = ((v1.wins || 0) + "–" + (v1.losses || 0)) + " · " + (v1.short_name || "Version 1.2").replace("Version ", "V") + " " + (v1.name || "Chart-Confirmed Active");
    if ($("stat-v2-sub")) $("stat-v2-sub").textContent = ((v2.wins || 0) + "–" + (v2.losses || 0)) + " · " + (v2.short_name || "Version 2.2").replace("Version ", "V") + " " + (v2.name || "Strict + Fair-Value Edge");
    if ($("stat-leader-val")) $("stat-leader-val").textContent = race.leader_label || (race.leader === "tied" ? "Tied" : "—") || "Tied";
    const gap = $("stat-gap-val");
    if (gap) {
      const m = race.leader_margin;
      gap.textContent = m == null ? "—" : money(m);
      gap.className = "val " + (m > 0 ? "up" : "");
    }
    if ($("stat-leader-sub")) $("stat-leader-sub").textContent = race.start_pt ? ("Race started " + race.start_pt) : "Head-to-head $100 race";
    if ($("stat-gap-sub")) $("stat-gap-sub").textContent = race.leader_text && race.leader !== "tied" ? race.leader_text : "Difference in equity";
  }

  function renderSession(sess) {
    const card = $("session-card");
    if (!card) return;
    if (!sess || !(sess.starts_at || sess.start)) {
      card.hidden = true;
      return;
    }
    card.hidden = false;
    $("session-window").textContent = sess.window_label_pt || sess.window_label || "1-hour $40 run";
    $("session-range").textContent = (sess.start_pt && sess.end_pt)
      ? (sess.start_pt + " → " + sess.end_pt + " · archived $40 run · real Kalshi taker fees")
      : "Archived $40 run · real Kalshi taker fees";
    tickSessionBadge();
    if (sessionTimer) clearInterval(sessionTimer);
    sessionTimer = setInterval(tickSessionBadge, 1000);

    const start = sess.starting_balance != null ? sess.starting_balance : 40;
    const equity = sess.equity != null ? sess.equity : sess.cash;
    $("sess-equity").textContent = money(start) + " → " + money(equity);
    const pnl = $("sess-pnl");
    pnl.textContent = money(sess.pnl, true);
    pnl.className = "v " + (sess.pnl > 0 ? "up" : sess.pnl < 0 ? "down" : "");
    $("sess-record").textContent = (sess.wins || 0) + "–" + (sess.losses || 0);
    const sessC = $("sess-contracts");
    if (sessC) sessC.textContent = String(bookContracts(sess));
    const sessB = $("sess-bought");
    if (sessB) sessB.textContent = money(bookAmountBought(sess));
    const press = $("session-press");
    if (press) {
      const label = sess.next_stake_label || "Next stake: $4–$8 flat (no press)";
      press.textContent = label;
      press.className = "pill BASE";
    }

    const posEl = $("session-open");
    const market = (DATA && DATA.live && DATA.live.market) || {};
    if (sess.position) {
      posEl.hidden = false;
      posEl.innerHTML = openTradeLine(sess.position, "Open trade", market);
    } else {
      posEl.hidden = true;
      posEl.innerHTML = "";
    }
  }

  function raceWindowTitle(market) {
    const m = market || {};
    if (m.close_time && !Number.isNaN(Date.parse(m.close_time))) {
      return windowLabelFromClose(new Date(Date.parse(m.close_time))).replace(/ window$/i, " PT");
    }
    return prettyWindow(m) ? prettyWindow(m).replace(/ window$/i, " PT") : "Waiting for the next window";
  }


  function centsEdge(n) {
    if (n == null || Number.isNaN(Number(n))) return "—";
    const c = Math.round(Number(n) * 100);
    return (c > 0 ? "+" : "") + c + "¢";
  }

  function biasClass(bias) {
    const b = String(bias || "").toLowerCase();
    if (b === "up" || b === "buy" || b === "yes" || b === "bullish") return "UP";
    if (b === "down" || b === "sell" || b === "no" || b === "bearish") return "DOWN";
    return "MIXED";
  }

  function renderMarketReader(data, live) {
    const reader = (data && data.market_reader) || (live && live.market_reader) || {};
    const charts = reader.charts || {};
    const cons = reader.consensus || {};
    const score = reader.scorecard || (data.strategy && data.strategy.chart_scorecard) || (data.learning && data.learning.chart_scorecard) || {};
    const best = reader.best_chart || score.best_chart || "5m";
    if ($("reader-best")) $("reader-best").textContent = "Best chart: " + best;
    const bias = cons.bias || reader.btc_bias || "mixed";
    const biasEl = $("reader-bias");
    if (biasEl) {
      biasEl.textContent = cons.label || (bias === "up" ? "Bullish alignment" : bias === "down" ? "Bearish alignment" : "Mixed / choppy");
      biasEl.className = "pill " + biasClass(bias);
    }
    if ($("reader-title")) {
      $("reader-title").textContent = cons.label ? (cons.label + (best ? " · watching the " + best + " chart" : "")) : "Reading Bitcoin…";
    }
    const grid = $("reader-charts");
    if (grid) {
      grid.innerHTML = ["1m", "5m", "15m", "1h"].map((tf) => {
        const c = charts[tf] || {};
        const b = c.bias || (c.ready ? "flat" : "—");
        const rsi = c.rsi14 != null ? ("RSI " + Math.round(c.rsi14)) : "warming up";
        const macd = c.macd_hist != null ? ("MACD " + (c.macd_hist >= 0 ? "+" : "") + Number(c.macd_hist).toFixed(1)) : "";
        const mark = (String(best).toLowerCase() === tf || String(best).toLowerCase() === "multi-tf" && tf === "15m") ? " · best" : "";
        return `<div class="tf-cell ${esc(c.bias || "flat")}"><div class="tf-name">${tf} chart${mark}</div><div class="tf-bias">${esc(String(b))}</div><div class="tf-meta">${esc([rsi, macd].filter(Boolean).join(" · "))}</div></div>`;
      }).join("");
    }
    const implied = reader.implied_yes;
    const fair = reader.fair_yes;
    const yesEdge = reader.yes_edge;
    const noEdge = reader.no_edge;
    const flow = reader.btc_bias || ((reader.btc_flow || {}).bias);
    const bookBias = (reader.kalshi_book || {}).bias;
    const spread = (reader.kalshi_book || {}).spread_cents;
    const mets = [
      ["Spot fair Yes", fair == null ? "—" : Math.round(fair * 100) + "¢"],
      ["Kalshi Yes ask", implied == null ? "—" : Math.round(implied * 100) + "¢"],
      ["Yes edge", centsEdge(yesEdge)],
      ["No edge", centsEdge(noEdge)],
      ["BTC book / flow", flow ? String(flow) : "—"],
      ["Kalshi book", bookBias ? (bookBias + (spread != null ? " · " + spread + "¢ spread" : "")) : "—"],
    ];
    if ($("reader-metrics")) {
      $("reader-metrics").innerHTML = mets.map(([k, v]) => {
        const cls = (k.indexOf("edge") >= 0 && typeof v === "string" && v.startsWith("+")) ? "up" : (k.indexOf("edge") >= 0 && typeof v === "string" && v.startsWith("-") ? "down" : "");
        return `<div class="metric"><div class="k">${esc(k)}</div><div class="v ${cls}">${esc(String(v))}</div></div>`;
      }).join("");
    }
    const buy = reader.buy_rules || "";
    const sell = reader.sell_rules || "";
    if ($("reader-rules")) {
      $("reader-rules").textContent = [sell, buy].filter(Boolean).join(" ");
    }
    if ($("reader-sub")) {
      const ready = (cons.ready || []).join(", ") || "waiting on candles";
      $("reader-sub").textContent = "Charts ready: " + ready + ". Coinbase / Kraken book & flow, Kalshi KXBTC15M book, and a spot-fair probability from realized 1-minute volatility.";
    }
  }

  function renderScorecard(data) {
    const score = (data.learning && data.learning.chart_scorecard) || (data.strategy && data.strategy.chart_scorecard) || {};
    if ($("scorecard-title")) {
      const bits = [score.best_chart ? ("Best chart: " + score.best_chart) : null, score.best_exit ? ("Best exit: " + score.best_exit) : null].filter(Boolean);
      $("scorecard-title").textContent = bits.length ? bits.join(" · ") : "Which charts are winning";
    }
    if ($("scorecard-sub") && score.windows_with_exit_path != null) {
      $("scorecard-sub").textContent = (score.windows || "") && (Number(score.windows).toLocaleString("en-US") + " windows scored") + (score.windows_with_exit_path != null ? (" · " + score.windows_with_exit_path + " with a full bid path for exits") : "");
    }
    if ($("scorecard-takeaway")) $("scorecard-takeaway").textContent = score.takeaway || "The hourly learner will fill this in after it scores the charts.";
    const el = $("scorecard-rows");
    if (!el) return;
    const rows = score.rows || [];
    if (!rows.length) { el.innerHTML = ""; return; }
    const bests = new Set([score.best_chart, score.best_exit, score.best_reader].filter(Boolean));
    el.innerHTML = `<div class="scorecard-table">${rows.map((r) => {
      const v = r.val || {};
      const o = r.overall || {};
      const cls = bests.has(r.name) ? "scorecard-row best" : "scorecard-row";
      const valPnl = v.net_pnl;
      const wr = v.win_rate != null && v.trade_count ? (v.win_rate.toFixed(1) + "% · " + v.trade_count + " trades") : "no trades";
      return `<div class="${cls}">
        <div><div class="name">${esc(r.name || "")}</div><div class="kind">${esc(r.kind || "")}</div></div>
        <div><div class="k" style="font-size:11px;font-weight:800;letter-spacing:0.06em;text-transform:uppercase;color:var(--muted)">Unseen P&amp;L</div><div class="v ${valPnl > 0 ? "up" : valPnl < 0 ? "down" : ""}">${money(valPnl, true)}</div></div>
        <div><div class="k" style="font-size:11px;font-weight:800;letter-spacing:0.06em;text-transform:uppercase;color:var(--muted)">Unseen record</div><div class="v">${esc(wr)}</div></div>
        <div><div class="k" style="font-size:11px;font-weight:800;letter-spacing:0.06em;text-transform:uppercase;color:var(--muted)">Overall / DD</div><div class="v">${money(o.net_pnl, true)} · ${o.max_drawdown_pct != null ? Number(o.max_drawdown_pct).toFixed(1) + "% DD" : "—"}</div></div>
      </div>`;
    }).join("")}</div>`;
  }

  function renderRaceBar(data, live) {
    const race = (data && data.race) || {};
    const m = (live && live.market) || {};
    if ($("race-window")) $("race-window").textContent = raceWindowTitle(m);
    const tick = () => {
      const el = $("race-countdown");
      if (!el) return;
      el.textContent = m.close_time ? remainingText(m.close_time) : "";
    };
    tick();
    if (closeTimer) clearInterval(closeTimer);
    if (m.close_time) closeTimer = setInterval(tick, 1000);

    const btc = liveBtc != null ? liveBtc : live.btc_price;
    const strike = (m.strike != null ? m.strike : live.strike);
    const vs = $("race-btc-vs");
    if (vs) {
      if (btc != null && strike != null) {
        const d = btc - strike;
        vs.textContent = "BTC " + (d >= 0 ? "+" : "−") + money(Math.abs(d)) + " vs " + px(strike) + " strike";
        vs.className = d >= 0 ? "up" : "down";
      } else {
        vs.textContent = "BTC vs strike";
        vs.className = "";
      }
    }
    const lead = $("race-leader");
    if (lead) {
      lead.textContent = race.leader_text || "Tied";
      const who = race.leader || "tied";
      lead.className = "pill LEADER " + (who === "v1" ? "v1" : who === "v2" ? "v2" : "tied");
    }
    const cta = $("kalshi-cta");
    if (cta) {
      if (m.kalshi_url) { cta.hidden = false; cta.href = m.kalshi_url; }
      else { cta.hidden = true; cta.removeAttribute("href"); }
    }
    const ind = liveInd || live.indicators_1m;
    const cells = [
      ["EMA 9", ind && ind.ema9],
      ["EMA 21", ind && ind.ema21],
      ["EMA 50", ind && ind.ema50],
      ["RSI 14", ind && ind.rsi14],
      ["MACD Hist", ind && ind.macd_hist],
    ];
    if ($("signal-inds")) {
      $("signal-inds").innerHTML = cells.map(([k, v]) =>
        `<div class="ind"><div class="k">${k}</div><div class="v">${v == null ? "—" : num(v, 2)}</div></div>`
      ).join("");
    }
    const dist = (btc != null && strike != null) ? (btc - strike) : live.distance;
    const quotes = `Yes ${cents(m.yes_bid)} / ${cents(m.yes_ask)} · No ${cents(m.no_bid)} / ${cents(m.no_ask)}`;
    const strikeBits = [
      strike != null ? `Strike ${px(strike)}` : null,
      dist != null ? (`BTC is ${dist >= 0 ? "+" : ""}${money(dist)} vs strike`) : null,
      live.last_1m_time_pt ? ("Last confirmed 1-minute bar: " + live.last_1m_time_pt) : null,
    ].filter(Boolean).join(". ");
    if ($("signal-math")) {
      $("signal-math").textContent = [quotes, strikeBits].filter(Boolean).join(". ") + (liveInd ? " 1-minute indicators are updating live from Coinbase in this browser." : "");
    }
  }

  function renderRaceBook(id, book, choice, live) {
    book = book || {};
    choice = choice || {};
    const start = book.starting_balance != null ? book.starting_balance : 100;
    const equity = book.equity != null ? book.equity : book.cash;
    if ($(id + "-equity")) $(id + "-equity").textContent = money(start) + " → " + money(equity);
    if ($(id + "-bought")) $(id + "-bought").textContent = money(bookAmountBought(book));
    const pnlEl = $(id + "-pnl");
    if (pnlEl) {
      pnlEl.textContent = money(book.pnl, true);
      pnlEl.className = "v " + (book.pnl > 0 ? "up" : book.pnl < 0 ? "down" : "");
    }
    if ($(id + "-contracts")) $(id + "-contracts").textContent = String(bookContracts(book));
    if ($(id + "-record")) $(id + "-record").textContent = (book.wins || 0) + "–" + (book.losses || 0);
    if ($(id + "-badge") && book.badge) $(id + "-badge").textContent = book.badge;
    if ($(id + "-kicker") && book.short_name) $(id + "-kicker").textContent = book.short_name;
    if ($(id + "-title") && book.name) $(id + "-title").textContent = book.name;
    const press = $(id + "-press");
    if (press) {
      const label = book.next_stake_label || "Next stake: $4–$8 flat (no press)";
      press.textContent = label;
      press.className = "pill BASE";
    }
    const box = $(id + "-choice");
    const action = choice.action || "WAIT";
    const side = choice.side || "";
    if (box) box.className = "choice-box " + action + (action === "HOLD" && side ? " " + side : "");
    if ($(id + "-choice-label")) $(id + "-choice-label").textContent = choice.label || action;
    if ($(id + "-choice-reason")) $(id + "-choice-reason").textContent = choice.reason || "Waiting for the next 5-minute check.";
    const opens = (book.open_positions && book.open_positions.length)
      ? book.open_positions
      : (book.position ? [book.position] : []);
    const openEl = $(id + "-open");
    const market = (live && live.market) || {};
    if (openEl) {
      if (!opens.length) {
        openEl.hidden = false;
        openEl.className = "open-line wait";
        openEl.textContent = "Waiting for setup";
      } else {
        openEl.hidden = false;
        openEl.className = "open-line open-stack";
        openEl.innerHTML = opens.map((p, i) => openTradeLine(
          p,
          opens.length > 1 ? ("Open trade #" + (p.window_entry || (i + 1))) : "Open trade",
          market
        )).join("");
      }
    }
    const cards = groupFeedItems(sessionChecks(book), book.trades || []).slice(0, 8);
    if ($(id + "-recent")) {
      const list = compactEventList(cards, "The first check in this $100 race will land here.");
      $(id + "-recent").innerHTML = list + viewAllTradesButton(id, book);
    }
  }

  function renderRaceArchive(list, market) {
    const el = $("race-archive");
    if (!el) return;
    if (!list || !list.length) {
      el.innerHTML = '<p class="empty">No finished races yet.</p>';
      return;
    }
    el.innerHTML = list.slice().reverse().map((r) => {
      const books = r.books || {};
      const card = (key) => {
        const b = books[key] || {};
        const trades = (b.trades || []).slice().sort((a, c) => Date.parse(c.entry_time || 0) - Date.parse(a.entry_time || 0));
        return `<details class="details"><summary>${esc(b.name || key)} — ${esc(money(b.starting_balance || 100))} → ${esc(money(b.equity))} · ${b.wins || 0}–${b.losses || 0} · ${esc(money(b.pnl, true))}</summary>${tradeRows(trades, "No trades in this race.", market)}</details>`;
      };
      return `<article class="card"><div class="card-kicker">Race ${esc(String(r.race_number || ""))} · archived</div>
        <div class="card-title" style="font-size:18px">${esc(r.label || "")}</div>
        <div class="card-range">${esc(r.start_pt || "")} → ${esc(r.ended_at_pt || "")}</div>
        ${card("v1")}${card("v2")}</article>`;
    }).join("");
  }

  function renderGates(data) {
    const el = $("reader-gates");
    if (!el) return;
    const gates = ((data && data.race) || {}).gates || {};
    const box = (key) => {
      const g = gates[key];
      if (!g) return "";
      const chips = (g.entry || []).map((x) => `<span class="pill BASE">${esc(x)}</span>`).join(" ");
      return `<div class="gate-box ${key}"><div class="card-kicker">${esc(g.label || key)}</div>
        <div class="gate-line"><strong>Entry gates:</strong> ${chips}</div>
        <div class="gate-line"><strong>Exit:</strong> ${esc(g.exit || "hold to settlement")}</div></div>`;
    };
    el.innerHTML = box("v1") + box("v2");
  }

  function renderRace(data, live) {
    renderGates(data);
    const race = (data && data.race) || {};
    renderRaceBar(data, live || {});
    const choice = race.choice || {};
    renderRaceBook("v1", race.v1, choice.v1, live);
    renderRaceBook("v2", race.v2, choice.v2, live);
  }

  function renderTrades(live, sess, race) {
    race = race || (DATA && DATA.race) || {};
    const market = live.market || {};
    const v1 = race.v1 || {};
    const v2 = race.v2 || {};
    renderRaceTradeLog(live, race);
    if ($("v1-checks")) $("v1-checks").innerHTML = checkRows(sessionChecks(v1));
    if ($("v2-checks")) $("v2-checks").innerHTML = checkRows(sessionChecks(v2));
    if ($("race-trades-range")) {
      $("race-trades-range").textContent = race.start_pt
        ? ("Race " + (race.race_number || 2) + ": both books restarted together at $100 / 0–0 (started " + race.start_pt + "). Version 1.2 takes at most one chart-confirmed ticket per window; Version 2.2 takes at most one, only when every strict rule and the fair-value gate pass.")
        : "Both books start together at $100. Version 1.2 takes at most one chart-confirmed ticket per window; Version 2.2 takes at most one, only when every strict rule passes.";
    }
    renderRaceArchive((DATA && DATA.race_archive) || [], market);
    const sessTrades = ((sess && sess.trades) || []).slice().sort((a, b) => Date.parse(b.entry_time || 0) - Date.parse(a.entry_time || 0));
    if ($("archive-session-range") && sess && sess.start_pt && sess.end_pt) {
      $("archive-session-range").textContent = "Completed " + (sess.window_label || "") + " · " + sess.start_pt + " → " + sess.end_pt + ". Kept here so nothing is lost.";
    }
    if ($("session-trades")) $("session-trades").innerHTML = tradeRows(sessTrades, "No trades from the archived 1-hour $40 run.", market);
    if ($("session-checks")) $("session-checks").innerHTML = checkRows(sessionChecks(sess));

    $("book-equity").textContent = money(live.equity);
    const bookBought = $("book-bought");
    if (bookBought) bookBought.textContent = money(bookAmountBought(live));
    const bp = $("book-pnl");
    bp.textContent = money(live.pnl, true);
    bp.className = "v " + (live.pnl > 0 ? "up" : live.pnl < 0 ? "down" : "");
    $("book-record").textContent = (live.wins || 0) + "–" + (live.losses || 0);
    const bookC = $("book-contracts");
    if (bookC) bookC.textContent = String(bookContracts(live));
    $("book-pnl-sub").textContent = (live.pnl > 0 ? "Gain " : live.pnl < 0 ? "Lost " : "") + pct(live.pnl_pct) + " from " + money(live.starting_balance) + (live.cash != null ? " · cash " + money(live.cash) : "");
    const bookWin = $("book-win");
    if (bookWin) {
      const press = live.next_stake_label || "Next stake: $4–$8 flat (no press)";
      bookWin.textContent = (live.win_rate == null ? "No closed trades yet" : (live.win_rate.toFixed(1) + "% after fees")) + " · " + press;
    }

    const pos = $("book-open");
    if (live.position) {
      pos.hidden = false;
      pos.innerHTML = openTradeLine(live.position, "Open $100 book", market);
    } else {
      pos.hidden = true;
      pos.innerHTML = "";
    }
    $("book-trades").innerHTML = tradeRows(live.trades, "No $100-book trades yet.", market);
  }

  function renderBacktest(bt) {
    $("bt-range").textContent = bt.start_pt && bt.end_pt
      ? `Same rules on ${bt.windows_scanned} settled KXBTC15M windows from ${bt.start_pt} to ${bt.end_pt} (${bt.days} days). Entry uses the real Kalshi quote 1 minute after each window opens; exits are official settlements.`
      : "Backtest unavailable.";
    $("bt-pnl").textContent = money(bt.net_pnl, true);
    $("bt-pnl").className = "val " + (bt.net_pnl > 0 ? "up" : bt.net_pnl < 0 ? "down" : "");
    $("bt-eq").textContent = money(bt.starting_balance) + " → " + money(bt.ending_equity) + " · $4–$8 flat per trade (no press)";
    $("bt-ret").textContent = pct(bt.return_pct);
    $("bt-ret").className = "val " + (bt.return_pct > 0 ? "up" : bt.return_pct < 0 ? "down" : "");
    $("bt-windows").textContent = (bt.windows_traded || 0) + " trades · " + (bt.windows_skipped || 0) + " stands aside";
    $("bt-win").textContent = bt.win_rate == null ? "—" : bt.win_rate.toFixed(1) + "%";
    $("bt-trades").textContent = (bt.wins || 0) + "W / " + (bt.losses || 0) + "L";
    $("bt-dd").textContent = bt.max_drawdown_pct == null ? "—" : "−" + Number(bt.max_drawdown_pct).toFixed(2) + "%";
    $("bt-pf").textContent = bt.profit_factor != null ? ("Profit factor " + bt.profit_factor) : (bt.profit_factor_note || "Profit factor n/a");
    $("bt-note").textContent = [bt.assumption_note, bt.fee_note].filter(Boolean).join(" ");
    drawChart($("bt-chart"), bt.equity_history || []);
    $("bt-log").innerHTML = tradeRows(bt.trades, "No backtest trades in this window.", true);
  }

  function renderLearning(data) {
    const strat = (data && data.strategy) || {};
    const learning = (data && data.learning) || {};
    const version = learning.live_version || strat.version || 1;
    const params = strat.params || {};
    if ($("learn-version")) $("learn-version").textContent = "v" + version;
    if ($("learn-updated")) {
      const when = strat.updated_at_pt || learning.generated_at_pt;
      const src = strat.source === "hourly-learner" ? "promoted by the hourly learner" : (strat.source === "defaults" ? "starting rules" : (strat.source || ""));
      $("learn-updated").textContent = [when ? ("Updated " + when) : "", src].filter(Boolean).join(" · ");
    }
    const pill = $("learn-pill");
    if (pill) {
      const last = learning.decision || (learning.last_run && learning.last_run.decision) || "";
      const promoted = /PROMOTED/i.test(last);
      pill.textContent = promoted ? last : (strat.source === "hourly-learner" ? "Learner" : "v" + version);
      pill.className = "pill " + (promoted ? "PROMOTED" : "KEPT");
    }
    if ($("learn-rules")) {
      $("learn-rules").textContent = strat.plain_english || "Buys Yes/No when the 1-minute trend is clean, BTC is at least $20 from the strike, the ask is 35–72¢, and 2–15 minutes remain.";
    }
    const windows = learning.windows;
    if ($("learn-windows")) $("learn-windows").textContent = windows != null ? Number(windows).toLocaleString("en-US") : "—";
    if ($("learn-split")) {
      const tr = learning.train_windows, va = learning.val_windows;
      $("learn-split").textContent = (tr != null && va != null) ? (tr + " / " + va) : "—";
    }
    const last = learning.last_run || {};
    const liveVal = (last.live && last.live.val) || {};
    const valPnl = liveVal.net_pnl;
    const valEl = $("learn-val-pnl");
    if (valEl) {
      valEl.textContent = valPnl == null ? "—" : money(valPnl, true);
      valEl.className = "v " + (valPnl > 0 ? "up" : valPnl < 0 ? "down" : "");
    }
    if ($("learn-decision")) {
      const dec = learning.decision || last.decision || "—";
      $("learn-decision").textContent = dec.replace(/^PROMOTED\s+/i, "").replace(/^KEPT\s+/i, "Kept ");
    }
    const logEl = $("learn-log");
    if (!logEl) return;
    const runs = learning.runs || [];
    if (!runs.length) {
      logEl.innerHTML = '<p class="empty">The first hourly learning run will land here. It studies settled windows and only promotes a change when unseen P&amp;L is clearly better.</p>';
      return;
    }
    logEl.innerHTML = `<div class="feed">${runs.map((r) => {
      const promoted = /PROMOTED/i.test(r.decision || "");
      const live = r.live || {};
      const best = r.best_candidate || {};
      const wr = (m) => (m && m.win_rate != null && m.trade_count) ? (m.win_rate.toFixed(1) + "% WR, " + m.trade_count + " trades") : "no trades";
      const stats = [
        r.windows != null ? (Number(r.windows).toLocaleString("en-US") + " windows") : null,
        "Live train " + money((live.train || {}).net_pnl, true) + " / unseen " + money((live.val || {}).net_pnl, true) + " (" + wr(live.val) + ")",
        best.name ? ("Best tested: " + (best.name === "current" ? "current rules" : best.name) + " · train " + money((best.train || {}).net_pnl, true) + " / unseen " + money((best.val || {}).net_pnl, true) + " (" + wr(best.val) + ")") : null,
      ].filter(Boolean).join(" · ");
      return `<article class="feed-card">
        <div class="feed-top">
          <div>
            <div class="feed-when">${esc(shortTimePT(r.t_pt || r.t || ""))}</div>
            <div class="feed-window">${esc((r.decision || "Hourly check").replace(/→/g, "→"))}</div>
          </div>
          <div class="feed-right">
            <div class="pill sm ${promoted ? "PROMOTED" : "KEPT"}">${esc(promoted ? "PROMOTED" : "KEPT")}</div>
          </div>
        </div>
        <div class="learn-takeaway">${esc(r.takeaway || "")}</div>
        <div class="learn-stats">${esc(stats)}</div>
      </article>`;
    }).join("")}</div>`;
  }

  function render(data) {
    DATA = data;
    const live = data.live || {};
    const sess = data.session || {};
    const bt = data.backtest || {};
    renderHero(data, live, sess);
    renderMarketReader(data, live);
    renderRace(data, live);
    renderSession(sess);
    renderTrades(live, sess, data.race || {});
    renderLearning(data);
    renderScorecard(data);
    renderBacktest(bt);
  }

  async function loadDashboard() {
    const res = await fetch("data.json?t=" + Date.now(), { cache: "no-store" });
    if (!res.ok) throw new Error("Could not load data.json (" + res.status + ")");
    const data = await res.json();
    if (!data.learning || !data.strategy) {
      try {
        const lr = await fetch("learning.json?t=" + Date.now(), { cache: "no-store" });
        if (lr.ok) {
          const extra = await lr.json();
          if (!data.learning && extra.learning) data.learning = extra.learning;
          if (!data.strategy && extra.strategy) data.strategy = extra.strategy;
        }
      } catch (e) { /* learning.json is optional until the first hourly run */ }
    }
    $("error").hidden = true;
    render(data);
  }

  async function pollCoinbase() {
    try {
      const [tickRes, candleRes] = await Promise.all([
        fetch("https://api.exchange.coinbase.com/products/BTC-USD/ticker", { cache: "no-store" }),
        fetch("https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=60", { cache: "no-store" }),
      ]);
      if (!tickRes.ok) throw new Error("ticker " + tickRes.status);
      const tick = await tickRes.json();
      liveBtc = parseFloat(tick.price);
      const dot = $("btc-dot");
      if (dot) dot.className = "dot";
      if (candleRes.ok) {
        const raw = await candleRes.json();
        const closes = raw.slice().sort((a, b) => a[0] - b[0]).map((r) => r[4]);
        liveInd = lastInd(closes);
      }
      if (DATA) render(DATA);
    } catch (err) {
      const dot = $("btc-dot");
      if (dot) dot.className = "dot stale";
      console.warn("Coinbase live poll failed", err);
    }
  }

  async function start() {
    const parsed = parseHash();
    if (parsed.filter) TRADE_FILTER = parsed.filter;
    switchTab(parsed.tab, { filter: parsed.filter || undefined });
    try { await loadDashboard(); }
    catch (err) {
      $("error").hidden = false;
      $("error").textContent = "Could not load the live dashboard data. " + err.message;
    }
    pollCoinbase();
    setInterval(pollCoinbase, 20000);
    setInterval(() => { loadDashboard().catch(() => {}); }, 60 * 1000);
  }

  $("recent-more")?.addEventListener("click", (e) => {
    e.preventDefault();
    openFullTradeLog("all");
  });
  document.addEventListener("click", (e) => {
    const btn = e.target && e.target.closest && e.target.closest(".view-all[data-book], .filter-pills .pill");
    if (!btn) return;
    e.preventDefault();
    if (btn.classList.contains("view-all")) {
      openFullTradeLog(btn.getAttribute("data-book") || "all");
      return;
    }
    const id = btn.id || "";
    if (id === "filter-v1") openFullTradeLog("v1");
    else if (id === "filter-v2") openFullTradeLog("v2");
    else if (id === "filter-all") openFullTradeLog("all");
  });

  start();
})();
