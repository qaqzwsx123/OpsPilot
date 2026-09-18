const $ = (selector) => document.querySelector(selector);
let latestSql = "";
let latestApprovalId = "";
let selectedMetric = null;
const currentRole = () => $("#role-selector").value;

const stageNames = { context: "Context Memory", recall: "Recall", writer: "Writer", reviewer: "Reviewer", fix: "Fix", risk: "Risk Guard", runner: "Runner", rag: "Agentic RAG" };

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
    const response = await fetch("/api/v1/query/stream", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ question, requester:"Lenovo", role:currentRole() }) });
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

async function searchKnowledge() {
  const query = $("#knowledge-query").value.trim();
  if (!query) return toast("请输入要检索的问题");
  try {
    const evidence = await (await fetch("/api/v1/knowledge/search?query=" + encodeURIComponent(query))).json();
    const target = $("#knowledge-search-result"); target.hidden = false;
    target.innerHTML = evidence.length ? evidence.map((item) => '<div class="knowledge-evidence"><strong>' + escapeHtml(item.title) + " · 片段 " + (item.chunk_index + 1) + " · 得分 " + item.score + '</strong><p>' + escapeHtml(item.content) + '</p></div>').join("") : '<div class="knowledge-evidence">没有检索到足够匹配的证据。</div>';
  } catch { toast("知识检索失败"); }
}

async function reindexKnowledge() {
  const button = $("#reindex-knowledge"); button.disabled = true; button.textContent = "构建中…";
  try {
    const result = await (await fetch("/api/v1/knowledge/reindex", { method:"POST" })).json();
    toast("索引已重建，共 " + result.chunk_count + " 个知识片段"); loadAudit();
  } catch { toast("索引重建失败"); }
  finally { button.disabled = false; button.textContent = "重建索引"; }
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

async function verifyAuditIntegrity() {
  const button = $("#verify-audit"); button.disabled = true; button.textContent = "校验中…";
  try {
    const result = await (await fetch("/api/v1/audit/integrity")).json();
    const target = $("#evaluation-result"); target.hidden = false;
    target.textContent = result.valid ? `审计链完整：已校验 ${result.checked_events} 条事件，未发现链路断裂。` : `审计链异常：第 ${result.checked_events + 1} 条附近存在断裂，请停止写入并核查数据库。`;
    target.className = "evaluation-result " + (result.valid ? "integrity-ok" : "integrity-broken");
    toast(result.valid ? "审计完整性校验通过" : "发现审计链异常");
  } catch { toast("审计完整性校验失败"); }
  finally { button.disabled = false; button.textContent = "验证完整性"; }
}

document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll(".nav-item").forEach((node) => node.classList.remove("active")); button.classList.add("active");
  document.querySelectorAll(".page").forEach((page) => page.classList.remove("active-page")); $(`#${button.dataset.page}-page`).classList.add("active-page");
  const titles = { agent:"智能运维查询", monitoring:"监控中心", metrics:"指标中心", audit:"审计中心", approval:"审批中心", policy:"权限中心", knowledge:"知识库", tools:"工具中心", skills:"Skills 与 SOP" }; $("#page-title").textContent = titles[button.dataset.page];
  if (button.dataset.page === "monitoring") loadMonitoring(); if (button.dataset.page === "metrics") loadMetricCatalog(); if (button.dataset.page === "audit") loadAudit(); if (button.dataset.page === "approval") loadApprovals(); if (button.dataset.page === "policy") loadPolicies(); if (button.dataset.page === "skills") loadSkills(); if (button.dataset.page === "knowledge") loadKnowledge(); if (button.dataset.page === "tools") loadTools();
}));
$("#run-query").addEventListener("click", runQuery);
document.querySelectorAll("[data-query]").forEach((button) => button.addEventListener("click", () => { document.querySelector('.nav-item[data-page="agent"]').click(); $("#question").value = button.dataset.query; runQuery(); }));
$("#copy-sql").addEventListener("click", async () => { await navigator.clipboard.writeText(latestSql); toast("SQL 已复制到剪贴板"); });
$("#refresh-audit").addEventListener("click", loadAudit);
$("#run-evaluation").addEventListener("click", runEvaluation);
$("#verify-audit").addEventListener("click", verifyAuditIntegrity);
$("#refresh-knowledge").addEventListener("click", loadKnowledge);
$("#search-knowledge").addEventListener("click", searchKnowledge);
$("#reindex-knowledge").addEventListener("click", reindexKnowledge);
$("#refresh-approvals").addEventListener("click", loadApprovals);
$("#refresh-monitoring").addEventListener("click", loadMonitoring);
$("#search-metrics").addEventListener("click", loadMetricCatalog);
$("#refresh-metrics").addEventListener("click", () => { $("#metric-keyword").value = ""; $("#metric-category").value = ""; loadMetricCatalog(); });
$("#analyze-metric").addEventListener("click", () => { if (!selectedMetric) return; document.querySelector('.nav-item[data-page="agent"]').click(); $("#question").value = "查询指标 #" + selectedMetric.id + " 近24小时趋势"; runQuery(); });
$("#role-selector").addEventListener("change", () => { const label = $("#role-selector").selectedOptions[0].textContent; $("#role-label").textContent = label; toast("当前角色已切换为：" + label); loadPolicies(); });
$("#save-knowledge").addEventListener("click", saveKnowledge);
$("#close-skill-detail").addEventListener("click", () => { $("#skill-detail").hidden = true; });
$("#approve-button").addEventListener("click", () => { if (latestApprovalId) resolveApproval(latestApprovalId, $("#approve-button")); });
$("#show-system-info").addEventListener("click", async () => { try { const metric = await (await fetch("/api/v1/metrics")).json(); toast("LLM：" + (metric.llm_enabled ? "已配置" : "离线规则模式") + "；审批写库：" + (metric.approved_writes_enabled ? "已开启" : "安全关闭")); } catch { toast("无法读取运行配置"); } });
$("#show-help").addEventListener("click", () => toast("可查询数据、查看审批与审计、维护知识库，并查看 Skill/SOP。"));
loadSkills(); loadKnowledge(); loadApprovals(); loadMetrics();

function renderApprovals(approvals) {
  const target = $("#approval-list");
  if (!approvals.length) { target.innerHTML = '<div class="empty-state"><strong>暂无审批单</strong><p>高危操作会在这里等待人工确认。</p></div>'; return; }
  target.innerHTML = approvals.map(function(approval) {
    const preview = approval.impact_preview || {};
    const estimate = preview.matched_rows === null || preview.matched_rows === undefined ? "无法预估" : ("预计影响 " + preview.matched_rows + " 行");
    const samples = preview.sample_rows?.length ? "，已抽样 " + preview.sample_rows.length + " 条用于审批核对" : "";
    const impact = '<div class="approval-impact"><b>影响预估</b><span>' + escapeHtml(estimate + samples) + '</span><small>仅执行只读预检，不修改数据</small></div>';
    const decision = approval.decided_by ? '<div class="approval-decision"><b>审批结论</b><span>' + escapeHtml(approval.decided_by) + (approval.decision_comment ? "：" + approval.decision_comment : "：未填写意见") + '</span></div>' : "";
    const result = approval.execution_result ? '<div class="approval-result">执行结果：' + escapeHtml(approval.execution_result.message || approval.execution_result.mode || JSON.stringify(approval.execution_result)) + '</div>' : "";
    const expiry = approval.expires_at && approval.status === "pending" ? '<div class="approval-expiry">审批有效期至：' + new Date(approval.expires_at).toLocaleString("zh-CN", {hour12:false}) + '</div>' : "";
    const action = approval.status === "pending" ? '<div class="approval-action"><button class="warning-button" data-approval="' + approval.id + '">批准并进入安全执行</button><button class="secondary-button" data-reject="' + approval.id + '">拒绝</button></div>' : "";
    const labels = { pending:"待审批", approved:"已批准", approved_safe_mode:"安全模式已批准", executed:"已执行", rejected:"已拒绝", expired:"已过期" };
    return '<article class="approval-item"><div><strong>' + escapeHtml(approval.reason) + '</strong><code>' + escapeHtml(approval.sql) + '</code>' + impact + decision + result + expiry + '<div class="approval-meta">申请人：' + escapeHtml(approval.requester) + ' · ' + new Date(approval.created_at).toLocaleString("zh-CN", {hour12:false}) + '</div>' + action + '</div><span class="approval-status ' + escapeHtml(approval.status) + '">' + (labels[approval.status] || escapeHtml(approval.status)) + '</span></article>';
  }).join("");
  document.querySelectorAll("[data-approval]").forEach((button) => button.addEventListener("click", () => resolveApproval(button.dataset.approval, button)));
  document.querySelectorAll("[data-reject]").forEach((button) => button.addEventListener("click", () => rejectApproval(button.dataset.reject, button)));
}

async function loadApprovals() {
  try { renderApprovals(await (await fetch("/api/v1/approvals")).json()); } catch { $("#approval-list").innerHTML = '<div class="empty-state"><strong>无法加载审批队列</strong></div>'; }
}

async function resolveApproval(id, button) {
  if (!confirm("确认批准该审批单？当前安全模式下只记录审批和影响预估，不会执行写库操作。")) return;
  if (button) { button.disabled = true; button.textContent = "处理中…"; }
  try {
    const comment = prompt("可选：填写审批意见（会写入审计日志）", "风险已核对，同意进入安全模式审批。") || "";
    const response = await fetch("/api/v1/approvals/" + id + "/approve", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ role:currentRole(), actor:"Lenovo", comment }) }); const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "审批失败");
    toast(result.message); loadApprovals(); loadAudit(); loadMetrics();
  } catch (error) { toast(error.message || "审批失败"); }
  finally { if (button) { button.disabled = false; button.textContent = "批准并进入安全执行"; } }
}

async function rejectApproval(id, button) {
  const comment = prompt("请填写拒绝原因（会写入审计日志）", "影响范围或执行窗口不满足要求。") || "";
  if (!confirm("确认拒绝该审批单？此操作不会执行 SQL，也不会修改业务数据。")) return;
  if (button) { button.disabled = true; button.textContent = "处理中…"; }
  try {
    const response = await fetch("/api/v1/approvals/" + id + "/reject", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ role:currentRole(), actor:"Lenovo", comment }) }); const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "拒绝失败");
    toast(result.message); loadApprovals(); loadAudit();
  } catch (error) { toast(error.message || "拒绝失败"); }
  finally { if (button) { button.disabled = false; button.textContent = "拒绝"; } }
}

async function loadPolicies() {
  try {
    const data = await (await fetch("/api/v1/policies")).json(); const active = currentRole();
    $("#role-cards").innerHTML = data.roles.map((role) => '<article class="card role-card ' + (role.id === active ? "active-role" : "") + '"><p class="section-label">' + escapeHtml(role.id.toUpperCase()) + '</p><h3>' + escapeHtml(role.label) + '</h3><p>' + escapeHtml(role.description) + '</p><div>' + role.permissions.map((permission) => '<span>' + escapeHtml(permission) + '</span>').join("") + '</div></article>').join("");
    $("#policy-table").innerHTML = data.policies.map((policy) => '<div class="policy-row"><div>' + escapeHtml(policy.operation) + '</div><div class="policy-tier ' + escapeHtml(policy.tier) + '">' + escapeHtml(policy.tier) + '</div><strong>' + escapeHtml(policy.required_permission) + '</strong></div>').join("");
  } catch { $("#role-cards").innerHTML = '<div class="empty-state"><strong>无法加载权限策略</strong></div>'; }
}

function renderStateRows(target, rows, colors) {
  const maximum = Math.max.apply(null, rows.map((row) => row.count).concat([1]));
  target.innerHTML = rows.map((row, index) => '<div class="state-row"><span>' + escapeHtml(row.status || row.severity) + '</span><i><b style="width:' + Math.round(row.count / maximum * 100) + '%;background:' + (colors[index % colors.length]) + '"></b></i><em>' + row.count + '</em></div>').join("");
}

async function loadMonitoring() {
  try {
    const data = await (await fetch("/api/v1/monitoring/overview")).json();
    const assetTotal = data.asset_states.reduce((sum, item) => sum + item.count, 0);
    const alertTotal = data.alert_severity.reduce((sum, item) => sum + item.count, 0);
    $("#monitoring-summary").innerHTML = '<article><strong>' + assetTotal + '</strong><small>纳管设备资产</small></article><article><strong>' + alertTotal + '</strong><small>未关闭告警</small></article><article><strong>' + data.open_tickets + '</strong><small>待处理工单</small></article>';
    renderStateRows($("#asset-state-list"), data.asset_states, ["#26ad75", "#e88b42", "#7668ed"]);
    renderStateRows($("#alert-severity-list"), data.alert_severity, ["#ed6d61", "#e99b47", "#796dec"]);
    $("#monitoring-alert-list").innerHTML = renderRows(data.latest_alerts);
  } catch { $("#monitoring-summary").innerHTML = '<div class="empty-state"><strong>无法加载监控数据</strong></div>'; }
}

function renderTools(tools) {
  const target = $("#tool-list");
  target.innerHTML = tools.map((tool) => '<article class="card tool-card"><span class="risk ' + escapeHtml(tool.risk) + '">' + escapeHtml(tool.risk.toUpperCase()) + '</span><p class="section-label">' + escapeHtml(tool.category) + '</p><h3>' + escapeHtml(tool.name) + '</h3><p>' + escapeHtml(tool.description) + '</p><button class="text-button" data-tool="' + escapeHtml(tool.name) + '">试运行工具</button></article>').join("");
  document.querySelectorAll("[data-tool]").forEach((button) => button.addEventListener("click", () => invokeTool(button.dataset.tool, button)));
}

async function loadTools() {
  try { renderTools(await (await fetch("/api/v1/tools")).json()); } catch { $("#tool-list").innerHTML = '<div class="empty-state"><strong>工具中心加载失败</strong></div>'; }
}

async function invokeTool(name, button) {
  button.disabled = true; button.textContent = "调用中…";
  try {
    const response = await fetch("/api/v1/tools/" + encodeURIComponent(name) + "/invoke", { method:"POST" });
    const result = await response.json(); const box = $("#tool-result"); box.hidden = false;
    box.textContent = JSON.stringify(result, null, 2); toast("工具调用状态：" + result.status); loadAudit();
  } catch { toast("工具调用失败"); }
  finally { button.disabled = false; button.textContent = "试运行工具"; }
}

function renderMetricCatalog(metrics) {
  const target = $("#metric-list");
  target.innerHTML = metrics.length ? metrics.map((metric) => '<article class="metric-item"><div><strong>' + escapeHtml(metric.name) + '</strong><small>' + escapeHtml(metric.category) + ' · ' + escapeHtml(metric.asset_scope) + ' · ' + escapeHtml(metric.unit) + '</small></div><button class="text-button" data-metric="' + metric.id + '">查看趋势</button></article>').join("") : '<div class="empty-state"><strong>没有匹配指标</strong><p>尝试切换分类或关键词。</p></div>';
  document.querySelectorAll("[data-metric]").forEach((button) => button.addEventListener("click", () => loadMetricTrend(button.dataset.metric)));
}

async function loadMetricCatalog() {
  const keyword = $("#metric-keyword").value.trim(); const category = $("#metric-category").value;
  try {
    const response = await fetch("/api/v1/metric-definitions?limit=60&keyword=" + encodeURIComponent(keyword) + "&category=" + encodeURIComponent(category));
    const metrics = await response.json(); renderMetricCatalog(metrics);
    const system = await (await fetch("/api/v1/metrics")).json();
    $("#metric-count-note").textContent = system.metric_definitions || metrics.length;
  } catch { $("#metric-list").innerHTML = '<div class="empty-state"><strong>无法加载指标目录</strong></div>'; }
}

async function loadMetricTrend(metricId) {
  try {
    const metric = await (await fetch("/api/v1/metric-definitions/" + metricId + "/trend")).json();
    selectedMetric = metric; $("#trend-card").hidden = false; $("#trend-title").textContent = metric.name + " · 24H 趋势";
    const values = metric.points.map((point) => point.value); const minimum = Math.min.apply(null, values); const maximum = Math.max.apply(null, values); const span = maximum - minimum || 1;
    const points = values.map((value, index) => (index * 600 / Math.max(1, values.length - 1)).toFixed(1) + "," + (145 - (value - minimum) / span * 118).toFixed(1)).join(" ");
    const area = "0,160 " + points + " 600,160";
    $("#trend-chart").innerHTML = '<polygon class="area" points="' + area + '"></polygon><polyline points="' + points + '"></polyline>';
    const latest = values[values.length - 1]; const average = values.reduce((sum, value) => sum + value, 0) / values.length;
    $("#trend-summary").innerHTML = '<span>当前值<strong>' + latest + " " + escapeHtml(metric.unit) + '</strong></span><span>24H 平均<strong>' + average.toFixed(2) + " " + escapeHtml(metric.unit) + '</strong></span><span>最小 / 最大<strong>' + minimum + " / " + maximum + '</strong></span>';
    $("#trend-axis").innerHTML = '<span>' + new Date(metric.points[0].observed_at).toLocaleTimeString("zh-CN", {hour:"2-digit", minute:"2-digit"}) + '</span><span>现在</span>';
    $("#trend-card").scrollIntoView({ behavior:"smooth", block:"nearest" });
  } catch { toast("无法加载指标趋势"); }
}
