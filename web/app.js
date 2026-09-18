const $ = (selector) => document.querySelector(selector);
let latestSql = "";
let latestApprovalId = "";

const stageNames = { context: "Context Memory", recall: "Recall", writer: "Writer", reviewer: "Reviewer", risk: "Risk Guard", runner: "Runner", rag: "Agentic RAG" };

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", "'":"&#39;", '"':"&quot;" })[character]);
}

function toast(message) {
  const node = $("#toast"); node.textContent = message; node.classList.add("show");
  setTimeout(() => node.classList.remove("show"), 2600);
}

function setTrace(events, status) {
  const trace = $("#trace-list");
  if (!events.length) return;
  trace.innerHTML = events.map((event, index) => {
    const state = status === "running" ? (index === events.length - 1 ? "active" : "complete") : (status === "blocked" || status === "approval_required" ? (index === events.length - 1 ? "blocked" : "complete") : "complete");
    const details = event.details && Object.keys(event.details).length ? Object.values(event.details)[0] : "";
    const short = Array.isArray(details) ? details.join("、") : String(details || event.message);
    return `<li class="${state}"><span class="trace-node">${state === "complete" ? "✓" : state === "active" ? "…" : "!"}</span><div><strong>${stageNames[event.stage] || event.stage}</strong><small>${escapeHtml(short)}</small></div></li>`;
  }).join("");
  const badge = $("#trace-status"); badge.textContent = status === "running" ? "Agent 执行中" : status === "completed" ? "执行完成" : status === "answered_by_rag" ? "RAG 已回答" : status === "approval_required" ? "等待审批" : "已阻断";
  badge.className = `trace-status ${status === "running" ? "running" : status === "completed" || status === "answered_by_rag" ? "done" : "blocked"}`;
}

function renderRows(rows) {
  if (!rows?.length) return "<div class='empty-state'><strong>没有匹配记录</strong><p>可以调整查询条件后重试。</p></div>";
  const columns = Object.keys(rows[0]);
  return `<table><thead><tr>${columns.map((column) => `<th>${escapeHtml(column)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${columns.map((column) => `<td>${escapeHtml(row[column])}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

function renderResult(result) {
  $("#result-empty").hidden = true; $("#result-content").hidden = false;
  const status = $("#result-status"); status.textContent = result.answer; status.className = `result-status ${result.status}`;
  const sql = $("#sql-code"); latestSql = result.sql || ""; sql.textContent = latestSql; sql.hidden = !latestSql;
  $("#copy-sql").disabled = !latestSql;
  const table = $("#table-wrap"); table.innerHTML = result.status === "completed" ? renderRows(result.rows) : ""; table.hidden = result.status !== "completed";
  const answer = $("#rag-answer"); answer.textContent = result.status === "answered_by_rag" ? result.answer : ""; answer.hidden = result.status !== "answered_by_rag";
  const sources = $("#sources"); sources.innerHTML = result.sources?.length ? `知识来源：${result.sources.map((source) => `<span>${escapeHtml(source)}</span>`).join("")}` : ""; sources.hidden = !result.sources?.length;
  const approval = $("#approval-box"); latestApprovalId = result.approval_id || ""; approval.style.display = result.status === "approval_required" ? "flex" : "none";
  $("#approval-description").textContent = result.status === "approval_required" ? `审批单 ${latestApprovalId.slice(0, 8)}… 已创建。审批后由安全策略决定是否允许执行。` : "";
}

async function runQuery() {
  const question = $("#question").value.trim(); if (!question) return toast("请先输入一个运维问题");
  const button = $("#run-query"); button.disabled = true; button.textContent = "分析中…";
  const badge = $("#trace-status"); badge.textContent = "Agent 执行中"; badge.className = "trace-status running";
  $("#trace-list").innerHTML = `<li class="active"><span class="trace-node">…</span><div><strong>Agent 正在规划</strong><small>召回元数据、生成 SQL 并进行安全审查</small></div></li>`;
  try {
    const response = await fetch("/api/v1/query/stream", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ question, requester:"Lenovo" }) });
    if (!response.ok || !response.body) throw new Error("无法建立流式连接");
    const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ""; const events = []; let resultReceived = false;
    const consumeFrame = (frame) => {
      if (!frame || frame.startsWith(":")) return;
      const event = frame.match(/^event:\s*(.+)$/m)?.[1]; const raw = frame.match(/^data:\s*(.+)$/m)?.[1];
      if (!event || !raw) return; const payload = JSON.parse(raw);
      if (event === "stage") { events.push(payload); setTrace(events, "running"); }
      if (event === "result") { resultReceived = true; renderResult(payload); setTrace(events, payload.status); loadAudit(); loadMetrics(); }
      if (event === "error") throw new Error(payload.message || "工作流失败");
    };
    while (true) { const { value, done } = await reader.read(); buffer += decoder.decode(value || new Uint8Array(), { stream: !done }); let boundary; while ((boundary = buffer.indexOf("\n\n")) >= 0) { consumeFrame(buffer.slice(0, boundary)); buffer = buffer.slice(boundary + 2); } if (done) break; }
    if (!resultReceived) throw new Error("流式响应未返回结果");
  } catch (error) { toast(error.message || "服务请求失败"); badge.textContent = "请求失败"; badge.className = "trace-status blocked"; }
  finally { button.disabled = false; button.innerHTML = "运行查询 <span>↗</span>"; }
}

async function loadAudit() {
  const target = $("#audit-list");
  try {
    const response = await fetch("/api/v1/audit?limit=30"); const rows = await response.json();
    target.innerHTML = rows.length ? rows.map((row) => `<div class="audit-row"><span class="audit-action">${escapeHtml(row.action)}</span><span class="audit-payload">${escapeHtml(row.payload.question || row.payload.sql || row.payload.sources?.join("、") || "系统事件")}</span><span class="audit-time">${new Date(row.created_at).toLocaleString("zh-CN", {hour12:false})}</span></div>`).join("") : "<div class='empty-state'><strong>暂无审计记录</strong></div>";
  } catch { target.innerHTML = "<div class='empty-state'><strong>无法加载审计记录</strong></div>"; }
}

async function loadSkills() {
  const target = $("#skills-list");
  try {
    const skills = await (await fetch("/api/v1/skills")).json();
    target.innerHTML = skills.map((skill, index) => `<article class="card skill-card"><span class="skill-symbol">${index ? "⌘" : "◈"}</span><h3>${escapeHtml(skill.name)}</h3><p>${escapeHtml(skill.description || "可复用的运维领域能力")}</p><span class="skill-tag">SKILL.md 已加载</span><br><button class="text-button" data-skill="${escapeHtml(skill.name)}">查看完整 SOP</button></article>`).join("");
    document.querySelectorAll("[data-skill]").forEach((button) => button.addEventListener("click", () => viewSkill(button.dataset.skill)));
  } catch { target.innerHTML = "<p>加载 Skills 失败。</p>"; }
}

async function viewSkill(name) {
  try {
    const skill = await (await fetch(`/api/v1/skills/${encodeURIComponent(name)}`)).json();
    $("#skill-content").textContent = skill.content; $("#skill-detail").hidden = false;
    $("#skill-detail").scrollIntoView({ behavior:"smooth", block:"start" });
  } catch { toast("无法读取 Skill 内容"); }
}

function renderKnowledge(documents) {
  const target = $("#knowledge-list");
  target.innerHTML = documents.length ? documents.map((document) => `<article class="knowledge-item"><strong>${escapeHtml(document.title)}</strong><p>${escapeHtml(document.content)}</p><div>${String(document.tags || "未分类").split(/[,，]/).filter(Boolean).map((tag) => `<span>${escapeHtml(tag.trim())}</span>`).join("")}</div></article>`).join("") : "<div class='empty-state'><strong>知识库为空</strong><p>新增一份 SOP 后即可在 RAG 中使用。</p></div>";
}

async function loadKnowledge() {
  try { renderKnowledge(await (await fetch("/api/v1/knowledge")).json()); } catch { $("#knowledge-list").innerHTML = "<div class='empty-state'><strong>无法加载知识库</strong></div>"; }
}

async function saveKnowledge() {
  const title = $("#knowledge-title").value.trim(); const tags = $("#knowledge-tags").value.trim() || "未分类"; const content = $("#knowledge-content").value.trim();
  if (title.length < 2 || content.length < 10) return toast("标题至少 2 个字符，正文至少 10 个字符");
  const button = $("#save-knowledge"); button.disabled = true; button.textContent = "保存中…";
  try {
    const response = await fetch("/api/v1/knowledge", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ title, tags, content }) });
    const document = await response.json(); if (!response.ok) throw new Error(document.detail || "保存失败");
    $("#knowledge-title").value = ""; $("#knowledge-tags").value = ""; $("#knowledge-content").value = "";
    toast(`“${document.title}” 已纳入 RAG`); loadKnowledge(); loadMetrics();
  } catch (error) { toast(error.message || "保存失败"); }
  finally { button.disabled = false; button.innerHTML = "保存并纳入 RAG <span>↗</span>"; }
}

async function loadMetrics() {
  try {
    const metric = await (await fetch("/api/v1/metrics")).json();
    $("#metric-tables").textContent = metric.demo_tables;
    $("#metric-knowledge").textContent = metric.knowledge_documents;
    $("#metric-audit").textContent = metric.audit_events;
  } catch { /* metrics do not block the console */ }
}

async function runEvaluation() {
  const button = $("#run-evaluation"); button.disabled = true; button.textContent = "评测中…";
  try {
    const report = await (await fetch("/api/v1/evaluations/run", { method:"POST" })).json();
    $("#metric-eval").textContent = `${report.success_rate}%`;
    const target = $("#evaluation-result"); target.hidden = false;
    target.textContent = `评测完成：${report.passed}/${report.dataset_size} 通过；SQL 首次通过率 ${report.first_pass_sql_rate}%；高危拦截率 ${report.high_risk_interception_rate}%。`;
    toast("离线评测已完成"); loadAudit(); loadMetrics();
  } catch { toast("评测执行失败"); }
  finally { button.disabled = false; button.textContent = "运行评测"; }
}

document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll(".nav-item").forEach((node) => node.classList.remove("active")); button.classList.add("active");
  document.querySelectorAll(".page").forEach((page) => page.classList.remove("active-page")); $(`#${button.dataset.page}-page`).classList.add("active-page");
  const titles = { agent:"智能运维查询", audit:"审计中心", knowledge:"知识库", skills:"Skills 与 SOP" }; $("#page-title").textContent = titles[button.dataset.page];
  if (button.dataset.page === "audit") loadAudit(); if (button.dataset.page === "skills") loadSkills(); if (button.dataset.page === "knowledge") loadKnowledge();
}));
$("#run-query").addEventListener("click", runQuery);
document.querySelectorAll("[data-query]").forEach((button) => button.addEventListener("click", () => { $("#question").value = button.dataset.query; runQuery(); }));
$("#copy-sql").addEventListener("click", async () => { await navigator.clipboard.writeText(latestSql); toast("SQL 已复制到剪贴板"); });
$("#refresh-audit").addEventListener("click", loadAudit);
$("#run-evaluation").addEventListener("click", runEvaluation);
$("#refresh-knowledge").addEventListener("click", loadKnowledge);
$("#save-knowledge").addEventListener("click", saveKnowledge);
$("#close-skill-detail").addEventListener("click", () => { $("#skill-detail").hidden = true; });
$("#approve-button").addEventListener("click", async () => { if (!latestApprovalId) return; const response = await fetch(`/api/v1/approvals/${latestApprovalId}/approve`, { method:"POST" }); const result = await response.json(); if (response.ok) { toast(result.message); $("#approve-button").disabled = true; $("#approve-button").textContent = result.outcome === "executed" ? "已执行" : "已审批"; loadAudit(); loadMetrics(); } else toast(result.detail || "审批失败"); });
loadSkills(); loadKnowledge(); loadMetrics();
