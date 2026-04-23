# encoding: utf-8
import codecs

NEW_DASHBOARD = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>StockAnalyzer 核心控制台</title>
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&family=IBM+Plex+Mono:wght@400;600&display=swap" rel="stylesheet" />
  <style>
    :root {
      --bg: #081826;
      --panel: rgba(15, 36, 52, 0.86);
      --panel-border: rgba(94, 136, 170, 0.35);
      --ink: #d5f3ff;
      --muted: #8fb6cb;
      --accent: #41d6b3;
      --warn: #ff7f50;
      --good: #4ddf7e;
      --bad: #ff6f59;
      --shadow: 0 18px 40px rgba(1, 14, 23, 0.45);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--ink);
      font-family: inherit;
      background: radial-gradient(circle at 15% 20%, rgba(65, 214, 179, 0.25), transparent 35%), linear-gradient(135deg, #061420 0%, #0a2234 52%, #0f1f2a 100%);
      min-height: 100vh;
      padding: 24px 16px 40px;
    }
    .container { max-width: 1000px; margin: 0 auto; display: grid; gap: 14px; }
    .hero { background: linear-gradient(145deg, rgba(17, 43, 62, 0.9), rgba(10, 25, 37, 0.94)); border: 1px solid var(--panel-border); border-radius: 18px; padding: 20px; box-shadow: var(--shadow); }
    .hero h1 { margin: 0; font-size: 1.8rem; }
    .toolbar { margin-top: 14px; display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
    .btn { border: 0; border-radius: 999px; padding: 9px 16px; background: linear-gradient(110deg, #38d2ae, #61e1c4); color: #062437; font-weight: bold; cursor: pointer; transition: 0.2s; }
    .btn.danger { background: linear-gradient(110deg, #ff7f50, #ff6f59); color: white; }
    .btn:hover { transform: translateY(-1px); filter: brightness(1.05); }
    .btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .status { font-family: monospace; font-size: 0.9rem; padding: 5px 10px; border-radius: 999px; border: 1px solid rgba(115, 167, 201, 0.35); background: rgba(16, 43, 61, 0.66); color: var(--muted); }
    .cards { display: grid; gap: 12px; grid-template-columns: repeat(4, 1fr); }
    .card, .panel { background: var(--panel); border: 1px solid var(--panel-border); border-radius: 14px; padding: 14px; box-shadow: var(--shadow); }
    .label { color: var(--muted); font-size: 0.85rem; letter-spacing: 0.05em; font-weight: bold;}
    .value { margin-top: 7px; font-size: 1.5rem; font-weight: 700; }
    .good { color: var(--good); }
    .bad { color: var(--bad); }
    .grid { display: grid; gap: 12px; grid-template-columns: 1fr 1fr; }
    .panel h2 { margin: 2px 0 12px; font-size: 1.1rem; }
    table { width: 100%; border-collapse: collapse; font-size: 0.95rem; }
    th, td { border-bottom: 1px solid rgba(108, 158, 190, 0.25); text-align: left; padding: 8px 6px; }
    th { color: var(--muted); font-size: 0.8rem; text-transform: uppercase; }
    .list { max-height: 250px; overflow: auto; display: flex; flex-direction: column; gap: 8px; }
    .item { border: 1px solid rgba(108, 158, 190, 0.25); border-radius: 10px; padding: 10px; background: rgba(12, 33, 48, 0.66); font-size: 0.9rem; }
    .item .meta { margin-top: 4px; color: var(--muted); font-size: 0.8rem; }
    .trace-input { padding: 8px 10px; border-radius: 8px; border: 1px solid rgba(108, 158, 190, 0.35); background: #0f2a3a; color: #d5f3ff; width: 140px; }
    @media (max-width: 800px) { .cards { grid-template-columns: repeat(2, 1fr); } .grid { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <main class="container">
    <section class="hero">
      <h1>StockAnalyzer 核心控制台</h1>
      <div class="toolbar">
        <button class="btn" id="refresh-btn">🔄 刷新数据</button>
        <span class="status" id="sync-status">就绪</span>
        <span class="status" id="ops-status">操作: 检查中</span>
        <button class="btn" id="ops-toggle-btn">切换操作权限</button>
      </div>
    </section>

    <!-- 核心指标 -->
    <section class="cards">
      <article class="card"><div class="label">净值 (Equity)</div><div class="value" id="card-equity">-</div></article>
      <article class="card"><div class="label">持仓数量</div><div class="value" id="card-positions">-</div></article>
      <article class="card"><div class="label">对账一致率</div><div class="value" id="card-align">-</div></article>
      <article class="card"><div class="label">是否暂停开仓</div><div class="value" id="card-week7-pause">-</div></article>
    </section>

    <section class="grid">
      <article class="panel" style="grid-column: span 2;">
        <h2>⚡ 快捷指令 (Command Deck)</h2>
        <div class="toolbar">
          <input id="set-symbol-input" class="trace-input" type="text" placeholder="代码 (如 300750)" />
          <input id="set-target-input" class="trace-input" type="number" step="0.01" placeholder="仓位 (如 0.15)" />
          <button class="btn" id="set-btn">📍 建仓/调仓</button>
          <button class="btn danger" id="close-btn">🗑️ 平仓</button>
        </div>
        <div class="toolbar" style="margin-top:10px;">
          <input id="snapshot-input" class="trace-input" type="text" style="width:300px;" placeholder="批量同步: 等同发微信 '同步持仓 600000:20%'" />
          <button class="btn" id="snapshot-btn">🔄 同步持仓</button>
          <button class="btn" id="reconcile-btn">✅ 全局对账</button>
        </div>
        <div class="toolbar" style="margin-top:10px;">
          <button class="btn danger" id="pause-btn">⏸️ 暂停新开仓</button>
          <button class="btn" id="resume-btn">▶️ 恢复新开仓</button>
        </div>
      </article>

      <!-- 持仓概览 -->
      <article class="panel">
        <h2>📊 当前持仓 (Open Positions)</h2>
        <table>
          <thead><tr><th>代码</th><th>策略</th><th>目标仓位</th><th>持仓天数</th></tr></thead>
          <tbody id="positions-body"></tbody>
        </table>
      </article>
      
      <!-- 最近成交 -->
      <article class="panel">
        <h2>📜 最近成交 (Recent Trades)</h2>
        <div class="list" id="trades-list"></div>
      </article>
      
      <!-- 闲时阻塞任务 -->
      <article class="panel" style="grid-column: span 2;">
        <h2>⏳ 被阻塞的后台任务 (需人工确认)</h2>
        <div class="toolbar">
          <input id="idle-ack-task-input" class="trace-input" type="text" placeholder="任务ID (如 WD-P0-01)" />
          <button class="btn" id="idle-ack-task-btn">✅ 批准任务</button>
          <button class="btn" id="idle-ack-all-btn">✅ 批准所有</button>
        </div>
        <div class="list" id="idle-blocked-list" style="margin-top:10px;"></div>
      </article>

      <article class="panel" style="grid-column: span 2;">
        <h2>📝 操作日志</h2>
        <div class="list" id="command-output-list"></div>
      </article>
    </section>
  </main>

  <script>
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

    async function loadDash() {
      setText("sync-status", "同步中...");
      try {
        const [dashRes, opsRes, idleRes] = await Promise.all([
          fetch("/dashboard/portfolio?days=7&trade_limit=120"), fetch("/dashboard/ops/state"), fetch("/idle/state")
        ]);
        const db = await dashRes.json() || {}, ops = await opsRes.json() || {}, idle = await idleRes.json() || {};
        
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
        
        // Positions
        const pos = db.positions_panel || [];
        setHtml("positions-body", pos.length ? pos.map(r => `<tr><td>${esc(r.symbol)}</td><td>${esc(r.strategy)}</td><td>${Number(r.target_position).toFixed(3)}</td><td>${r.hold_days}</td></tr>`).join("") : "<tr><td colspan='4'>无持仓</td></tr>");
        
        // Trades
        const trds = db.recent_trades || [];
        setHtml("trades-list", trds.length ? [...trds].reverse().map(r => `<div class="item"><div><strong>${esc(r.side)}</strong> ${esc(r.symbol)} (${esc(r.strategy)})</div><div class="meta">${esc(r.timestamp)} | ${esc(r.reason)}</div></div>`).join("") : "<div class='item'>暂无记录</div>");
        
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
    el("ops-toggle-btn").onclick = async () => { if(confirm("切换操作权限?")) { await postJson("/dashboard/ops/toggle", {enabled: !el("ops-status").textContent.includes("允许")}); loadDash(); } };
    el("pause-btn").onclick = () => { if(confirm("确定暂停新买入？")) qCmd("PAUSE_NEW_BUY", {}); };
    el("resume-btn").onclick = () => { if(confirm("确定恢复运行？")) qCmd("RESUME_NEW_BUY", {}); };
    el("reconcile-btn").onclick = () => { if(confirm("立即触发对账？")) qCmd("RUN_RECONCILE", {}); };
    
    el("set-btn").onclick = () => {
      const sym = el("set-symbol-input").value; const pt = el("set-target-input").value;
      if (!sym || !pt) return alert("请填写代码和仓位");
      if(confirm(`确定建仓/调仓 ${sym} 目标仓位 ${pt}?`)) qCmd("SET_POSITION", {symbol: sym, target_position: Number(pt), strategy: "manual"});
    };
    el("close-btn").onclick = () => {
      const sym = el("set-symbol-input").value;
      if (!sym) return alert("请填写代码(建仓输入框提取)");
      if(confirm(`确定平仓 ${sym}?`)) qCmd("CLOSE_POSITION", {symbol: sym});
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
    
    el("idle-ack-task-btn").onclick = async () => {
      if(!el("idle-ack-task-input").value) return alert("请输入Task ID");
      if(confirm("批准该任务运行？")) { await postJson("/idle/ack", {task_id: el("idle-ack-task-input").value, clear_all: false}); loadDash(); }
    };
    el("idle-ack-all-btn").onclick = async () => {
      if(confirm("一键批准所有失败的任务重新尝试？")) { await postJson("/idle/ack", {task_id: "", clear_all: true}); loadDash(); }
    };

    loadDash();
    setInterval(loadDash, 10000);
  </script>
</body>
</html>
"""

with codecs.open("src/stock_analyzer/main.py", "r", "utf-8") as f:
    content = f.read()

start_marker = '_DASHBOARD_HTML = """<!doctype html>'
# we know it ends with HTML string """
# let's find the first _DASHBOARD_HTML =
start_idx = content.find(start_marker)

# find the next class PipelineRunRequest, and back up to the prev """
next_class_idx = content.find("class PipelineRunRequest", start_idx)
if next_class_idx != -1:
    # find the ending """
    end_idx = content.rfind('"""', start_idx, next_class_idx) + 3
    if start_idx != -1 and end_idx != -1:
        new_content = content[:start_idx] + '_DASHBOARD_HTML = """' + NEW_DASHBOARD + '"""' + content[end_idx:]
        with codecs.open("src/stock_analyzer/main.py", "w", "utf-8") as f:
            f.write(new_content)
        print("Successfully updated main.py with localized simplified dashboard!")
    else:
        print("Failed to find end index")
else:
    print("Failed to find next class index")
