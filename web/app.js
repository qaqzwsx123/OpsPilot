const $ = (selector) => document.querySelector(selector);
let latestSql = "";
let latestApprovalId = "";

const stageNames = { recall: "Recall", writer: "Writer", reviewer: "Reviewer", risk: "Risk Guard", runner: "Runner", rag: "Agentic RAG" };

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
    const state = status === "blocked" || status === "approval_required" ? (index === events.length - 1 ? "blocked" : "complete") : "complete";
    const details = event.details && Object.keys(event.details).length ? Object.values(event.details)[0] : "";
    const short = Array.isArray(details) ? details.join("、") : String(details || event.message);
    return `<li class="${state}"><span class="trace-node">${state === "complete" ? "✓" : "!"}</span><div><strong>${stageNames[event.stage] || event.stage}</strong><small>${escapeHtml(short)}</small></div></li>`;
  }).join("");
  const badge = $("#trace-status"); badge.textContent = status === "completed" ? "执行完成" : status === "answered_by_rag" ? "RAG 已回答" : status === "approval_required" ? "等待审批" : "已阻断";
  badge.className = `trace-status ${status === "completed" || status === "answered_by_rag" ? "done" : "blocked"}`;
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
  $("#approval-description").textContent = result.status === "approval_required" ? `审批单 ${latestApprovalId.slice(0, 8)}… 已创建。Demo 不会直接执行任何写库操作。` : "";
}

async function runQuery() {
  const question = $("#question").value.trim(); if (!question) return toast("请先输入一个运维问题");
  const button = $("#run-query"); button.disabled = true; button.textContent = "分析中…";
  const badge = $("#trace-status"); badge.textContent = "Agent 执行中"; badge.className = "trace-status running";
  $("#trace-list").innerHTML = `<li class="active"><span class="trace-node">…</span><div><strong>Agent 正在规划</strong><small>召回元数据、生成 SQL 并进行安全审查</small></div></li>`;
  try {
    const response = await fetch("/api/v1/query", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ question, requester:"Lenovo" }) });
    const result = await response.json(); if (!response.ok) throw new Error(result.detail || "请求失败");
    renderResult(result); setTrace(result.events || [], result.status); loadAudit();
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
  try { const skills = await (await fetch("/api/v1/skills")).json(); target.innerHTML = skills.map((skill, index) => `<article class="card skill-card"><span class="skill-symbol">${index ? "⌘" : "◈"}</span><h3>${escapeHtml(skill.name)}</h3><p>${escapeHtml(skill.description || "可复用的运维领域能力")}</p><span class="skill-tag">SKILL.md 已加载</span></article>`).join(""); } catch { target.innerHTML = "<p>加载 Skills 失败。</p>"; }
}

document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll(".nav-item").forEach((node) => node.classList.remove("active")); button.classList.add("active");
  document.querySelectorAll(".page").forEach((page) => page.classList.remove("active-page")); $(`#${button.dataset.page}-page`).classList.add("active-page");
  const titles = { agent:"智能运维查询", audit:"审计中心", skills:"Skills 与 SOP" }; $("#page-title").textContent = titles[button.dataset.page];
  if (button.dataset.page === "audit") loadAudit(); if (button.dataset.page === "skills") loadSkills();
}));
$("#run-query").addEventListener("click", runQuery);
document.querySelectorAll("[data-query]").forEach((button) => button.addEventListener("click", () => { $("#question").value = button.dataset.query; runQuery(); }));
$("#copy-sql").addEventListener("click", async () => { await navigator.clipboard.writeText(latestSql); toast("SQL 已复制到剪贴板"); });
$("#refresh-audit").addEventListener("click", loadAudit);
$("#approve-button").addEventListener("click", async () => { if (!latestApprovalId) return; const response = await fetch(`/api/v1/approvals/${latestApprovalId}/approve`, { method:"POST" }); if (response.ok) { toast("审批已记录：Demo 不会执行写操作"); $("#approve-button").disabled = true; $("#approve-button").textContent = "已审批"; loadAudit(); } else toast("审批失败"); });
loadSkills();
