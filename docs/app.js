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

  const TABS = ["live", "trades", "backtest", "rules"];

  function switchTab(id) {
    const name = String(id || "live").replace(/^tab-/, "");
    TABS.forEach((t) => {
      const btn = $("tab-" + t);
      const panel = $("panel-" + t);
      const on = t === name;
      if (btn) btn.setAttribute("aria-selected", on ? "true" : "false");
      if (panel) panel.hidden = !on;
    });
    if (location.hash !== "#" + name) {
      history.replaceState(null, "", "#" + name);
    }
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  document.querySelectorAll("nav.tabs button").forEach((b) => {
    b.addEventListener("click", () => switchTab(b.id));
  });
  window.addEventListener("hashchange", () => {
    const h = (location.hash || "#live").slice(1);
    if (TABS.includes(h)) switchTab(h);
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
        <td class="mono">${esc((t.ticker || "").replace("KXBTC15M-", ""))}${pressTag(t)}</td>
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

  function checkRows(checks) {
    const list = (checks || []).filter(Boolean);
    if (!list.length) return '<p class="empty">The bot will log every 5-minute check here, including WAIT reasons.</p>';
    return `<div class="table-wrap"><table>
      <thead><tr><th>Time (PT)</th><th>Call</th><th>Contract</th><th>Yes / No</th><th>Why</th></tr></thead>
      <tbody>${list.map((c) => {
        const quotes = (c.yes_ask != null || c.no_ask != null) ? ("Yes " + cents(c.yes_ask) + " / No " + cents(c.no_ask)) : "—";
        return `<tr>
          <td>${esc(c.t_pt || "")}</td>
          <td>${esc(c.label || c.action || "—")}</td>
          <td class="mono">${esc((c.ticker || "—").replace("KXBTC15M-", ""))}</td>
          <td class="mono">${esc(quotes)}</td>
          <td>${esc(c.reason || (typeof c === "string" ? c : ""))}</td>
        </tr>`;
      }).join("")}</tbody></table></div>`;
  }

  function compactEventList(items, emptyText) {
    if (!items || !items.length) return `<p class="empty">${esc(emptyText)}</p>`;
    return `<div class="list">${items.map((e) => {
      const when = e.t_pt || e.entry_time_pt || "";
      const ticker = (e.ticker || "").replace("KXBTC15M-", "");
      const quotes = (e.yes_ask != null || e.no_ask != null) ? ("Yes " + cents(e.yes_ask) + " / No " + cents(e.no_ask)) : "";
      const act = e.label || e.action || ((e.side || "") + (e.status === "open" ? " OPEN" : "")).toUpperCase() || "CHECK";
      const why = e.reason || e.reason_exit || e.reason_entry || "";
      const pnl = e.pnl != null ? ` · ${money(e.pnl, true)}` : "";
      const contracts = e.contracts != null ? (contractsLabel(e.contracts) + (e.entry_price != null ? " @ " + cents(e.entry_price) : "")) : "";
      return `<div class="row">
        <div class="row-top">
          <span class="when">${esc(when)}</span>
          ${ticker ? `<span class="mono">${esc(ticker)}</span>` : ""}
          ${contracts ? `<span class="mono">${esc(contracts)}</span>` : ""}
          ${quotes ? `<span>${esc(quotes)}</span>` : ""}
          <span class="act">${esc(act)}${esc(pnl)}</span>
        </div>
        <div class="row-why">${esc(why)}</div>
      </div>`;
    }).join("")}</div>`;
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

    const start = sess && (sess.starting_balance != null ? sess.starting_balance : 40);
    const equity = sess ? (sess.final_balance != null && sess.status === "COMPLETE" ? sess.final_balance : (sess.equity != null ? sess.equity : sess.cash)) : null;
    $("stat-balance-val").textContent = equity != null ? (money(start) + " → " + money(equity)) : "—";
    const balSub = $("stat-balance-sub");
    if (balSub) {
      const n = sess ? bookContracts(sess) : 0;
      balSub.textContent = n ? (contractsLabel(n) + " traded") : "Start → current balance";
    }
    const boughtEl = $("stat-bought-val");
    if (boughtEl) boughtEl.textContent = sess ? money(bookAmountBought(sess)) : "—";
    const pnl = $("stat-pnl-val");
    const pnlVal = sess ? sess.pnl : null;
    pnl.textContent = sess ? money(pnlVal, true) : "—";
    pnl.className = "val " + (pnlVal > 0 ? "up" : pnlVal < 0 ? "down" : "");
    $("stat-pnl-sub").textContent = sess ? ((pnlVal > 0 ? "Gain" : pnlVal < 0 ? "Lost" : "Flat") + " · " + pct(sess.pnl_pct)) : "";
    $("stat-record-val").textContent = sess ? ((sess.wins || 0) + "–" + (sess.losses || 0)) : "—";
    $("stat-record-sub").textContent = sess && sess.window_label ? sess.window_label : "1-hour $40 run";
  }

  function renderSession(sess) {
    const card = $("session-card");
    if (!sess || !(sess.starts_at || sess.start)) {
      card.hidden = true;
      return;
    }
    card.hidden = false;
    $("session-window").textContent = sess.window_label_pt || sess.window_label || "1-hour $40 run";
    $("session-range").textContent = (sess.start_pt && sess.end_pt)
      ? (sess.start_pt + " → " + sess.end_pt + " · $5 per trade · real Kalshi taker fees")
      : "$5 per trade · real Kalshi taker fees";
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
      const label = sess.next_stake_label || (sess.press_next ? ("Next trade: 2× press ($" + Math.round(Math.min(8, 2 * (sess.last_win_stake || 0))) + ")") : "Next stake: $2–$4");
      press.textContent = label;
      press.className = "pill " + (sess.press_next ? "PRESS" : "BASE");
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

  function renderSignal(live) {
    const card = $("signal-card");
    const action = live.call || "WAIT";
    const side = live.side || (live.position && live.position.side) || "";
    card.className = "card signal-card " + action + (action === "HOLD" && side ? " " + side : "");
    $("signal-action").textContent = live.call_label || action;
    $("signal-reason").textContent = live.reason || "Waiting for the next Kalshi 15-minute window.";
    const m = live.market || {};
    const bits = [];
    if (m.ticker) bits.push(`<span class="mono">${esc(m.ticker)}</span>`);
    if (m.close_time) bits.push(`<span class="countdown" id="signal-countdown">${remainingText(m.close_time)}</span>`);
    if (m.strike != null) bits.push(`<span>Strike ${esc(px(m.strike))}</span>`);
    $("signal-meta").innerHTML = bits.join(" · ");
    if (closeTimer) clearInterval(closeTimer);
    if (m.close_time) closeTimer = setInterval(tickContract, 1000);

    const cta = $("kalshi-cta");
    if (m.kalshi_url) {
      cta.hidden = false;
      cta.href = m.kalshi_url;
    } else {
      cta.hidden = true;
      cta.removeAttribute("href");
    }

    const ind = liveInd || live.indicators_1m;
    const cells = [
      ["EMA 9", ind && ind.ema9],
      ["EMA 21", ind && ind.ema21],
      ["EMA 50", ind && ind.ema50],
      ["RSI 14", ind && ind.rsi14],
      ["MACD Hist", ind && ind.macd_hist],
    ];
    $("signal-inds").innerHTML = cells.map(([k, v]) =>
      `<div class="ind"><div class="k">${k}</div><div class="v">${v == null ? "—" : num(v, 2)}</div></div>`
    ).join("");
    const dist = live.distance;
    const quotes = `Yes ${cents(m.yes_bid)} / ${cents(m.yes_ask)} · No ${cents(m.no_bid)} / ${cents(m.no_ask)}`;
    const strikeBits = [
      m.strike != null ? `Strike ${px(m.strike)}` : null,
      dist != null ? (`BTC is ${dist >= 0 ? "+" : ""}${money(dist)} vs strike`) : null,
      live.last_1m_time_pt ? ("Last confirmed 1-minute bar: " + live.last_1m_time_pt) : null,
    ].filter(Boolean).join(". ");
    $("signal-math").textContent = [quotes, strikeBits].filter(Boolean).join(". ") + (liveInd ? " 1-minute indicators are updating live from Coinbase in this browser." : "");
  }

  function renderRecent(sess) {
    const checks = sessionChecks(sess).slice(0, 5);
    const trades = ((sess && sess.trades) || []).slice().sort((a, b) => Date.parse(b.entry_time || 0) - Date.parse(a.entry_time || 0)).slice(0, 3);
    const items = [];
    trades.forEach((t) => items.push({
      t: t.entry_time || t.exit_time,
      t_pt: t.entry_time_pt,
      ticker: t.ticker,
      label: ((t.side || "") + (t.status === "open" ? " · OPEN" : (t.pnl != null ? " · " + (t.pnl >= 0 ? "WIN" : "LOSS") : ""))).toUpperCase(),
      reason: t.reason_exit || t.reason || t.reason_entry || "",
      pnl: t.pnl,
      contracts: t.contracts,
      entry_price: t.entry_price,
      yes_ask: null,
      no_ask: null,
    }));
    checks.forEach((c) => items.push(c));
    items.sort((a, b) => Date.parse(b.t || 0) - Date.parse(a.t || 0));
    // Prefer unique recent lines: trades first, then checks, max 5
    const seen = new Set();
    const uniq = [];
    for (const it of items) {
      const key = (it.t || "") + "|" + (it.label || it.action || "") + "|" + (it.ticker || "");
      if (seen.has(key)) continue;
      seen.add(key);
      uniq.push(it);
      if (uniq.length >= 5) break;
    }
    $("recent-list").innerHTML = compactEventList(uniq, "Every 15-minute window still gets a line here, even on a WAIT.");
  }

  function renderTrades(live, sess) {
    const sessTrades = ((sess && sess.trades) || []).slice().sort((a, b) => Date.parse(b.entry_time || 0) - Date.parse(a.entry_time || 0));
    const market = live.market || {};
    $("session-trades").innerHTML = tradeRows(sessTrades, "No $40-session trades yet this hour. WAIT checks still appear in the activity log below.", market);
    $("session-checks").innerHTML = checkRows(sessionChecks(sess));

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
      const press = live.next_stake_label || (live.press_next ? "Next trade: 2× press" : "Next stake: $2–$4");
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
    $("bt-eq").textContent = money(bt.starting_balance) + " → " + money(bt.ending_equity) + " · $2–$4 tickets, 2× after a win";
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

  function render(data) {
    DATA = data;
    const live = data.live || {};
    const sess = data.session || {};
    const bt = data.backtest || {};
    renderHero(data, live, sess);
    renderSession(sess);
    renderSignal(live);
    renderRecent(sess);
    renderTrades(live, sess);
    renderBacktest(bt);
  }

  async function loadDashboard() {
    const res = await fetch("data.json?t=" + Date.now(), { cache: "no-store" });
    if (!res.ok) throw new Error("Could not load data.json (" + res.status + ")");
    const data = await res.json();
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
    const h = (location.hash || "#live").slice(1);
    switchTab(TABS.includes(h) ? h : "live");
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
    switchTab("trades");
  });

  start();
})();
