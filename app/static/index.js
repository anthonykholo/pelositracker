// Tab Navigation Logic
  const nativeFetch = window.fetch.bind(window);
  window.fetch = (resource, options = {}) => {
    const method = String(options.method || "GET").toUpperCase();
    if (["POST", "PUT", "PATCH", "DELETE"].includes(method)) {
      const cookie = document.cookie.split("; ")
        .find(value => value.startsWith("csrf_token=") || value.startsWith("__Host-csrf_token="));
      if (cookie) {
        const headers = new Headers(options.headers || {});
        headers.set("X-CSRF-Token", decodeURIComponent(cookie.split("=").slice(1).join("=")));
        options = {...options, headers};
      }
    }
    return nativeFetch(resource, options);
  };

  document.querySelectorAll('.tab-carousel .pill').forEach(btn => {
    btn.addEventListener('click', e => {
      document.querySelectorAll('.tab-carousel .pill').forEach(b => b.classList.remove('active'));
      e.target.classList.add('active');
      document.querySelectorAll('.tab-content').forEach(tc => tc.classList.add('is-hidden'));
      document.getElementById(e.target.dataset.tab).classList.remove('is-hidden');
    });
  });

  // Auth & Login Logic
  document.querySelector("#login-form").addEventListener("submit", async e => {
    e.preventDefault();
    const form = e.currentTarget;
    const btn = form.querySelector("button");
    const err = document.querySelector("#login-error");
    btn.disabled = true; err.hidden = true;
    try {
      const r = await fetch("/api/login", {
        method: "POST",
        body: new URLSearchParams(new FormData(form))
      });
      const data = await r.json().catch(()=>({}));
      if (!r.ok) throw new Error(data.detail || "Invalid credentials");

      const overlay = document.querySelector("#login-overlay");
      overlay.classList.add("dissolve");
      setTimeout(() => { overlay.hidden = true; }, 1500);

      // Start the app!
      startApp();
    } catch(er) {
      err.textContent = er.message; err.hidden = false;
    } finally { btn.disabled = false; }
  });

  // The rest of the app logic...
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"})[c]);
  const pct = value => value == null ? "—" : `${(value * 100).toFixed(1)}%`;
  const cents = value => value == null ? "—" : `${(value * 100).toFixed(1)}¢`;
  const signedCents = value => value == null ? "—" : `${value >= 0 ? "+" : ""}${(value * 100).toFixed(1)}¢`;
  const money = value => value == null ? "—" : `${value >= 0 ? "+" : "-"}$${Math.abs(value).toFixed(2)}`;
  const keyFor = (...parts) => encodeURIComponent(parts.join("|"));
  let refreshInFlight = false;

  function tagClass(action) {
    if (action === "ENTRY WINDOW") return "entry";
    if (action === "HOLD") return "hold";
    if (action === "CONSIDER CASH") return "cash";
    if (action === "EXIT WATCH") return "exit";
    if (action === "MARKET ONLY") return "marketonly";
    return "wait";
  }

  let activeLine = "all", lastEvents = [];
  const LINE_META = { moneyline:{label:"Moneyline",cls:"lt-ml"}, spread:{label:"Spread",cls:"lt-sp"}, total:{label:"Over / Under",cls:"lt-ou"} };
  const LINE_ORDER = ["moneyline","spread","total"];
  function lineType(market, outcome){
    const m=(market||"").toLowerCase(), o=String(outcome||"").trim().toLowerCase();
    if(o.startsWith("over")||o.startsWith("under")||/total|over.?under|o\/u/.test(m)) return "total";
    if(/spread|handicap|run.?line|puck.?line|\bline\b/.test(m) || /[+-]\d/.test(String(outcome||""))) return "spread";
    return "moneyline";
  }
  function lineBadge(market, outcome){ const meta=LINE_META[lineType(market,outcome)]; return `<span class="line-badge ${meta.cls}">${meta.label}</span>`; }
  function renderCarousel(present){
    const el=document.querySelector("#line-filter");
    const types=LINE_ORDER.filter(t=>present.has(t));
    if(types.length<=1){ el.innerHTML=""; return; }
    const pill=(k,l)=>`<button class="pill${activeLine===k?" active":""}" data-line="${k}">${l}</button>`;
    el.innerHTML=pill("all","All")+types.map(t=>pill(t,LINE_META[t].label)).join("");
  }

  function marketRow(eventId, market, openDetails) {
    const detailKey = keyFor("market", eventId, market.token_id);
    const modelClass = market.entry_margin == null ? "" : market.entry_margin >= 0 ? "positive" : "negative";
    const guide = market.price_ceiling == null
      ? "Add a matching sportsbook event to calculate a validated entry ceiling."
      : market.room_to_ceiling >= 0
        ? `<strong>Entry ceiling ${cents(market.price_ceiling)}</strong> · current ask is ${cents(market.room_to_ceiling)} below the ceiling.`
        : `<strong>Wait for ${cents(market.price_ceiling)} or lower</strong> · current ask is ${cents(-market.room_to_ceiling)} above the ceiling.`;
    const risks = market.risk_flags.length ? market.risk_flags : ["No elevated execution flags detected; continue watching price and news latency."];
    const quality = market.quality_components;
    const qualityReason = quality ? `<li>Signal-quality policy: completeness ${quality.data_completeness.toFixed(0)}, provider freshness ${quality.provider_freshness.toFixed(0)}, identity ${quality.identity_confidence.toFixed(0)}, execution ${quality.execution_quality.toFixed(0)}, source independence ${quality.source_independence.toFixed(0)}, model sample support ${quality.model_sample_support.toFixed(0)}, calibration support ${quality.calibration_support.toFixed(0)}. These are reliability checks, not a win probability.</li>` : "";
    const ages = `Provider age ${market.provider_age_seconds == null ? "unknown" : market.provider_age_seconds.toFixed(0)+"s"} · receipt age ${market.receipt_age_seconds == null ? "unknown" : market.receipt_age_seconds.toFixed(0)+"s"}`;
    const uncertainty = market.uncertainty_low == null ? "unavailable" : `${pct(market.uncertainty_low)}–${pct(market.uncertainty_high)} historical bootstrap interval`;
    const calibration = market.calibrated_consensus_probability == null ? "unavailable" : pct(market.calibrated_consensus_probability);
    const positiveEv = market.probability_net_ev_positive == null ? "unavailable" : pct(market.probability_net_ev_positive);
    const netEv = market.net_expected_value_total == null ? "unavailable" : money(market.net_expected_value_total);
    const independentModel = market.independent_model_probability == null
      ? "unavailable (no approved exact-segment artifact)"
      : `${pct(market.independent_model_probability)} · ${esc(market.independent_model_version||"unknown version")} · calibration ${esc(market.independent_calibration_version||"unknown")} · test n=${Number(market.independent_model_sample_size||0)} across ${Number(market.independent_model_event_count||0)} events`;
    const executionAudit = `<li>Requested-size VWAP ${cents(market.requested_size_vwap)}; fee-adjusted requested cost ${cents(market.requested_effective_cost)}; simulated fee ${market.execution_fee == null ? "unknown" : "$"+market.execution_fee.toFixed(4)}; historical execution-cost adjustment ${signedCents(market.expected_execution_cost_offset)}; fillable ${market.paper_fillable_size == null ? "unknown" : market.paper_fillable_size.toFixed(2)+" shares"}.</li>`;
    const lineage = `<li>Engine ${esc(market.engine_version||"unavailable")} · consensus model ${esc(market.model_version||"unavailable")} (selection n=${Number(market.model_sample_size||0)}) · calibration ${esc(market.calibration_version||"unavailable")} (n=${Number(market.calibration_sample_size||0)}) · independent registry ${esc(market.independent_model_registry_version||"unavailable")} · independent model hash ${esc((market.independent_model_hash||"unavailable").slice(0,12))} · independent calibration hash ${esc((market.independent_calibration_hash||"unavailable").slice(0,12))} · execution ${esc(market.execution_policy_version||"unavailable")} · config ${esc((market.configuration_hash||"unavailable").slice(0,12))}.</li>`;
    const gates = (market.gate_results||[]).map(gate => `<li class="${gate.status === "fail" ? "risk" : ""}">Gate ${esc(gate.code)}: ${esc(gate.status)} · ${esc(gate.explanation||"")}${gate.value == null ? "" : ` · value ${Number(gate.value).toFixed(4)}`}${gate.threshold == null ? "" : ` · threshold ${Number(gate.threshold).toFixed(4)}`}</li>`).join("");
    return `<div class="market" data-line="${lineType(market.market,market.outcome)}" data-token-id="${esc(market.token_id)}">
      <div class="market-top"><div class="outcome">${lineBadge(market.market,market.outcome)}${esc(market.outcome)}<small>${esc(market.question)}</small></div><span class="tag ${tagClass(market.entry_action)}">${esc(market.entry_action)}</span></div>
      <div class="figs">
        <div class="fig"><div class="key">Buy now</div><div class="value">${cents(market.buy_price)}</div><div class="hint">Executable ask</div></div>
        <div class="fig"><div class="key">Sell now</div><div class="value">${cents(market.sell_price)}</div><div class="hint">Executable bid</div></div>
        <div class="fig"><div class="key">Net edge</div><div class="value ${modelClass}">${signedCents(market.edge)}</div><div class="hint">After execution costs · need ${cents(market.required_edge)} · buffer ${signedCents(market.edge_buffer)}</div></div>
        <div class="fig"><div class="key">Signal quality</div><div class="value">${market.confidence == null ? "—" : market.confidence.toFixed(0)+"/100"}</div><div class="hint">Data reliability · ${market.reference_sources} source family/families</div></div>
      </div>
      <div class="guide">${guide} ${esc(market.consensus_method||"display-only")} consensus ${pct(market.consensus_probability)} · calibrated consensus ${calibration} · independent model ${independentModel} · uncertainty ${uncertainty} · P(net EV &gt; 0) ${positiveEv} · net EV ${netEv}. Spread ${cents(market.spread)} · ask depth ${market.ask_size == null ? "—" : market.ask_size.toFixed(1)+" shares"} · liquidity ${market.market_liquidity == null ? "—" : "$"+Number(market.market_liquidity).toLocaleString(undefined,{maximumFractionDigits:0})}.</div>
      <details class="why" data-detail-key="${detailKey}"${openDetails.has(detailKey) ? " open" : ""}><summary>What to look out for</summary>
        <ul><li>${ages}</li>${executionAudit}${qualityReason}${lineage}${gates}${market.reasons.map(reason => `<li>${esc(reason)}</li>`).join("")}${risks.map(risk => `<li class="risk">${esc(risk)}</li>`).join("")}</ul></details>
      <details class="why"><summary>Add or update my position</summary>
        <form class="position-form" data-save-position data-event-id="${esc(eventId)}" data-token-id="${esc(market.token_id)}" data-market="${esc(market.market)}" data-outcome="${esc(market.outcome)}">
          <div><label>Shares</label><input name="shares" type="number" min="0.01" max="1000000" step="0.01" required placeholder="25"></div>
          <div><label>Average entry (cents)</label><input name="entry_cents" type="number" min="0.1" max="99.9" step="0.1" required placeholder="52.5"></div>
          <button type="submit">Save position</button>
        </form>
      </details>
    </div>`;
  }

  function positionRow(eventId, position, openDetails) {
    const detailKey = keyFor("position", eventId, position.token_id);
    const pnlClass = position.unrealized_pnl == null ? "" : position.unrealized_pnl >= 0 ? "positive" : "negative";
    return `<div class="position">
      <div class="position-top"><div class="outcome">${esc(position.outcome)}<small>${position.shares.toFixed(2)} shares · average ${cents(position.avg_entry_price)}</small></div><span class="tag ${tagClass(position.advice)}">${esc(position.advice)}</span></div>
      <div class="figs">
        <div class="fig"><div class="key">Cash-out bid</div><div class="value">${cents(position.current_bid)}</div><div class="hint">Before fees/slippage</div></div>
        <div class="fig"><div class="key">Cash value</div><div class="value">${position.cash_value == null ? "—" : "$"+position.cash_value.toFixed(2)}</div><div class="hint">Shares × bid</div></div>
        <div class="fig"><div class="key">Unrealized P/L</div><div class="value ${pnlClass}">${money(position.unrealized_pnl)}</div><div class="hint">${position.roi == null ? "—" : pct(position.roi)} return</div></div>
        <div class="fig"><div class="key">Remaining hold edge</div><div class="value">${signedCents(position.remaining_hold_edge)}</div><div class="hint">Calibrated consensus minus executable bid · lower-bound edge ${signedCents(position.conservative_hold_edge)}</div></div>
      </div>
      <details class="why" data-detail-key="${detailKey}"${openDetails.has(detailKey) ? " open" : ""}><summary>Why this hold/cash status?</summary><ul>${position.reasons.map(reason => `<li>${esc(reason)}</li>`).join("")}</ul></details>
      <button class="position-remove" type="button" data-remove-position data-event-id="${esc(eventId)}" data-token-id="${esc(position.token_id)}">Remove position</button>
    </div>`;
  }

  function fallbackSignal(signal) {
    return `<div class="market" data-line="${lineType(signal.market,signal.outcome)}"><div class="market-top"><div class="outcome">${lineBadge(signal.market,signal.outcome)}${esc(signal.outcome)}<small>${esc(signal.market)} reference signal</small></div><span class="tag ${signal.action === "PAPER_BET" ? "entry" : "wait"}">${esc(signal.action.replace("_"," "))}</span></div>
      <div class="figs"><div class="fig"><div class="key">Consensus prob</div><div class="value">${pct(signal.consensus_probability??signal.model_probability)}</div><div class="hint">One observation per source family</div></div><div class="fig"><div class="key">Display gap</div><div class="value">${signedCents(signal.edge)}</div><div class="hint">Not actionable without calibration and execution gates</div></div><div class="fig"><div class="key">Signal quality</div><div class="value">${signal.confidence.toFixed(0)}/100</div><div class="hint">Reliability, not win probability</div></div></div></div>`;
  }

  function eventCard(view, openDetails) {
    const {event,state_points,quote_points,latest_state:state,actionable_markets:markets,positions,signals} = view;
    const health = view.edge_health;
    const usingFallback = !markets.length && !event.polymarket_slug;
    const anyReference = markets.some(m => (m.reference_sources||0) >= 1);
    let priceOnly = markets.length && !anyReference
      ? '<div class="notice price-only"><strong>Price only</strong> · no sportsbook reference matched yet, so there\'s no validated edge here.</div>'
      : "";
    if (health) {
      const sources = health.fresh_reference_sources.length ? health.fresh_reference_sources.map(esc).join(", ") : "none";
      priceOnly = `<div class="notice"><strong>Edge pipeline: ${esc(health.status.replaceAll("_"," "))}</strong> · ${esc(health.message)} Fresh references: ${sources}.</div>` + priceOnly;
    }
    let mkts = markets, sigs = signals.slice(0,3);
    if (activeLine !== "all") {
      mkts = markets.filter(m => lineType(m.market,m.outcome) === activeLine);
      sigs = sigs.filter(s => lineType(s.market,s.outcome) === activeLine);
    }
    if (activeLine !== "all" && !mkts.length && !(usingFallback && sigs.length) && !positions.length) return "";
    const score = state ? `${state.home_score}<span class="sep">–</span>${state.away_score}` : "—";
    const portfolio = positions.length ? `<div class="section-strip"><span>My positions</span> · paper hold/cash monitor</div><div class="portfolio">${positions.map(p => positionRow(event.id,p,openDetails)).join("")}</div>` : "";
    const marketBody = mkts.length ? mkts.map(m => marketRow(event.id,m,openDetails)).join("")
      : (usingFallback && sigs.length) ? sigs.map(fallbackSignal).join("")
      : activeLine !== "all" ? '<div class="pending">No matching lines for this filter.</div>'
      : '<div class="pending">Waiting for a fresh executable ask and reference prices…</div>';
    const link = event.polymarket_url ? `<a href="${esc(event.polymarket_url)}" target="_blank" rel="noopener">Open event ↗</a>` : "manual event";
    const restriction = event.polymarket_restricted ? '<strong>Region notice:</strong> Polymarket marks this event restricted. The monitor shows public data only and does not bypass availability rules.' : 'Only selections accepting orders with a visible ask are listed.';
    return `<article class="event" data-event-id="${esc(event.id)}"><div class="event-head"><div><div class="name">${esc(event.name)}</div><div class="meta">${esc(event.sport)} · ${link} · ${state_points} state / ${quote_points} updates</div></div><div class="event-actions"><button class="ghost chart-button" data-chart-event="${esc(event.id)}" data-chart-title="${esc(event.name)}">View Chart</button><div class="score">${score}</div><button class="remove" data-remove-event="${esc(event.id)}">Remove</button></div></div>
      <div class="notice">${restriction}</div>${priceOnly}${portfolio}<div class="section-strip"><span>Actionable selections</span> · buy, sell, margin, and risk</div><div>${marketBody}</div></article>`;
  }

  function showActionError(message) { const box=document.querySelector("#action-error"); box.textContent=message; box.hidden=false; }
  function metricTile(key,value,cls="",sub="") { return `<div class="mtile"><div class="k">${key}</div><div class="v ${cls}">${value}</div>${sub?`<div class="sub2">${sub}</div>`:""}</div>`; }
  function reliabilityView(bins) { if(!bins?.length)return "";const columns=bins.map(bin=>`<div class="rbin" title="predicted ${pct(bin.mean_predicted)} · actual ${pct(bin.empirical_rate)} · n=${bin.count}"><meter class="reliability-meter" min="0" max="1" value="${Number(bin.empirical_rate).toFixed(4)}">${pct(bin.empirical_rate)}</meter><div class="rlabel">${Math.round(bin.lo*100)} · p ${Math.round(bin.mean_predicted*100)}</div></div>`).join("");return `<div class="reliability">${columns}</div><div class="metrics-sub">Meter = actual win rate · label p = predicted probability</div>`; }

  async function refreshMetrics() {
    const body=document.querySelector("#metrics-body"), sub=document.querySelector("#metrics-sub");
    try {
      const response=await fetch("/api/metrics");
      if(!response.ok)return;
      const m=await response.json();
      if(!m?.n_bets){
        sub.textContent="";
        body.innerHTML='<div class="metrics-empty">No eligible paper fills yet. Close-price CLV and calibration appear only after validated signals and event closure.</div>';
        return;
      }
      const clv=m.clv||{}, model=m.model||{}, base=m.market_baseline||{};
      const independent=m.independent_model||{};
      const execution=m.execution||{}, portfolio=m.portfolio||{}, coverage=m.eligibility_coverage||{};
      const opportunities=coverage.all_opportunities==null?"coverage unavailable":`${coverage.all_opportunities} evaluated decision(s)`;
      sub.textContent=`${m.n_bets} paper fill(s) · ${m.n_settled} settled · ${opportunities}`;
      const tiles=[
        metricTile("Beat close",clv.beat_close_rate==null?"—":pct(clv.beat_close_rate),clv.beat_close_rate>=.5?"good":"bad",clv.n?`n=${clv.n}`:"awaiting closes"),
        metricTile("Mean CLV",clv.mean_clv==null?"—":signedCents(clv.mean_clv),clv.mean_clv>=0?"good":"bad","fill vs last executable close"),
        metricTile("Fill rate",execution.fill_rate==null?"—":pct(execution.fill_rate),"",`${execution.filled||0}/${execution.submitted||0} simulated orders`),
        metricTile("Net paper return",execution.net_paper_return==null?"—":money(execution.net_paper_return),execution.net_paper_return>=0?"good":"bad",execution.turnover==null?"":`$${Number(execution.turnover).toFixed(2)} turnover`),
        metricTile("Max drawdown",portfolio.max_drawdown_dollars==null?"—":`$${Number(portfolio.max_drawdown_dollars).toFixed(2)}`,"","settled paper sequence"),
        metricTile("Consensus Brier",model.brier==null?"—":model.brier.toFixed(3),model.brier!=null&&base.brier!=null&&model.brier<base.brier?"good":"",base.brier==null?"awaiting settle":`executable baseline ${base.brier.toFixed(3)}`)
      ];
      if(model.log_loss!=null)tiles.push(metricTile("Log loss",model.log_loss.toFixed(3)));
      if(model.ece!=null)tiles.push(metricTile("ECE",model.ece.toFixed(3),"","calibration gap"));
      if(model.calibration?.slope!=null)tiles.push(metricTile("Calibration slope",model.calibration.slope.toFixed(2),"",`intercept ${model.calibration.intercept.toFixed(2)}`));
      if(independent.brier!=null){
        const paired=independent.same_rows_calibrated_consensus||{};
        tiles.push(metricTile("Independent Brier",independent.brier.toFixed(3),paired.brier!=null&&independent.brier<paired.brier?"good":"",`cross-check n=${independent.n_settled||0} · same-row consensus ${paired.brier==null?"—":paired.brier.toFixed(3)}`));
      }
      const rejected=Object.entries(coverage.rejection_gates||{}).map(([code,count])=>`${esc(code)} ${count}`).join(" · ")||"none recorded";
      body.innerHTML=`<div class="metric-tiles">${tiles.join("")}</div>`+reliabilityView(m.reliability)+`<div class="metrics-sub">Failed-gate counts: ${rejected}. No statistical edge claim is supported by this report.</div>`;
    } catch {}
  }

  async function viewBot(name) {
    const dialog = document.querySelector("#bot-modal");
    document.querySelector("#bot-modal-title").textContent = name + " Activity";
    document.querySelector("#bot-modal-content").innerHTML = "Loading...";
    dialog.showModal();
    try {
      const r = await fetch(`/api/accounts/${encodeURIComponent(name)}/bets`);
      if (!r.ok) throw new Error();
      const bets = await r.json();
      if (!bets.length) { document.querySelector("#bot-modal-content").innerHTML = '<div class="empty">No bets placed yet.</div>'; return; }
      const rows = bets.map(b => `<div class="game bot-bet">
        <div class="bot-bet-head">
          <div><div class="g-title">${esc(b.event_name)}</div><div class="g-league">${esc(b.market)}: ${esc(b.outcome)}</div></div>
          <div class="align-right"><div class="${b.pnl > 0 ? 'positive' : b.pnl < 0 ? 'negative' : ''}">${money(b.pnl || 0)}</div><div class="g-league">${b.status.toUpperCase()}</div></div>
        </div>
        <div class="bot-bet-meta">
          <span>Stake: $${b.stake.toFixed(2)}</span>
          <span>Entry: ${cents(b.entry_price)}</span>
          <span>Edge: ${pct(b.edge)}</span>
        </div>
      </div>`).join("");
      document.querySelector("#bot-modal-content").innerHTML = rows;
    } catch { document.querySelector("#bot-modal-content").innerHTML = '<div class="error">Failed to load activity</div>'; }
  }

  async function refreshLeaderboard() {
    const body = document.querySelector("#leaderboard-body");
    try {
      const response = await fetch("/api/leaderboard");
      if (!response.ok) return;
      const leaderboard = await response.json();
      if (!leaderboard.length) {
        body.innerHTML = '<div class="metrics-empty">No dummy accounts seeded.</div>';
        return;
      }
      const columns = leaderboard.map(account => `<div class="mtile" title="${esc(account.strategy)}" data-bot="${esc(account.name)}"><div class="k bot-name">${esc(account.name)}</div><div class="v ${account.roi >= 0 ? "good" : "bad"}">${pct(account.roi)}</div><div class="sub2">$${account.equity.toFixed(2)} eq · ${account.win_rate == null ? "—" : pct(account.win_rate)} WR</div><div class="sub2 bot-count">${account.n_bets} bets (${account.wins}W-${account.losses}L)</div></div>`).join("");
      body.innerHTML = `<div class="metric-tiles">${columns}</div>`;
    } catch {}
  }
  function renderEvents(events) {
    if (document.activeElement?.closest("[data-save-position]")) return;
    lastEvents = events;
    const root=document.querySelector("#events");
    const openDetails=new Set([...root.querySelectorAll("details[open][data-detail-key]")].map(d=>d.dataset.detailKey));
    const present=new Set();
    for (const v of events) {
      if ((v.actionable_markets||[]).length) v.actionable_markets.forEach(m=>present.add(lineType(m.market,m.outcome)));
      else if (!v.event.polymarket_slug) (v.signals||[]).slice(0,3).forEach(s=>present.add(lineType(s.market,s.outcome)));
    }
    if (activeLine !== "all" && !present.has(activeLine)) activeLine = "all";
    renderCarousel(present);
    const cards = events.map(e=>eventCard(e,openDetails)).filter(Boolean).join("");
    root.innerHTML = cards ? `<div class="stack">${cards}</div>`
      : (events.length && activeLine !== "all"
          ? `<div class="panel empty"><b>No ${LINE_META[activeLine].label} lines</b>Nothing matches this filter right now.</div>`
          : '<div class="panel empty"><b>No events yet</b>Go to the Discovery tab to begin.</div>');
  }
  async function refresh() {
    if (refreshInFlight || document.activeElement?.closest("[data-save-position]")) return;
    refreshInFlight=true;
    try { const response=await fetch("/api/events"); if(!response.ok) throw new Error(); renderEvents(await response.json()); }
    catch { const root=document.querySelector("#events"); if(!root.children.length) root.innerHTML='<div class="panel empty"><b>Dashboard disconnected</b>Check your connection.</div>'; }
    finally { refreshInFlight=false; }
  }
  function startStream() {
    let source;
    try { source = new EventSource("/api/stream"); } catch { return; }
    source.onmessage = event => { if(!event.data) return; try { renderEvents(JSON.parse(event.data)); refreshMetrics(); } catch {} };
  }

  document.querySelector("#form").addEventListener("submit",async event=>{event.preventDefault();const form=event.currentTarget,button=document.querySelector("#submit-event"),box=document.querySelector("#form-error");
    const payload=Object.fromEntries(new FormData(form));Object.keys(payload).forEach(k=>{if(!payload[k])delete payload[k]});button.disabled=true;box.hidden=true;
    try{const response=await fetch("/api/events",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(payload)});const body=await response.json().catch(()=>({}));if(!response.ok)throw new Error(body.detail||`Could not monitor event (${response.status})`);form.reset();await refresh();
      document.querySelector('[data-tab="tab-live"]').click();
    }
    catch(error){box.textContent=error.message;box.hidden=false}finally{button.disabled=false}});

  document.querySelector("#bot-form").addEventListener("submit", async e => {
    e.preventDefault();
    const form = e.currentTarget, btn = form.querySelector("button"), err = document.querySelector("#bot-error");
    btn.disabled = true; err.hidden = true;
    const sizing = form.querySelector("#bot-sizing").value;
    const mult = Number(form.querySelector("#bot-multiplier").value);
    const payload = {
      name: form.querySelector("#bot-name").value,
      edge_threshold: Number(form.querySelector("#bot-edge").value) / 100,
      sizing: sizing,
      kelly_multiplier: sizing === "kelly" ? mult : 1.0,
      flat_stake: sizing === "flat" ? mult : 100.0,
      start_bankroll: 10000.0,
      webhook_url: form.querySelector("#bot-webhook").value || ""
    };
    try {
      const r = await fetch("/api/accounts", { method: "POST", headers: {"content-type":"application/json"}, body: JSON.stringify(payload) });
      const b = await r.json().catch(()=>({}));
      if (!r.ok) throw new Error(b.detail || "Failed to create bot");
      form.reset();
      await refreshLeaderboard();
    } catch (er) { err.textContent = er.message; err.hidden = false; }
    finally { btn.disabled = false; }
  });

  document.querySelector("#events").addEventListener("submit",async event=>{const form=event.target.closest("[data-save-position]");if(!form)return;event.preventDefault();const button=form.querySelector("button"),data=new FormData(form);button.disabled=true;document.querySelector("#action-error").hidden=true;
    const payload={token_id:form.dataset.tokenId,market:form.dataset.market,outcome:form.dataset.outcome,shares:Number(data.get("shares")),avg_entry_price:Number(data.get("entry_cents"))/100};
    try{const response=await fetch(`/api/events/${encodeURIComponent(form.dataset.eventId)}/positions`,{method:"PUT",headers:{"content-type":"application/json"},body:JSON.stringify(payload)});const body=await response.json().catch(()=>({}));if(!response.ok)throw new Error(body.detail||"Could not save position");document.activeElement.blur();await refresh()}
    catch(error){showActionError(error.message)}finally{button.disabled=false}});
  let currentChart = null;
  async function viewChart(eventId, eventName) {
    const dialog = document.querySelector("#chart-modal");
    document.querySelector("#chart-modal-title").textContent = eventName + " History";
    dialog.showModal();
    try {
      const r = await fetch(`/api/events/${encodeURIComponent(eventId)}/history`);
      if (!r.ok) throw new Error();
      const data = await r.json();

      const ctx = document.getElementById('historyChart').getContext('2d');
      if (currentChart) currentChart.destroy();

      const datasets = [];
      const colors = ['#29e7d6', '#ffcf3f', '#ff4d2e', '#a020f0', '#00ff00', '#ff00ff'];
      let cIdx = 0;

      const byOutcome = {};
      for (const q of data.quotes) {
        if (!byOutcome[q.outcome]) byOutcome[q.outcome] = [];
        byOutcome[q.outcome].push({x: q.observed_at * 1000, y: q.probability * 100});
      }
      for (const [outcome, pts] of Object.entries(byOutcome)) {
        datasets.push({ label: outcome + ' Prob (%)', data: pts, borderColor: colors[cIdx % colors.length], fill: false, stepped: true });
        cIdx++;
      }

      const homeScores = data.states.filter(s => s.home_score != null).map(s => ({x: s.observed_at * 1000, y: s.home_score}));
      if (homeScores.length > 0) datasets.push({ label: 'Home Score', data: homeScores, borderColor: '#ffffff', borderDash: [5, 5], stepped: true });

      currentChart = new Chart(ctx, {
        type: 'line', data: { datasets },
        options: { responsive: true, maintainAspectRatio: false, scales: { x: { type: 'linear', ticks: { callback: v => new Date(v).toLocaleTimeString() } }, y: { min: 0 } }, animation: false }
      });
    } catch {
      console.error("Failed to load chart");
    }
  }

  document.querySelector("#events").addEventListener("click",async event=>{const removeEvent=event.target.closest("[data-remove-event]"),removePosition=event.target.closest("[data-remove-position]"),chartBtn=event.target.closest("[data-chart-event]");
    if(removeEvent){removeEvent.disabled=true;try{const response=await fetch(`/api/events/${encodeURIComponent(removeEvent.dataset.removeEvent)}`,{method:"DELETE"});if(response.ok){await refresh();await refreshMetrics()}}catch{}finally{removeEvent.disabled=false}}
    if(removePosition){removePosition.disabled=true;try{const response=await fetch(`/api/events/${encodeURIComponent(removePosition.dataset.eventId)}/positions/${encodeURIComponent(removePosition.dataset.tokenId)}`,{method:"DELETE"});if(response.ok)await refresh()}catch{}finally{removePosition.disabled=false}}
    if(chartBtn){viewChart(chartBtn.dataset.chartEvent, chartBtn.dataset.chartTitle)}});
  let discoverGames = [];
  function discoverStatus(game){
    if(game.status==="live")return '<span class="g-live">● LIVE</span> ';
    if(game.status==="started")return '<span class="g-started">◌ STARTED</span> ';
    return "";
  }
  function renderDiscover() {
    const list=document.querySelector("#discover-list");
    if(!discoverGames.length){list.innerHTML='<div class="discover-empty">No live or upcoming games found right now.</div>';return}
    const q=(document.querySelector("#discover-search").value||"").toLowerCase();
    const shown=discoverGames.filter(g=>!q||`${g.title} ${g.league||""}`.toLowerCase().includes(q));
    list.innerHTML=shown.length?shown.map(g=>`<div class="game" data-slug="${esc(g.slug)}" role="button" tabindex="0" title="${esc(g.title)}"><div><div class="g-title">${esc(g.title)}</div><div class="g-league">${discoverStatus(g)}${esc(g.league||"sports")}${g.reference_adapter===false?' · PRICE ONLY — NO REFERENCE ADAPTER':''}</div></div><span class="g-add">+ Monitor</span></div>`).join(""):'<div class="discover-empty">No games match.</div>';
  }
  async function loadDiscover() {
    const list=document.querySelector("#discover-list");
    try{const r=await fetch("/api/discover");if(!r.ok)throw new Error();discoverGames=await r.json();renderDiscover()}
    catch{if(!discoverGames.length)list.innerHTML='<div class="discover-empty">Could not load games.</div>'}
  }
  async function monitorGame(slug,row) {
    const box=document.querySelector("#form-error");
    let addBtn = null;
    if(row){
      row.setAttribute("aria-disabled","true");
      addBtn = row.querySelector(".g-add");
      if(addBtn) addBtn.textContent = "Adding...";
    }
    box.hidden=true;
    try{const r=await fetch("/api/events",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({polymarket_url:`https://polymarket.com/event/${slug}`})});
      const body=await r.json().catch(()=>({}));if(!r.ok)throw new Error(body.detail||`Could not monitor (${r.status})`);await refresh();await refreshMetrics();
      document.querySelector('[data-tab="tab-live"]').click();
    }
    catch(error){
      box.textContent=error.message;box.hidden=false;
      if(addBtn) addBtn.textContent = "+ Monitor";
    }
    finally{if(row)row.removeAttribute("aria-disabled")}
  }
  document.querySelector("#discover-list").addEventListener("click",e=>{const row=e.target.closest("[data-slug]");if(row&&row.getAttribute("aria-disabled")!=="true")monitorGame(row.dataset.slug,row)});
  document.querySelector("#discover-search").addEventListener("input",renderDiscover);
  document.querySelector("#discover-refresh").addEventListener("click",loadDiscover);
  document.querySelector("#line-filter").addEventListener("click",e=>{const p=e.target.closest("[data-line]");if(!p)return;activeLine=p.dataset.line;renderEvents(lastEvents);});

  document.addEventListener("click", event => {
    const close = event.target.closest("[data-close-dialog]");
    if (close) document.getElementById(close.dataset.closeDialog)?.close();
    const bot = event.target.closest("[data-bot]");
    if (bot) viewBot(bot.dataset.bot);
  });

  document.querySelector("#auto-monitor-toggle")?.addEventListener("change", async e => {
    try { await fetch("/api/config", { method: "POST", headers: {"content-type":"application/json"}, body: JSON.stringify({ auto_monitor: e.target.checked }) }); } catch {}
  });

  // Only start the app data fetching after successful login or if already authenticated.
  // We check if the events endpoint succeeds. If so, we are logged in, hide overlay immediately.
  async function checkAuthAndStart() {
    try {
      const r = await fetch("/api/events");
      if (r.ok) {
        document.querySelector("#login-overlay").hidden = true;
        startApp();
      }
    } catch {}
  }

  function startApp() {
    fetch("/api/config").then(r=>r.json()).then(c=>{
      document.querySelector("#config").textContent=`Quality ≥ ${c.confidence_threshold} · Base edge ≥ ${(c.edge_threshold*100).toFixed(1)}%`;
      if(document.querySelector("#auto-monitor-toggle")) document.querySelector("#auto-monitor-toggle").checked = !!c.auto_monitor;
    }).catch(()=>document.querySelector("#config").textContent="Thresholds unavailable");

    refresh();refreshMetrics();refreshLeaderboard();startStream();loadDiscover();

    // Set intervals
    if (!window.intervalsStarted) {
      window.intervalsStarted = true;
      setInterval(refresh,10000);setInterval(refreshMetrics,5000);setInterval(refreshLeaderboard,5000);setInterval(loadDiscover,60000);
    }
  }

  checkAuthAndStart();
