const $ = (selector) => document.querySelector(selector);
let latestSql = "";
let latestApprovalId = "";
let selectedMetric = null;
let explorerCatalog = [];
let explorerOffset = 0;
let auditOffset = 0;
let approvalOffset = 0;
let chatConversationId = "";
let chatConversations = [];
const recordPageSize = 10;
const currentRole = () => $("#role-selector").value;

const stageNames = { context: "Context Memory", recall: "Recall", writer: "Writer", reviewer: "Reviewer", fix: "Fix", risk: "Risk Guard", runner: "Runner", rag: "Agentic RAG" };
let selectedTraceIndex = -1;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", "'":"&#39;", '"':"&quot;" })[character]);
}

function toast(message) {
  const node = $("#toast"); node.textContent = message; node.classList.add("show");
  setTimeout(() => node.classList.remove("show"), 2600);
}

function formatTraceValue(key, value) {
  if (value === undefined || value === null || value === "") return "—";
  if (key === "confidence" && typeof value === "number") return Math.round(value * 100) + "%";
  if (key === "context" && typeof value === "object") {
    const parts = [];
    if (value.before !== undefined) parts.push("原始 " + value.before + " tokens");
    if (value.after !== undefined) parts.push("压缩后 " + value.after + " tokens");
    if (value.before !== undefined && value.after !== undefined) parts.push("减少 " + Math.max(0, Math.round((1 - value.after / Math.max(1, value.before)) * 100)) + "%");
    if (value.strategy) parts.push(value.strategy === "not_needed" ? "无需压缩" : "已归档压缩");
    return parts.join(" · ") || JSON.stringify(value);
  }
  if (Array.isArray(value)) return value.join("、") || "无";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function traceSummary(event) {
  const entries = Object.entries(event.details || {});
  if (!entries.length) return event.message;
  const preferred = entries.find(([key]) => ["reason", "tables", "sql", "issues", "row_count", "mode", "error"].includes(key)) || entries[0];
  return formatTraceValue(preferred[0], preferred[1]);
}

function renderTraceInspector(events, index) {
  const target = $("#trace-inspector");
  const event = events[index];
  if (!event) {
    target.innerHTML = '<span class="trace-inspector-kicker">选择一个节点</span><strong>这里会展示该阶段实际产生的证据。</strong><p>例如召回的表、生成 SQL、审查结论、风险原因或执行结果。</p>';
    return;
  }
  const entries = Object.entries(event.details || {});
  target.innerHTML = '<span class="trace-inspector-kicker">' + escapeHtml(stageNames[event.stage] || event.stage) + ' · 实际输出</span><strong>' + escapeHtml(event.message) + '</strong>' + (entries.length ? '<div class="trace-evidence">' + entries.map(([key, value]) => '<span><b>' + escapeHtml(key) + '</b>' + escapeHtml(formatTraceValue(key, value)) + '</span>').join("") + '</div>' : '<p>该步骤没有额外参数，但已记录其决策结论。</p>');
}

function setTrace(events, status) {
  const trace = $("#trace-list");
  $("#trace-count").textContent = events.length + " 个节点";
  if (!events.length) { renderTraceInspector([], -1); return; }
  if (selectedTraceIndex < 0 || selectedTraceIndex >= events.length) selectedTraceIndex = events.length - 1;
  trace.innerHTML = events.map((event, index) => {
    const state = status === "running" ? (index === events.length - 1 ? "active" : "complete") : (status === "blocked" || status === "approval_required" ? (index === events.length - 1 ? "blocked" : "complete") : "complete");
    const short = traceSummary(event);
    return `<li class="${state}"><button type="button" class="trace-row ${index === selectedTraceIndex ? "selected" : ""}" data-trace-index="${index}"><span class="trace-node">${state === "complete" ? "✓" : state === "active" ? "…" : "!"}</span><span><strong>${stageNames[event.stage] || event.stage}</strong><small>${escapeHtml(short)}</small></span><b class="trace-detail-link">详情</b></button></li>`;
  }).join("");
  document.querySelectorAll("[data-trace-index]").forEach((button) => button.addEventListener("click", () => { selectedTraceIndex = Number(button.dataset.traceIndex); setTrace(events, status); }));
  renderTraceInspector(events, selectedTraceIndex);
  const badge = $("#trace-status"); badge.textContent = status === "running" ? "Agent 执行中" : status === "completed" ? "执行完成" : status === "answered_by_rag" ? "RAG 已回答" : status === "approval_required" ? "等待审批" : "已阻断";
  badge.className = `trace-status ${status === "running" ? "running" : status === "completed" || status === "answered_by_rag" ? "done" : "blocked"}`;
}

function guardItem(label, detail, state, verdict) {
  const icon = state === "pass" ? "✓" : state === "hold" ? "!" : state === "block" ? "×" : "…";
  return '<div><span class="policy-check ' + state + '">' + icon + '</span><div><strong>' + escapeHtml(label) + '</strong><small>' + escapeHtml(detail) + '</small></div><em class="guard-item-state ' + state + '">' + escapeHtml(verdict) + '</em></div>';
}

function updateContextStat(result) {
  const runner = (result.events || []).filter((event) => event.stage === "runner").at(-1);
  const context = runner?.details?.context;
  const reduction = context && Number.isFinite(Number(context.before)) && Number.isFinite(Number(context.after))
    ? Math.max(0, Math.round((1 - Number(context.after) / Math.max(1, Number(context.before))) * 100)) : null;
  const reset = (headline, note) => {
    $("#context-reduction").textContent = headline; $("#context-before").textContent = "—"; $("#context-after").textContent = "—"; $("#context-percent").textContent = "—";
    $("#context-progress-bar").style.width = "0%"; $("#context-note").textContent = note;
  };
  if (!context || result.status !== "completed") {
    if (result.status === "running") reset("计算中", "等待只读 SQL 返回结果后再计算真实 Token 数。");
    else if (result.status === "approval_required") reset("未执行", "本次请求等待审批，未产生可压缩的 SQL 结果。");
    else if (result.status === "answered_by_rag") reset("不适用", "本次由知识库回答，未产生 SQL 结果压缩统计。");
    else if (result.status === "blocked") reset("未执行", "本次请求已被策略拦截，未产生可压缩的 SQL 结果。");
    else reset("等待本次结果", "仅在只读 SQL 返回结果后计算，不使用固定展示值。");
    return;
  }
  $("#context-before").textContent = context.before + " tokens";
  $("#context-after").textContent = context.after + " tokens";
  $("#context-percent").textContent = reduction + "%";
  $("#context-reduction").textContent = context.strategy === "not_needed" ? "无需压缩" : "Token ↓ " + reduction + "%";
  $("#context-progress-bar").style.width = reduction + "%";
  $("#context-note").textContent = context.strategy === "not_needed" ? "结果未超过 180 Token 预算，保留原文以避免无意义压缩。" : "完整结果已归档，本次向后续 Agent 仅传递压缩上下文。";
}

function updateGuard(result) {
  const events = result.events || [];
  const inProgress = result.status === "running";
  const findStage = (stage) => events.filter((event) => event.stage === stage).at(-1);
  const recall = findStage("recall"); const writer = findStage("writer"); const reviewer = findStage("reviewer"); const risk = findStage("risk"); const runner = findStage("runner"); const rag = findStage("rag");
  const tables = recall?.details?.tables;
  const metadata = tables?.length ? guardItem("元数据范围约束", "仅允许访问：" + tables.join("、"), "pass", "已收敛") : guardItem("元数据范围约束", rag ? "无可靠结构化表命中，已转知识库" : (inProgress ? "正在召回授权表" : "未产生可执行表范围"), rag ? "hold" : (inProgress ? "waiting" : "block"), rag ? "已兜底" : (inProgress ? "分析中" : "未通过"));
  const reviewOk = reviewer?.message?.includes("通过");
  const reviewDetail = reviewOk ? "单条只读 SQL 已通过表范围与语法审查" : (reviewer?.details?.issues?.join("；") || (writer ? "候选 SQL 未获得审查放行" : "未生成候选 SQL"));
  const reviewState = reviewOk ? "pass" : (inProgress ? "waiting" : (rag ? "hold" : "block"));
  const reviewLabel = reviewOk ? "已放行" : (inProgress ? "分析中" : (rag ? "未执行" : "已拦截"));
  const riskMode = risk?.details?.mode;
  const riskReason = risk?.details?.reason || (result.status === "approval_required" ? "写操作必须先经人工审批" : result.status === "blocked" ? "未满足安全执行规则" : "只读查询自动执行");
  const riskState = riskMode === "auto" || result.status === "completed" ? "pass" : inProgress ? "waiting" : result.status === "approval_required" ? "hold" : result.status === "answered_by_rag" ? "hold" : "block";
  const riskLabel = riskMode === "auto" || result.status === "completed" ? "自动执行" : inProgress ? "分析中" : result.status === "approval_required" ? "待审批" : result.status === "answered_by_rag" ? "已兜底" : "已阻断";
  const auditDetail = result.status === "completed" ? "问题、SQL、行数与每个阶段已写入审计链" : result.status === "approval_required" ? "审批申请及风险原因已写入审计链" : result.status === "answered_by_rag" ? "RAG 兜底路径与知识来源已写入审计链" : "拦截原因已写入审计链";
  $("#guard-list").innerHTML = metadata + guardItem("SQL 审查", reviewDetail, reviewState, reviewLabel) + guardItem("风险分级", riskReason, riskState, riskLabel) + guardItem("全链路审计", inProgress ? "将在本次工作流结束后写入防篡改审计链" : auditDetail, inProgress ? "waiting" : "pass", inProgress ? "等待结束" : "已留痕");
  const stateMap = { completed:["pass", "✓", "本次查询已安全执行", (runner?.details?.row_count ?? result.rows?.length ?? 0) + " 条记录已在只读范围内返回"], answered_by_rag:["hold", "⌁", "未执行 SQL，已由知识库回答", "结构化查询未被放行，因此没有访问业务表"], approval_required:["hold", "!", "已暂停，等待人工审批", "写操作不会自动执行；请先核对影响预估"], blocked:["block", "×", "请求已被安全策略阻断", riskReason] };
  const state = stateMap[result.status] || ["waiting", "…", "正在判定执行护栏", "正在收集元数据与审查证据"];
  $("#guard-status").textContent = state[2]; $("#guard-status").className = "guard-status " + state[0];
  $("#guard-verdict").className = "guard-verdict " + state[0]; $("#guard-verdict").innerHTML = '<span class="guard-verdict-icon">' + state[1] + '</span><div><strong>' + escapeHtml(state[2]) + '</strong><small>' + escapeHtml(state[3]) + '</small></div>';
  $("#guard-open-approval").hidden = result.status !== "approval_required";
  updateContextStat(result);
}

function resetGuard() {
  selectedTraceIndex = -1;
  $("#trace-count").textContent = "规划中";
  $("#trace-inspector").innerHTML = '<span class="trace-inspector-kicker">Agent 正在规划</span><strong>安全证据会随工作流逐步出现。</strong><p>依次观察召回表范围、候选 SQL、审查结论、风险等级和执行结果。</p>';
  $("#guard-status").textContent = "分析中"; $("#guard-status").className = "guard-status waiting";
  $("#guard-verdict").className = "guard-verdict waiting"; $("#guard-verdict").innerHTML = '<span class="guard-verdict-icon">…</span><div><strong>正在收集安全证据</strong><small>召回表范围、SQL 审查、风险分级和审计会依次更新。</small></div>';
  $("#guard-open-approval").hidden = true;
  updateContextStat({ status:"running", events:[] });
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
  updateGuard(result);
}

async function runQuery() {
  const question = $("#question").value.trim(); if (!question) return toast("请先输入一个运维问题");
  const button = $("#run-query"); button.disabled = true; button.textContent = "分析中…";
  resetGuard();
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
      if (event === "stage") { events.push(payload); setTrace(events, "running"); updateGuard({ status:"running", events }); }
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
    const [response, summaryResponse] = await Promise.all([fetch("/api/v1/audit?limit=" + recordPageSize + "&offset=" + auditOffset), fetch("/api/v1/audit/summary")]);
    const rows = await response.json(); const summary = await summaryResponse.json();
    if (!response.ok || !summaryResponse.ok) throw new Error("审计记录加载失败");
    target.innerHTML = rows.length ? rows.map((row) => `<div class="audit-row"><span class="audit-action">${escapeHtml(row.action)}</span><span class="audit-payload">${escapeHtml(row.payload.question || row.payload.sql || row.payload.sources?.join("、") || "系统事件")}</span><span class="audit-time">${new Date(row.created_at).toLocaleString("zh-CN", {hour12:false})}</span></div>`).join("") : "<div class='empty-state'><strong>暂无审计记录</strong></div>";
    $("#audit-page-note").textContent = "第 " + (Math.floor(auditOffset / recordPageSize) + 1) + " 页 · 本页 " + rows.length + " 条 / 共 " + summary.total + " 条";
    $("#prev-audit").disabled = auditOffset === 0; $("#next-audit").disabled = auditOffset + rows.length >= summary.total;
  } catch { target.innerHTML = "<div class='empty-state'><strong>无法加载审计记录</strong></div>"; }
}

async function loadSkills() {
  const target = $("#skills-list");
  try {
    const skills = await (await fetch("/api/v1/skills")).json();
    target.innerHTML = skills.map((skill, index) => {
      const suggestions = (skill.suggestions || []).map((item) => '<button class="skill-suggestion" data-run-skill="' + escapeHtml(skill.name) + '" data-skill-input="' + escapeHtml(item) + '">' + escapeHtml(item) + '</button>').join("");
      const run = skill.runnable ? '<button class="primary-button skill-run" data-run-skill="' + escapeHtml(skill.name) + '">运行 Skill ↗</button>' : '<span class="skill-tag">规范型能力</span>';
      return `<article class="card skill-card"><span class="skill-symbol">${index ? "⌘" : "◈"}</span><span class="skill-risk ${escapeHtml(skill.risk || "auto")}">${escapeHtml((skill.category || "通用") + " · " + (skill.risk || "auto").toUpperCase())}</span><h3>${escapeHtml(skill.name)}</h3><p>${escapeHtml(skill.description || "可复用的运维领域能力")}</p><div class="skill-suggestions">${suggestions}</div><div class="skill-actions"><button class="text-button" data-skill="${escapeHtml(skill.name)}">查看完整 SOP</button>${run}</div></article>`;
    }).join("");
    document.querySelectorAll("[data-skill]").forEach((button) => button.addEventListener("click", () => viewSkill(button.dataset.skill)));
    document.querySelectorAll("[data-run-skill]").forEach((button) => button.addEventListener("click", () => runSkill(button.dataset.runSkill, button.dataset.skillInput || "")));
  } catch { target.innerHTML = "<p>加载 Skills 失败。</p>"; }
}

async function viewSkill(name) {
  try {
    const skill = await (await fetch(`/api/v1/skills/${encodeURIComponent(name)}`)).json();
    $("#skill-content").textContent = skill.content; $("#skill-detail").hidden = false;
    $("#skill-detail").scrollIntoView({ behavior:"smooth", block:"start" });
  } catch { toast("无法读取 Skill 内容"); }
}

async function runSkill(name, suggestedInput) {
  const input = prompt("输入本次 Skill 的业务上下文", suggestedInput || "") ?? "";
  try {
    const response = await fetch("/api/v1/skills/" + encodeURIComponent(name) + "/run", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ role:currentRole(), requester:"Lenovo", user_input:input }) });
    const result = await response.json(); if (!response.ok) throw new Error(result.detail || "Skill 运行失败");
    const steps = (result.next_steps || []).map((item) => "- " + item).join("\n");
    $("#skill-content").textContent = "# " + name + " 运行结果\n\n状态：" + result.status + "\n风险：" + result.risk + "\n\n## 结论\n" + result.summary + "\n\n## 下一步\n" + (steps || "无") + "\n\n## 结构化数据\n" + JSON.stringify(result.data, null, 2);
    $("#skill-detail").hidden = false; $("#skill-detail").scrollIntoView({ behavior:"smooth", block:"start" });
    toast(name + " 运行完成"); loadAudit();
  } catch (error) { toast(error.message || "Skill 运行失败"); }
}

function renderKnowledge(documents) {
  const target = $("#knowledge-list");
  target.innerHTML = documents.length ? documents.map((document) => `<article class="knowledge-item"><button class="text-button knowledge-delete" data-delete-knowledge="${document.id}">删除</button><strong>${escapeHtml(document.title)}</strong><p>${escapeHtml(document.content)}</p><div>${String(document.tags || "未分类").split(/[,，]/).filter(Boolean).map((tag) => `<span>${escapeHtml(tag.trim())}</span>`).join("")}</div></article>`).join("") : "<div class='empty-state'><strong>知识库为空</strong><p>新增一份 SOP 后即可在 RAG 中使用。</p></div>";
  document.querySelectorAll("[data-delete-knowledge]").forEach((button) => button.addEventListener("click", () => deleteKnowledge(button.dataset.deleteKnowledge)));
}

async function loadKnowledge() {
  try { const response = await fetch("/api/v1/knowledge?role=" + encodeURIComponent(currentRole())); const documents = await response.json(); if (!response.ok) throw new Error(documents.detail); renderKnowledge(documents); } catch { $("#knowledge-list").innerHTML = "<div class='empty-state'><strong>无法加载知识库</strong></div>"; }
}

async function saveKnowledge() {
  const title = $("#knowledge-title").value.trim(); const tags = $("#knowledge-tags").value.trim() || "未分类"; const content = $("#knowledge-content").value.trim();
  if (title.length < 2 || content.length < 10) return toast("标题至少 2 个字符，正文至少 10 个字符");
  const button = $("#save-knowledge"); button.disabled = true; button.textContent = "保存中…";
  try {
    const response = await fetch("/api/v1/knowledge", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ title, tags, content, role:currentRole(), requester:"Lenovo" }) });
    const document = await response.json(); if (!response.ok) throw new Error(document.detail || "保存失败");
    $("#knowledge-title").value = ""; $("#knowledge-tags").value = ""; $("#knowledge-content").value = "";
    toast(`“${document.title}” 已纳入 RAG`); loadKnowledge(); loadMetrics();
  } catch (error) { toast(error.message || "保存失败"); }
  finally { button.disabled = false; button.innerHTML = "保存并纳入 RAG <span>↗</span>"; }
}

async function deleteKnowledge(documentId) {
  if (!confirm("确认删除这份知识文档？它将不再参与 RAG 检索。")) return;
  try {
    const response = await fetch("/api/v1/knowledge/" + documentId, { method:"DELETE", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ role:currentRole(), requester:"Lenovo" }) });
    const result = response.ok ? null : await response.json(); if (!response.ok) throw new Error(result.detail || "删除失败");
    toast("知识文档已删除，并已同步移除索引"); loadKnowledge(); loadAudit(); loadMetrics();
  } catch (error) { toast(error.message || "删除失败"); }
}

async function searchKnowledge() {
  const query = $("#knowledge-query").value.trim();
  if (!query) return toast("请输入要检索的问题");
  try {
    const response = await fetch("/api/v1/knowledge/search?query=" + encodeURIComponent(query) + "&role=" + encodeURIComponent(currentRole())); const evidence = await response.json(); if (!response.ok) throw new Error(evidence.detail || "知识检索失败");
    const target = $("#knowledge-search-result"); target.hidden = false;
    target.innerHTML = evidence.length ? evidence.map((item) => '<div class="knowledge-evidence"><strong>' + escapeHtml(item.title) + " · 片段 " + (item.chunk_index + 1) + " · 得分 " + item.score + '</strong><p>' + escapeHtml(item.content) + '</p></div>').join("") : '<div class="knowledge-evidence">没有检索到足够匹配的证据。</div>';
  } catch { toast("知识检索失败"); }
}

async function reindexKnowledge() {
  const button = $("#reindex-knowledge"); button.disabled = true; button.textContent = "构建中…";
  try {
    const response = await fetch("/api/v1/knowledge/reindex", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ role:currentRole(), requester:"Lenovo" }) }); const result = await response.json(); if (!response.ok) throw new Error(result.detail || "索引重建失败");
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
  const titles = { agent:"智能运维查询", chat:"Agent 聊天", monitoring:"监控中心", metrics:"指标中心", data:"数据浏览器", audit:"审计中心", approval:"审批中心", policy:"权限中心", knowledge:"知识库", tools:"工具中心", skills:"Skills 与 SOP" }; $("#page-title").textContent = titles[button.dataset.page];
  if (button.dataset.page === "chat") loadChat(); if (button.dataset.page === "monitoring") loadMonitoring(); if (button.dataset.page === "metrics") loadMetricCatalog(); if (button.dataset.page === "data") loadDataExplorer(); if (button.dataset.page === "audit") loadAudit(); if (button.dataset.page === "approval") loadApprovals(); if (button.dataset.page === "policy") loadPolicies(); if (button.dataset.page === "skills") loadSkills(); if (button.dataset.page === "knowledge") loadKnowledge(); if (button.dataset.page === "tools") loadTools();
}));
$("#run-query").addEventListener("click", runQuery);
$("#guard-open-audit").addEventListener("click", () => document.querySelector('.nav-item[data-page="audit"]').click());
$("#guard-open-approval").addEventListener("click", () => document.querySelector('.nav-item[data-page="approval"]').click());
$("#new-chat").addEventListener("click", createChat);
$("#send-chat").addEventListener("click", sendChat);
$("#chat-input").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); sendChat(); } });
document.querySelectorAll("[data-query]").forEach((button) => button.addEventListener("click", () => { document.querySelector('.nav-item[data-page="agent"]').click(); $("#question").value = button.dataset.query; runQuery(); }));
$("#copy-sql").addEventListener("click", async () => { await navigator.clipboard.writeText(latestSql); toast("SQL 已复制到剪贴板"); });
$("#refresh-audit").addEventListener("click", () => { auditOffset = 0; loadAudit(); });
$("#prev-audit").addEventListener("click", () => { auditOffset = Math.max(0, auditOffset - recordPageSize); loadAudit(); });
$("#next-audit").addEventListener("click", () => { auditOffset += recordPageSize; loadAudit(); });
$("#refresh-data").addEventListener("click", () => loadDataExplorer());
$("#data-table-select").addEventListener("change", () => { explorerOffset = 0; loadDataTable(); });
$("#prev-data").addEventListener("click", () => { explorerOffset = Math.max(0, explorerOffset - 30); loadDataTable(); });
$("#next-data").addEventListener("click", () => { explorerOffset += 30; loadDataTable(); });
$("#run-evaluation").addEventListener("click", runEvaluation);
$("#verify-audit").addEventListener("click", verifyAuditIntegrity);
$("#refresh-knowledge").addEventListener("click", loadKnowledge);
$("#search-knowledge").addEventListener("click", searchKnowledge);
$("#reindex-knowledge").addEventListener("click", reindexKnowledge);
$("#refresh-approvals").addEventListener("click", () => { approvalOffset = 0; loadApprovals(); });
$("#prev-approvals").addEventListener("click", () => { approvalOffset = Math.max(0, approvalOffset - recordPageSize); loadApprovals(); });
$("#next-approvals").addEventListener("click", () => { approvalOffset += recordPageSize; loadApprovals(); });
$("#refresh-monitoring").addEventListener("click", loadMonitoring);
$("#search-metrics").addEventListener("click", loadMetricCatalog);
$("#refresh-metrics").addEventListener("click", () => { $("#metric-keyword").value = ""; $("#metric-category").value = ""; loadMetricCatalog(); });
$("#download-metric-template").addEventListener("click", () => { window.location.href = "/api/v1/metrics/import-template"; });
$("#upload-metric-csv").addEventListener("click", importMetricCsv);
$("#analyze-metric").addEventListener("click", () => { if (!selectedMetric) return; document.querySelector('.nav-item[data-page="agent"]').click(); $("#question").value = "查询指标 #" + selectedMetric.id + " 近24小时趋势"; runQuery(); });
$("#role-selector").addEventListener("change", () => { const label = $("#role-selector").selectedOptions[0].textContent; $("#role-label").textContent = label; toast("当前角色已切换为：" + label); loadPolicies(); updateMetricImportAccess(); if ($("#data-page").classList.contains("active-page")) loadDataExplorer(); if ($("#metrics-page").classList.contains("active-page")) loadMetricCatalog(); });
$("#save-knowledge").addEventListener("click", saveKnowledge);
$("#close-skill-detail").addEventListener("click", () => { $("#skill-detail").hidden = true; });
$("#approve-button").addEventListener("click", () => { if (latestApprovalId) resolveApproval(latestApprovalId, $("#approve-button")); });
$("#show-system-info").addEventListener("click", async () => { try { const metric = await (await fetch("/api/v1/metrics")).json(); toast("LLM：" + (metric.llm_enabled ? "已配置" : "离线规则模式") + "；审批写库：" + (metric.approved_writes_enabled ? "已开启" : "安全关闭")); } catch { toast("无法读取运行配置"); } });
$("#show-help").addEventListener("click", () => toast("可查询数据、查看审批与审计、维护知识库，并查看 Skill/SOP。"));
loadSkills(); loadKnowledge(); loadApprovals(); loadMetrics();

function chatRequestOptions(method, body) {
  return { method, headers:{"Content-Type":"application/json"}, body:JSON.stringify(body) };
}

function renderChatMessages(messages) {
  const target = $("#chat-messages");
  if (!messages.length) { target.innerHTML = '<div class="empty-state"><strong>开始一次 Agent 对话</strong><p>例如：如何处理华东 P1 网关离线？</p></div>'; return; }
  target.innerHTML = messages.map((message) => '<article class="chat-bubble ' + escapeHtml(message.role) + '"><span>' + (message.role === "user" ? "你" : "OpsPilot") + '</span><div>' + escapeHtml(message.content).replace(/\n/g, "<br>") + '</div></article>').join("");
  target.scrollTop = target.scrollHeight;
}

function renderChatConversations() {
  const target = $("#chat-conversation-list"); $("#chat-count").textContent = chatConversations.length + " 个";
  target.innerHTML = chatConversations.length ? chatConversations.map((conversation) => '<div class="chat-conversation-row"><button class="chat-conversation ' + (conversation.id === chatConversationId ? "active" : "") + '" data-chat-conversation="' + escapeHtml(conversation.id) + '"><strong>' + escapeHtml(conversation.title) + '</strong><small>' + new Date(conversation.updated_at).toLocaleString("zh-CN", {month:"numeric", day:"numeric", hour:"2-digit", minute:"2-digit", hour12:false}) + '</small></button><button class="chat-delete" title="删除对话" data-delete-chat="' + escapeHtml(conversation.id) + '">×</button></div>').join("") : '<div class="empty-state"><strong>暂无历史对话</strong></div>';
  document.querySelectorAll("[data-chat-conversation]").forEach((button) => button.addEventListener("click", () => openChat(button.dataset.chatConversation)));
  document.querySelectorAll("[data-delete-chat]").forEach((button) => button.addEventListener("click", () => deleteChat(button.dataset.deleteChat)));
}

async function loadChat() {
  try {
    const response = await fetch("/api/v1/chat/conversations?requester=Lenovo&role=" + encodeURIComponent(currentRole())); const conversations = await response.json();
    if (!response.ok) throw new Error(conversations.detail || "会话列表加载失败");
    chatConversations = conversations; renderChatConversations();
    if (!chatConversationId && conversations.length) await openChat(conversations[0].id);
    if (!conversations.length) { $("#chat-title").textContent = "新对话"; renderChatMessages([]); }
  } catch (error) { $("#chat-messages").innerHTML = '<div class="empty-state"><strong>无法加载聊天</strong><p>' + escapeHtml(error.message || "服务请求失败") + '</p></div>'; }
}

async function createChat() {
  try {
    const response = await fetch("/api/v1/chat/conversations", chatRequestOptions("POST", { role:currentRole(), requester:"Lenovo", title:"新对话" })); const conversation = await response.json();
    if (!response.ok) throw new Error(conversation.detail || "创建对话失败");
    chatConversations = [conversation, ...chatConversations]; chatConversationId = conversation.id; renderChatConversations(); renderChatMessages([]); $("#chat-title").textContent = conversation.title; $("#chat-input").focus();
  } catch (error) { toast(error.message || "创建对话失败"); }
}

async function openChat(conversationId) {
  try {
    const response = await fetch("/api/v1/chat/conversations/" + encodeURIComponent(conversationId) + "/messages?requester=Lenovo&role=" + encodeURIComponent(currentRole())); const messages = await response.json();
    if (!response.ok) throw new Error(messages.detail || "对话加载失败");
    chatConversationId = conversationId; const conversation = chatConversations.find((item) => item.id === conversationId); $("#chat-title").textContent = conversation?.title || "Agent 对话"; renderChatConversations(); renderChatMessages(messages);
  } catch (error) { toast(error.message || "对话加载失败"); }
}

async function deleteChat(conversationId) {
  if (!confirm("确认删除该 Agent 对话及其历史消息？")) return;
  try {
    const response = await fetch("/api/v1/chat/conversations/" + encodeURIComponent(conversationId), chatRequestOptions("DELETE", { role:currentRole(), requester:"Lenovo" })); const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "删除对话失败");
    chatConversations = chatConversations.filter((item) => item.id !== conversationId); if (chatConversationId === conversationId) chatConversationId = ""; renderChatConversations();
    if (chatConversations.length) await openChat(chatConversations[0].id); else { $("#chat-title").textContent = "新对话"; renderChatMessages([]); }
  } catch (error) { toast(error.message || "删除对话失败"); }
}

async function sendChat() {
  const content = $("#chat-input").value.trim(); if (!content) return;
  if (!chatConversationId) { await createChat(); if (!chatConversationId) return; }
  const button = $("#send-chat"); button.disabled = true; button.textContent = "思考中…"; $("#chat-input").disabled = true;
  try {
    const response = await fetch("/api/v1/chat/conversations/" + encodeURIComponent(chatConversationId) + "/messages", chatRequestOptions("POST", { role:currentRole(), requester:"Lenovo", content })); const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "聊天请求失败");
    $("#chat-input").value = ""; $("#chat-provider").textContent = result.provider === "local_deepseek" ? "本地 DeepSeek" : "离线提示";
    await loadChat(); await openChat(chatConversationId); loadAudit();
  } catch (error) { toast(error.message || "聊天请求失败"); }
  finally { button.disabled = false; button.textContent = "发送 ↗"; $("#chat-input").disabled = false; $("#chat-input").focus(); }
}

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
  try {
    const [response, summaryResponse] = await Promise.all([fetch("/api/v1/approvals?limit=" + recordPageSize + "&offset=" + approvalOffset), fetch("/api/v1/approvals/summary")]);
    const rows = await response.json(); const summary = await summaryResponse.json();
    if (!response.ok || !summaryResponse.ok) throw new Error("审批队列加载失败");
    renderApprovals(rows);
    $("#approval-page-note").textContent = "第 " + (Math.floor(approvalOffset / recordPageSize) + 1) + " 页 · 本页 " + rows.length + " 条 / 共 " + summary.total + " 条";
    $("#prev-approvals").disabled = approvalOffset === 0; $("#next-approvals").disabled = approvalOffset + rows.length >= summary.total;
  } catch { $("#approval-list").innerHTML = '<div class="empty-state"><strong>无法加载审批队列</strong></div>'; }
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

function renderDataCatalog(tables) {
  $("#data-catalog").innerHTML = tables.map((table) => '<button class="data-catalog-item" data-data-table="' + escapeHtml(table.name) + '"><strong>' + escapeHtml(table.label) + '</strong><small>' + escapeHtml(table.name) + ' · ' + table.row_count + ' 行 · ' + table.column_count + ' 列</small></button>').join("");
  document.querySelectorAll("[data-data-table]").forEach((button) => button.addEventListener("click", () => { $("#data-table-select").value = button.dataset.dataTable; explorerOffset = 0; loadDataTable(); }));
}

async function loadDataExplorer() {
  try {
    const response = await fetch("/api/v1/data/tables?role=" + encodeURIComponent(currentRole())); const tables = await response.json();
    if (!response.ok) throw new Error(tables.detail || "数据目录加载失败");
    const current = $("#data-table-select").value; explorerCatalog = tables; renderDataCatalog(tables);
    $("#data-table-select").innerHTML = tables.map((table) => '<option value="' + escapeHtml(table.name) + '">' + escapeHtml(table.label) + '（' + escapeHtml(table.name) + '）</option>').join("");
    $("#data-table-select").value = tables.some((table) => table.name === current) ? current : (tables[0]?.name || "");
    explorerOffset = 0; await loadDataTable();
  } catch (error) { $("#data-table-wrap").innerHTML = '<div class="empty-state"><strong>无法加载数据浏览器</strong><p>' + escapeHtml(error.message || "请确认当前角色具有只读权限") + '</p></div>'; }
}

async function loadDataTable() {
  const tableName = $("#data-table-select").value; if (!tableName) return;
  try {
    const response = await fetch("/api/v1/data/tables/" + encodeURIComponent(tableName) + "?role=" + encodeURIComponent(currentRole()) + "&limit=30&offset=" + explorerOffset); const snapshot = await response.json();
    if (!response.ok) throw new Error(snapshot.detail || "表数据加载失败");
    $("#data-table-title").textContent = snapshot.label + " · " + snapshot.name;
    $("#data-summary").textContent = "共 " + snapshot.total + " 行，仅只读预览";
    $("#data-schema").innerHTML = snapshot.schema.map((column) => '<span><b>' + escapeHtml(column.name) + '</b> ' + escapeHtml(column.type || "TEXT") + (column.primary_key ? " · PK" : "") + (column.required ? " · NOT NULL" : "") + '</span>').join("");
    $("#data-table-wrap").innerHTML = renderRows(snapshot.rows);
    $("#data-page-note").textContent = "第 " + (Math.floor(snapshot.offset / snapshot.limit) + 1) + " 页 · " + snapshot.rows.length + " / " + snapshot.total + " 行";
    $("#prev-data").disabled = snapshot.offset === 0; $("#next-data").disabled = snapshot.offset + snapshot.limit >= snapshot.total;
  } catch (error) { $("#data-table-wrap").innerHTML = '<div class="empty-state"><strong>无法加载表数据</strong><p>' + escapeHtml(error.message || "请求失败") + '</p></div>'; }
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
    const response = await fetch("/api/v1/tools/" + encodeURIComponent(name) + "/invoke", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ role:currentRole(), requester:"Lenovo" }) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "工具调用失败");
    const box = $("#tool-result"); box.hidden = false;
    box.textContent = JSON.stringify(result, null, 2);
    if (result.status === "approval_required" && result.approval_id) {
      toast("已创建审批单，可前往审批中心处理"); loadApprovals();
    } else {
      toast("工具调用状态：" + result.status);
    }
    loadAudit();
  } catch (error) {
    const box = $("#tool-result"); box.hidden = false;
    box.textContent = "工具调用失败：" + (error.message || "未知错误");
    toast(error.message || "工具调用失败");
  }
  finally { button.disabled = false; button.textContent = "试运行工具"; }
}

function renderMetricCatalog(metrics) {
  const target = $("#metric-list");
  target.innerHTML = metrics.length ? metrics.map((metric) => '<article class="metric-item"><div><strong>' + escapeHtml(metric.name) + '</strong><small>' + escapeHtml(metric.category) + ' · ' + escapeHtml(metric.asset_scope) + ' · ' + escapeHtml(metric.unit) + '</small><span class="metric-source ' + escapeHtml(metric.source || "demo") + '">' + (metric.source === "csv" ? "真实 CSV" : "本地演示") + '</span></div><button class="text-button" data-metric="' + metric.id + '">查看趋势</button></article>').join("") : '<div class="empty-state"><strong>没有匹配指标</strong><p>尝试切换分类或关键词。</p></div>';
  document.querySelectorAll("[data-metric]").forEach((button) => button.addEventListener("click", () => loadMetricTrend(button.dataset.metric)));
}

function updateMetricImportAccess() {
  const permitted = currentRole() !== "viewer";
  const button = $("#upload-metric-csv"); button.disabled = !permitted;
  button.title = permitted ? "" : "观察者角色只能查看指标，不能导入数据";
}

function renderMetricImportHistory(rows) {
  const target = $("#metric-import-history");
  target.innerHTML = rows.length ? '<strong>最近导入</strong>' + rows.slice(0, 3).map((row) => '<div><span>' + escapeHtml(row.filename) + '</span><small>' + row.sample_count + ' 条样本 · 新增 ' + row.created_metrics + ' 个指标 · ' + new Date(row.created_at).toLocaleString("zh-CN", {month:"numeric", day:"numeric", hour:"2-digit", minute:"2-digit", hour12:false}) + '</small></div>').join("") : '<small>尚未导入真实 CSV。</small>';
}

async function loadMetricImports() {
  try {
    const response = await fetch("/api/v1/metrics/imports?role=" + encodeURIComponent(currentRole())); const rows = await response.json();
    if (!response.ok) throw new Error(rows.detail || "导入记录加载失败");
    renderMetricImportHistory(rows);
  } catch { $("#metric-import-history").innerHTML = '<small>无法加载导入记录。</small>'; }
}

async function importMetricCsv() {
  const file = $("#metric-csv-file").files?.[0];
  if (!file) return toast("请先选择一个 CSV 文件");
  if (!file.name.toLowerCase().endsWith(".csv")) return toast("仅支持 .csv 文件");
  if (file.size > 5 * 1024 * 1024) return toast("CSV 文件不能超过 5 MB");
  const button = $("#upload-metric-csv"); button.disabled = true; button.textContent = "校验并导入中…";
  try {
    const url = "/api/v1/metrics/import?filename=" + encodeURIComponent(file.name) + "&role=" + encodeURIComponent(currentRole()) + "&requester=Lenovo";
    const response = await fetch(url, { method:"POST", headers:{"Content-Type":"text/csv; charset=utf-8"}, body:file }); const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "CSV 导入失败");
    const target = $("#metric-import-result"); target.hidden = false;
    target.textContent = "导入完成：校验 " + result.total_rows + " 行，写入 " + result.sample_count + " 个样本，新增 " + result.created_metrics + " 个指标，匹配已有 " + result.updated_metrics + " 个指标。";
    $("#metric-csv-file").value = ""; toast("真实 CSV 指标已导入");
    await Promise.all([loadMetricCatalog(), loadMetricImports(), loadMetrics()]);
  } catch (error) { toast(error.message || "CSV 导入失败"); }
  finally { button.disabled = currentRole() === "viewer"; button.textContent = "校验并导入 CSV"; }
}

async function loadMetricCatalog() {
  const keyword = $("#metric-keyword").value.trim(); const category = $("#metric-category").value;
  try {
    const response = await fetch("/api/v1/metric-definitions?limit=60&keyword=" + encodeURIComponent(keyword) + "&category=" + encodeURIComponent(category));
    const metrics = await response.json(); renderMetricCatalog(metrics);
    const system = await (await fetch("/api/v1/metrics")).json();
    $("#metric-count-note").textContent = system.metric_definitions || metrics.length;
    $("#metric-count-description").textContent = "条指标定义，其中 " + (system.imported_metric_definitions || 0) + " 条来自真实 CSV";
    updateMetricImportAccess(); loadMetricImports();
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
