
    const el = id => document.getElementById(id);
    const setHtml = (id, html) => el(id).innerHTML = html;
    const setText = (id, txt) => el(id).textContent = txt;
    
    async function postJson(url, payload) {
      const res = await fetch(url, { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload) });
      return res.json();
    }
    
    function logResult(title, payload) {
      const node = document.createElement("div");
      node.className = "item";
      node.innerHTML = `<div><strong>${title}</strong></div><div class="meta">${new Date().toLocaleTimeString()} | ${JSON.stringify(payload)}</div>`;
      el("command-output-list").prepend(node);
    }
    const esc = str => String(str).replace(/[&<>"']/g, m => ({'&': '&amp;','<': '&lt;','>': '&gt;','"': '&quot;',"'": '&#39;'}[m]));
    const REC_PAGE_SIZE = 10, HOLDING_PAGE_SIZE = 8, BIAS_PAGE_SIZE = 8;
    let recRows = [], holdingRows = [], biasRows = [];
    let recPage = 1, holdingPage = 1, biasPage = 1;

    function downloadCsv(filename, rows, columns) {
      if (!rows.length) return alert("暂无可导出数据");
      const escCsv = val => `"${String(val ?? "").replace(/"/g, """")}"`;
      const head = columns.map(c => escCsv(c.label)).join(",");
      const body = rows.map(r => columns.map(c => escCsv(r[c.key])).join(",")).join("
");
      const csv = "﻿" + head + "
" + body;
      const blob = new Blob([csv], {type: "text/csv;charset=utf-8;"});
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    }

    function renderRecommendationTable() {
      const symbolFilter = (el("rec-filter-symbol").value || "").trim().toUpperCase();
      const statusFilter = (el("rec-filter-status").value || "").trim().toLowerCase();
      const sortField = (el("rec-sort-field").value || "updated_at").trim();
      let rows = recRows.filter(r => {
        const symbolOk = !symbolFilter || String(r.symbol || "").toUpperCase().includes(symbolFilter);
        const statusOk = !statusFilter || String(r.status || "").toLowerCase() === statusFilter;
        return symbolOk && statusOk;
      });
      rows = rows.sort((a, b) => {
        if (sortField === "last_signal_score") return Number(b.last_signal_score || 0) - Number(a.last_signal_score || 0);
        return String(b.updated_at || "").localeCompare(String(a.updated_at || ""));
      });
      const pages = Math.max(1, Math.ceil(rows.length / REC_PAGE_SIZE));
      recPage = Math.max(1, Math.min(recPage, pages));
      const slice = rows.slice((recPage - 1) * REC_PAGE_SIZE, recPage * REC_PAGE_SIZE);
      setText("rec-page-info", `第 ${recPage}/${pages} 页 (${rows.length}条)`);
      setHtml("recommendation-body", slice.length ? slice.map(r => `<tr><td>${esc(r.symbol)}</td><td>${esc(r.status)}</td><td>${esc(r.strategy)}</td><td>${Number(r.last_signal_score || 0).toFixed(2)}</td><td>${esc(r.updated_at || "")}</td><td>${esc(r.note || "")}</td></tr>`).join("") : "<tr><td colspan='6'>暂无推荐跟踪记录</td></tr>");
    }

    function renderHoldingAlerts() {
      const severityFilter = (el("holding-filter-severity").value || "").trim().toLowerCase();
      let rows = holdingRows.filter(r => !severityFilter || String(r.severity || "").toLowerCase() === severityFilter);
      const pages = Math.max(1, Math.ceil(rows.length / HOLDING_PAGE_SIZE));
      holdingPage = Math.max(1, Math.min(holdingPage, pages));
      const slice = rows.slice((holdingPage - 1) * HOLDING_PAGE_SIZE, holdingPage * HOLDING_PAGE_SIZE);
      setText("holding-page-info", `第 ${holdingPage}/${pages} 页 (${rows.length}条)`);
      setHtml("holding-alert-list", slice.length ? slice.map(r => `<div class="item"><div><strong>${esc(r.symbol)}</strong> ${esc(r.reason)} (${esc(r.severity)})</div><div class="meta">P&L ${Number((r.pnl_pct || 0) * 100).toFixed(2)}% | 持仓 ${Number(r.hold_days || 0)} 天 | 最新价 ${Number(r.latest_price || 0).toFixed(2)}</div></div>`).join("") : "<div class='item'>暂无持仓预警</div>");
    }

    function renderBiasList() {
      const symbolFilter = (el("bias-filter-symbol").value || "").trim().toUpperCase();
      let rows = biasRows.filter(r => !symbolFilter || String(r.symbol || "").toUpperCase().includes(symbolFilter));
      rows = rows.sort((a, b) => Math.abs(Number(b.price_bias_pct || 0)) - Math.abs(Number(a.price_bias_pct || 0)));
      const pages = Math.max(1, Math.ceil(rows.length / BIAS_PAGE_SIZE));
      biasPage = Math.max(1, Math.min(biasPage, pages));
      const slice = rows.slice((biasPage - 1) * BIAS_PAGE_SIZE, biasPage * BIAS_PAGE_SIZE);
      setText("bias-page-info", `第 ${biasPage}/${pages} 页 (${rows.length}条)`);
      setHtml("bias-list", slice.length ? slice.map(r => `<div class="item"><div><strong>${esc(r.symbol)}</strong> 仓位偏差 ${Number(r.position_bias || 0).toFixed(4)}</div><div class="meta">${esc(r.timestamp || "")} | 价格偏差 ${Number((r.price_bias_pct || 0) * 100).toFixed(2)}% | 目标 ${Number(r.target_position || 0).toFixed(3)} vs 推荐 ${Number(r.recommended_target_position || 0).toFixed(3)}</div></div>`).join("") : "<div class='item'>暂无成交偏差记录</div>");
    }

    async function loadDash() {
      setText("sync-status", "同步中...");
      try {
        const [dashRes, opsRes, idleRes, cacheRes, historyRes] = await Promise.all([
          fetch("/dashboard/portfolio?days=7&trade_limit=120"),
          fetch("/dashboard/ops/state"),
          fetch("/idle/state"),
          fetch("/news/score/cache/state"),
          fetch("/news/score/history?limit=8")
        ]);
        const db = await dashRes.json() || {},
              ops = await opsRes.json() || {},
              idle = await idleRes.json() || {},
              cacheState = await cacheRes.json() || {},
              newsHistory = await historyRes.json() || {};
        
        // Cards
        const summ = db.summary || {}, qual = db.execution_quality || {}, wk7 = db.week7_kill_switch || {};
        const align = ((qual.reconcile_alignment_rate || 0) * 100).toFixed(2);
        setText("card-equity", Number(summ.current_equity || 0).toFixed(4));
        setText("card-positions", summ.open_positions || 0);
        setText("card-align", align + "%");
        el("card-align").className = "value " + (align >= 95 ? "good" : "bad");
        setText("card-week7-pause", wk7.pause_new_buy ? "是 (暂停)" : "否 (正常)");
        el("card-week7-pause").className = "value " + (wk7.pause_new_buy ? "bad" : "good");
        
        // Ops Mode
        const en = Boolean(ops.enabled);
        setText("ops-status", en ? "操作: 允许" : "操作: 禁用");
        el("ops-status").style.color = en ? "#4ddf7e" : "#ff6f59";
        const advisoryOnly = Boolean(ops.advisory_only);
        setText("execution-mode-status", advisoryOnly ? "执行模式: 仅建议" : "执行模式: 自动落地");
        el("execution-mode-status").style.color = advisoryOnly ? "#ffb86b" : "#4ddf7e";
        
        // Positions
        const pos = db.positions_panel || [];
        setHtml("positions-body", pos.length ? pos.map(r => `<tr><td>${esc(r.symbol)}</td><td>${esc(r.strategy)}</td><td>${Number(r.target_position).toFixed(3)}</td><td>${Number(r.entry_price || 0).toFixed(2)}</td><td>${Number(r.quantity || 0)}</td><td>${r.hold_days}</td></tr>`).join("") : "<tr><td colspan='6'>无持仓</td></tr>");
        
        // Trades
        const trds = db.recent_trades || [];
        setHtml("trades-list", trds.length ? [...trds].reverse().map(r => `<div class="item"><div><strong>${esc(r.side)}</strong> ${esc(r.symbol)} (${esc(r.strategy)})</div><div class="meta">${esc(r.timestamp)} | ${esc(r.reason)}</div></div>`).join("") : "<div class='item'>暂无记录</div>");

        // Recommendation lifecycle
        const recPanel = db.recommendation_panel || {};
        recRows = recPanel.items || [];
        renderRecommendationTable();
        holdingRows = (db.holding_alerts || {}).items || [];
        renderHoldingAlerts();
        const bias = db.execution_bias || {};
        const biasSummary = bias.summary || {};
        const avgPosBias = Number(biasSummary.avg_abs_position_bias || 0).toFixed(4);
        const avgPriceBiasPct = Number((biasSummary.avg_abs_price_bias_pct || 0) * 100).toFixed(2);
        setHtml("bias-summary", `<div><strong>记录:</strong> ${Number(bias.records || 0)} | <strong>平均仓位偏差:</strong> ${avgPosBias} | <strong>平均价格偏差:</strong> ${avgPriceBiasPct}%</div>`);
        biasRows = bias.items || [];
        renderBiasList();

        // News Watchlist Preview
        const news = db.news_watchlist_preview || {};
        const newsSummary = news.summary || {};
        const newsAvg = Number(newsSummary.average_news_component || 0).toFixed(3);
        const newsRecords = Number(news.records || 0);
        const newsSource = news.source || "watchlist";
        setHtml("news-watch-summary", `<div><strong>来源:</strong> ${esc(newsSource)} | <strong>记录:</strong> ${newsRecords} | <strong>均值:</strong> ${newsAvg}</div>`);
        const newsItems = news.items || [];
        setHtml("news-watch-list", newsItems.length ? newsItems.slice(0, 6).map(r => `<div class="item"><div><strong>${esc(r.symbol || "")}</strong> 分数 ${Number(r.news_component || 0).toFixed(3)}</div><div class="meta">状态: ${esc(r.status || "unknown")} | 策略: ${esc(r.strategy || "trend")}</div></div>`).join("") : "<div class='item'>暂无新闻因子数据</div>");
        const cacheEntries = Number(cacheState.entries || 0);
        const cacheTtlSec = Number(cacheState.ttl_sec || 0);
        setText("news-cache-status", `缓存: ${cacheEntries} 条 | TTL: ${cacheTtlSec}s`);
        const historySummary = newsHistory.summary || {};
        const historyRecords = Number(newsHistory.records || 0);
        const historyAvg = Number(historySummary.average_news_component || 0).toFixed(3);
        setHtml("news-history-summary", `<div><strong>历史记录:</strong> ${historyRecords} | <strong>均值:</strong> ${historyAvg}</div>`);
        const historyItems = newsHistory.items || [];
        setHtml("news-history-list", historyItems.length ? [...historyItems].reverse().map(r => `<div class="item"><div><strong>${esc(r.symbol || "")}</strong> 分数 ${Number(r.news_component || 0).toFixed(3)} (${esc(r.strategy || "trend")})</div><div class="meta">${esc(r.timestamp || "")} | ${esc(r.status || "unknown")}</div></div>`).join("") : "<div class='item'>暂无新闻历史记录</div>");
        
        // Blocked Task
        const blocked = idle.blocked_tasks || {}; const bkeys = Object.keys(blocked);
        setHtml("idle-blocked-list", bkeys.length ? bkeys.map(k => `<div class="item"><div><strong>${k}</strong> 原因: ${esc(blocked[k].reason)}</div><div class="meta">阻塞时间: ${esc(blocked[k].blocked_since)}</div></div>`).join("") : "<div class='item'>无需批准的任务</div>");
        
        setText("sync-status", "最后更新: " + new Date().toLocaleTimeString());
      } catch (err) {
        setText("sync-status", "同步失败");
      }
    }
    
    // Commands Actions
    async function qCmd(action, payload) { try { const d = await postJson("/dashboard/command/quick", {action, payload}); logResult(action, d); loadDash(); } catch (e) { logResult(action, {error: String(e)}); } }
    
    el("refresh-btn").onclick = loadDash;
    el("news-cache-refresh-btn").onclick = loadDash;
    el("ops-toggle-btn").onclick = async () => { if(confirm("切换操作权限?")) { await postJson("/dashboard/ops/toggle", {enabled: !el("ops-status").textContent.includes("允许")}); loadDash(); } };
    el("pause-btn").onclick = () => { if(confirm("确定暂停新买入？")) qCmd("PAUSE_NEW_BUY", {}); };
    el("resume-btn").onclick = () => { if(confirm("确定恢复运行？")) qCmd("RESUME_NEW_BUY", {}); };
    el("reconcile-btn").onclick = () => { if(confirm("立即触发对账？")) qCmd("RUN_RECONCILE", {}); };
    
    // Auto-calculate target position
    const calcPos = () => {
      const price = parseFloat(el("set-price-input").value) || 0;
      const vol = parseFloat(el("set-vol-input").value) || 0;
      const total = parseFloat(el("set-total-asset-input").value) || 0;
      if (total > 0 && price > 0 && vol > 0) {
        el("set-target-input").value = (price * vol / total).toFixed(4);
      }
    };
    el("set-price-input").oninput = calcPos;
    el("set-vol-input").oninput = calcPos;
    el("set-total-asset-input").oninput = () => {
      localStorage.setItem("sa_total_asset", el("set-total-asset-input").value);
      calcPos();
    };
    
    // Restore total asset
    const savedAsset = localStorage.getItem("sa_total_asset");
    if (savedAsset) el("set-total-asset-input").value = savedAsset;

    el("set-btn").onclick = () => {
      const sym = el("set-symbol-input").value; const pt = el("set-target-input").value;
      if (!sym || !pt) return alert("请填写代码和仓位");
      const payload = {symbol: sym, target_position: Number(pt), strategy: "manual"};
      const entryPrice = parseFloat(el("set-price-input").value);
      const quantity = parseInt(el("set-vol-input").value || "0", 10);
      const fee = parseFloat(el("set-fee-input").value);
      const account = (el("set-account-input").value || "").trim();
      const tradeTime = (el("set-trade-time-input").value || "").trim();
      const note = (el("set-note-input").value || "").trim();
      if (!Number.isNaN(entryPrice) && entryPrice > 0) payload.entry_price = Number(entryPrice.toFixed(6));
      if (!Number.isNaN(quantity) && quantity > 0) payload.quantity = quantity;
      if (!Number.isNaN(fee) && fee >= 0) payload.fee = Number(fee.toFixed(6));
      if (account) payload.account = account;
      if (tradeTime) payload.trade_time = tradeTime;
      if (note) payload.note = note;
      if(confirm(`确定建仓/调仓 ${sym} 目标仓位 ${pt}?`)) qCmd("SET_POSITION", payload);
    };
    el("close-btn").onclick = () => {
      const sym = el("set-symbol-input").value;
      if (!sym) return alert("请填写代码(建仓输入框提取)");
      const payload = {symbol: sym};
      const exitPrice = parseFloat(el("set-price-input").value);
      const quantity = parseInt(el("set-vol-input").value || "0", 10);
      const fee = parseFloat(el("set-fee-input").value);
      const account = (el("set-account-input").value || "").trim();
      const tradeTime = (el("set-trade-time-input").value || "").trim();
      const note = (el("set-note-input").value || "").trim();
      if (!Number.isNaN(exitPrice) && exitPrice > 0) payload.exit_price = Number(exitPrice.toFixed(6));
      if (!Number.isNaN(quantity) && quantity > 0) payload.quantity = quantity;
      if (!Number.isNaN(fee) && fee >= 0) payload.fee = Number(fee.toFixed(6));
      if (account) payload.account = account;
      if (tradeTime) payload.trade_time = tradeTime;
      if (note) payload.note = note;
      if(confirm(`确定平仓 ${sym}?`)) qCmd("CLOSE_POSITION", payload);
    };
    el("rec-update-btn").onclick = () => {
      const sym = (el("set-symbol-input").value || "").trim();
      const status = (el("rec-status-input").value || "").trim();
      const note = (el("set-note-input").value || "").trim();
      if (!sym) return alert("请填写代码");
      if (!status) return alert("请选择跟踪状态");
      const payload = {symbol: sym, status, strategy: "manual"};
      if (note) payload.note = note;
      if (confirm(`确定更新 ${sym} 状态为 ${status}?`)) qCmd("SET_RECOMMENDATION_STATUS", payload);
    };

    // Sync Snapshot helper
    el("snapshot-btn").onclick = async () => {
      const raw = el("snapshot-input").value.trim();
      if (!raw) return alert("请填写格式 如 600000:20%,300750:15%");
      const items = raw.split(",").filter(i => i.includes(":"));
      const positions = items.map(i => { const p = i.split(":"); return {symbol: p[0], target_position: parseFloat(p[1]) / (p[1].includes("%")?100:1)}; });
      if(!positions.length) return alert("格式未识别");
      if(confirm("确定同步持仓并立刻触发对账？")) {
        try {
          const data = await postJson("/dashboard/reconcile/quick", {positions, run_reconcile: true});
          logResult("同步并对账", data); loadDash();
        } catch(e) { logResult("同步异常", {error: String(e)}); }
      }
    };

    el("news-cache-clear-btn").onclick = async () => {
      const symbol = (el("news-cache-symbol-input").value || "").trim();
      const strategy = (el("news-cache-strategy-input").value || "").trim();
      const target = symbol || strategy ? `symbol=${symbol || "*"} strategy=${strategy || "*"}` : "全部缓存";
      if (!confirm(`确定清理新闻评分缓存？目标: ${target}`)) return;
      try {
        const data = await postJson("/news/score/cache/clear", {symbol, strategy});
        logResult("清空新闻缓存", data);
        loadDash();
      } catch (e) {
        logResult("清空新闻缓存失败", {error: String(e)});
      }
    };
    
    el("idle-ack-task-btn").onclick = async () => {
      if(!el("idle-ack-task-input").value) return alert("请输入Task ID");
      if(confirm("批准该任务运行？")) { await postJson("/idle/ack", {task_id: el("idle-ack-task-input").value, clear_all: false}); loadDash(); }
    };
    el("idle-ack-all-btn").onclick = async () => {
      if(confirm("一键批准所有失败的任务重新尝试？")) { await postJson("/idle/ack", {task_id: "", clear_all: true}); loadDash(); }
    };

    // Recommendation filters / paging / export
    el("rec-filter-symbol").oninput = () => { recPage = 1; renderRecommendationTable(); };
    el("rec-filter-status").onchange = () => { recPage = 1; renderRecommendationTable(); };
    el("rec-sort-field").onchange = () => { recPage = 1; renderRecommendationTable(); };
    el("rec-prev-btn").onclick = () => { recPage = Math.max(1, recPage - 1); renderRecommendationTable(); };
    el("rec-next-btn").onclick = () => { recPage += 1; renderRecommendationTable(); };
    el("rec-export-btn").onclick = () => downloadCsv(
      "recommendation_lifecycle.csv",
      recRows,
      [
        {key: "symbol", label: "symbol"},
        {key: "status", label: "status"},
        {key: "strategy", label: "strategy"},
        {key: "last_signal_score", label: "last_signal_score"},
        {key: "updated_at", label: "updated_at"},
        {key: "note", label: "note"}
      ]
    );

    // Holding filters / paging / export
    el("holding-filter-severity").onchange = () => { holdingPage = 1; renderHoldingAlerts(); };
    el("holding-prev-btn").onclick = () => { holdingPage = Math.max(1, holdingPage - 1); renderHoldingAlerts(); };
    el("holding-next-btn").onclick = () => { holdingPage += 1; renderHoldingAlerts(); };
    el("holding-export-btn").onclick = () => downloadCsv(
      "holding_alerts.csv",
      holdingRows,
      [
        {key: "symbol", label: "symbol"},
        {key: "severity", label: "severity"},
        {key: "reason", label: "reason"},
        {key: "entry_price", label: "entry_price"},
        {key: "latest_price", label: "latest_price"},
        {key: "pnl_pct", label: "pnl_pct"},
        {key: "hold_days", label: "hold_days"},
        {key: "updated_at", label: "updated_at"}
      ]
    );

    // Bias filters / paging / export
    el("bias-filter-symbol").oninput = () => { biasPage = 1; renderBiasList(); };
    el("bias-prev-btn").onclick = () => { biasPage = Math.max(1, biasPage - 1); renderBiasList(); };
    el("bias-next-btn").onclick = () => { biasPage += 1; renderBiasList(); };
    el("bias-export-btn").onclick = () => downloadCsv(
      "execution_bias.csv",
      biasRows,
      [
        {key: "timestamp", label: "timestamp"},
        {key: "symbol", label: "symbol"},
        {key: "strategy", label: "strategy"},
        {key: "recommendation_id", label: "recommendation_id"},
        {key: "target_position", label: "target_position"},
        {key: "recommended_target_position", label: "recommended_target_position"},
        {key: "position_bias", label: "position_bias"},
        {key: "entry_price", label: "entry_price"},
        {key: "reference_price", label: "reference_price"},
        {key: "price_bias_pct", label: "price_bias_pct"}
      ]
    );

    loadDash();
    setInterval(loadDash, 10000);
  