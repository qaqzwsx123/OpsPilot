const $ = (selector) => document.querySelector(selector);
let latestSql = "";
let latestApprovalId = "";
let selectedMetric = null;
let explorerCatalog = [];
let explorerOffset = 0;
let auditOffset = 0;
let approvalOffset = 0;
let approvalStatus = "";
let knowledgeOffset = 0;
let editingKnowledgeId = null;
let knowledgeRecordCache = new Map();
let chatConversationId = "";
let chatConversations = [];
let allSkills = [];
let allTools = [];
const recordPageSize = 10;
const knowledgePageSize = 5;
const currentRole = () => $("#role-selector").value;

const stageNames = { context: "Context Memory", tool_plan: "Tool Planner", tool: "Tool Runner", tool_summary: "Agent Summary", recall: "Recall", writer: "Writer", reviewer: "Reviewer", fix: "Fix", risk: "Risk Guard", runner: "Runner", rag: "Agentic RAG" };
let selectedTraceIndex = -1;
const pageGuides = {
  agent: { eyebrow:"SAFE SQL WORKFLOW", title:"智能查询使用说明", lead:"这里把自然语言运维问题转换为受控查询。系统优先查询授权的结构化数据，无法生成可靠 SQL 时才使用知识库回答。", sections:[
    { title:"如何使用", text:"输入问题后点击“运行查询”。可直接使用示例问题，例如查询 P1 告警、离线设备或未关闭工单。" },
    { title:"执行过程", text:"工作流会先由 Tool Planner 从 AUTO 白名单中选择相关只读工具，再召回授权表、生成 SQL、审查 SQL、评估风险并执行只读查询。点击工作流节点可查看选择理由、工具返回摘要与后续证据。" },
    { title:"安全边界", text:"只读 SELECT 可自动执行；写操作只创建审批单；DDL、多语句和高危维护请求会被阻断。结果、SQL 和决策均会审计留痕。", tone:"blocked" }
  ] },
  chat: { eyebrow:"LOCAL DEEPSEEK CHAT", title:"Agent 聊天使用说明", lead:"这是本地 DeepSeek 的多轮讨论入口，适合咨询排障思路、解释系统概念和制定操作建议。", sections:[
    { title:"会话管理", text:"点击“新建对话”开始；历史对话会保存在本地 SQLite，可重新打开或删除自己的会话。" },
    { title:"输入方式", text:"Enter 发送，Shift + Enter 换行。模型回复会标识为本地 DeepSeek 或离线提示。" },
    { title:"重要边界", text:"聊天不会自动执行 SQL、调用工具、创建工单或修改数据。需要业务操作时，请到智能查询、工具中心或审批中心明确发起。", tone:"blocked" }
  ] },
  monitoring: { eyebrow:"LIVE OPERATIONS", title:"监控中心使用说明", lead:"监控中心把当前资产状态、未关闭告警和待处理工单汇总在同一页，便于快速发现异常范围。", sections:[
    { title:"查看内容", text:"资产运行状态展示在线、离线和维护中的设备；告警分布按严重程度汇总；最近告警提供明细。" },
    { title:"继续分析", text:"点击“用 Agent 分析 P1 告警”会将问题带入智能查询，由安全 SQL 工作流继续检索关联数据。" },
    { title:"数据范围", text:"当前页面读取项目本地已接入的资产、告警和工单表；刷新仅重新读取数据，不会改变任何业务状态。" }
  ] },
  metrics: { eyebrow:"OBSERVABILITY METRICS", title:"指标中心使用说明", lead:"指标中心用于筛选指标、查看时序趋势，并将具体指标带入 Agent 做进一步分析。", sections:[
    { title:"数据来源", text:"灰色“本地演示”是项目初始化的样例数据；绿色“真实 CSV”来自用户导入的本地 CSV，二者会明确区分。" },
    { title:"导入 CSV", text:"运维工程师及以上角色可下载模板并导入 UTF-8 CSV。每行必须包含指标名、分类、单位、资源范围、采集时间和数值；最大 5MB、5 万行。", tone:"manual" },
    { title:"趋势与 Agent", text:"点击“查看趋势”查看最多 24 小时样本；点击“交给 Agent 分析”会跳转智能查询，仍遵守 SQL 白名单与只读约束。" }
  ] },
  data: { eyebrow:"READ-ONLY DATA EXPLORER", title:"数据浏览器使用说明", lead:"数据浏览器类似轻量数据库客户端，用于查看本地 SQLite 的授权表、字段结构和分页样例。", sections:[
    { title:"如何查看", text:"在表目录或下拉框选择表，即可查看字段、主键标记、总行数和每页最多 30 行的只读数据。" },
    { title:"可查看范围", text:"只能查看后端白名单中的业务表、审计表和指标表，不能访问 sqlite_master 或任意文件。" },
    { title:"安全边界", text:"此页面没有新增、修改、删除或任意 SQL 输入入口。它只用于核对数据，不是数据库管理工具。", tone:"blocked" }
  ] },
  audit: { eyebrow:"AUDITABLE BY DEFAULT", title:"审计中心使用说明", lead:"审计中心记录 Agent 查询、工具调用、审批决定、知识维护、CSV 导入等关键事件，用于追踪与复盘。", sections:[
    { title:"查看记录", text:"记录按时间倒序分页展示，每页 10 条，并显示总数。刷新只读取最新审计数据。" },
    { title:"验证完整性", text:"“验证完整性”会弹出范围选择：可校验全量 SHA-256 哈希链，或快速校验最近 100 条及前序锚点；发现断裂时应停止依赖该链进行合规判断。" },
    { title:"离线评测", text:"“运行评测”可选择 5 条内置基线、全部用例或勾选的用例。运维工程师可新增自定义“问题 + 预期状态”用例；评测不会修改业务表。" }
  ] },
  approval: { eyebrow:"HUMAN IN THE LOOP", title:"审批中心使用说明", lead:"审批中心承接 SQL 写操作和 MANUAL 工具请求。它让高风险变更必须经过人工确认，而非由 Agent 自动执行。", sections:[
    { title:"审批前检查", text:"待审批记录会显示请求 SQL、影响预估、抽样结果和有效期，便于判断是否应放行。" },
    { title:"状态含义", text:"pending 表示待处理；approved_safe_mode 表示已经同意但安全模式未写库；executed 才表示已执行；rejected 和 expired 分别表示拒绝或过期。" },
    { title:"默认安全模式", text:"当前默认不会执行写 SQL。批准操作仅记录审批结论和只读影响预估，不会删除表或表中数据。", tone:"blocked" }
  ] },
  policy: { eyebrow:"RBAC + RISK POLICY", title:"权限中心使用说明", lead:"权限中心展示角色、可用权限和风险分级策略。角色切换会立即影响后端接口的实际授权判断。", sections:[
    { title:"角色", text:"观察者可读数据和检索知识；运维工程师可发起变更审批；值班负责人额外拥有审批权限。" },
    { title:"风险分级", text:"AUTO 为只读操作，MANUAL 为必须审批的变更，BLOCKED 为永远不允许 Agent 执行的高危操作。" },
    { title:"不是前端装饰", text:"权限同时在后端校验。即使手动构造请求，未授权角色也会收到拒绝并写入审计。", tone:"blocked" }
  ] },
  knowledge: { eyebrow:"KNOWLEDGE IN, RAG OUT", title:"知识库使用说明", lead:"知识库用于沉淀 SOP、排障手册和规范文本。当 SQL 工作流无法可靠回答时，Agent 会检索这些内容提供带来源的答复。", sections:[
    { title:"维护知识", text:"运维工程师及以上可新增、删除文档或重建索引。正文应写清判断条件、步骤、升级规则和验证方式。", tone:"manual" },
    { title:"验证检索", text:"在右侧输入问题并点击检索，可看到命中文档片段和得分，用于检查知识是否能被正确召回。" },
    { title:"索引说明", text:"保存文档会纳入本地检索索引；删除后不再参与 RAG。知识维护动作都会记录在审计中心。" }
  ] },
  skills: { eyebrow:"REUSABLE EXPERIENCE", title:"Skills 与 SOP 使用说明", lead:"Skill 将重复运维经验封装为可复用的输入、步骤、输出和风险声明，方便 Agent 按标准方式执行或给出建议。", sections:[
    { title:"阅读 SOP", text:"点击“查看完整 SOP”可以查看 Skill 的原始说明、适用场景、输入要求、步骤和边界。" },
    { title:"试运行", text:"可运行的 Skill 会要求输入本次业务上下文，并返回结构化结论和下一步建议；运行记录会写入审计。" },
    { title:"执行范围", text:"当前内置试运行 Skill 为只读诊断与建议能力，不会直接关闭告警、创建工单或修改数据库。", tone:"blocked" }
  ] },
  tools: { eyebrow:"TOOL CENTER GUIDE", title:"工具中心使用说明", lead:"工具中心是 Agent 的白名单能力注册表。每张卡片对应一个已经注册、授权并可审计的后端函数，不提供任意命令执行入口。", sections:[
    { title:"只读工具（AUTO）", text:"点击“试运行工具”会真实调用后端函数、返回结构化结果并写入审计，但不会修改业务数据。", items:["<code>asset_lookup</code>：查询设备资产", "<code>alert_query</code>：查询未关闭告警", "<code>ticket_query</code>：查询运维工单", "<code>work_order_query</code>：查询作业任务", "<code>knowledge_search</code>：检索知识库", "<code>system_health</code>：查看 Agent 服务与数据接入状态"] },
    { title:"变更工具（MANUAL）", text:"<code>create_work_order</code> 和 <code>close_alert</code> 不会直接改变数据。点击后仅创建审批单；需在审批中心核对影响并处理。", tone:"manual" },
    { title:"禁止工具（BLOCKED）", text:"<code>database_maintenance</code> 等数据库维护、DDL 和多语句操作会被直接拒绝，不执行也不进入审批。", tone:"blocked" }
  ], roles:["观察者：仅 AUTO", "运维工程师：可发起 MANUAL 审批", "值班负责人：可审批受控变更"] }
};
const guideExamples = {
  agent: { title:"示例：查询华东 P1 告警关联设备", steps:["在输入框输入 <code>查询华东 P1 告警关联设备</code>。", "点击“运行查询”，观察 Recall、Writer、Reviewer、Risk Guard、Runner 逐步完成。", "点击 Writer 或 Reviewer 的“详情”核对 SQL 与审查结论；结果区会返回关联设备，护栏显示本次为只读自动执行。"] },
  chat: { title:"示例：咨询离线排障方案", steps:["点击“新建对话”。", "输入 <code>华东网关离线时，现场工程师应先排查什么？</code>，按 Enter 发送。", "阅读 DeepSeek 的建议；若要查询真实告警或资产，转到“智能查询”明确发起，而不是要求聊天直接执行。"] },
  monitoring: { title:"示例：从 P1 告警进入根因查询", steps:["点击“刷新”，先查看未关闭告警数量和 P1 分布。", "在“最近告警”卡片点击“用 Agent 分析 P1 告警”。", "系统跳转智能查询并自动带入问题；执行后可查看关联资产、地区和离线状态。"] },
  metrics: { title:"示例：导入并分析真实 CPU 指标", steps:["切换为“运维工程师”，点击“下载模板”，填写并保存 UTF-8 CSV。", "选择 CSV 后点击“校验并导入 CSV”；成功后目录顶部会显示绿色“真实 CSV”标记。", "点击该指标“查看趋势”，再点击“交给 Agent 分析”查看该指标的安全查询路径。"] },
  data: { title:"示例：核对告警表数据", steps:["在表目录选择“监控告警（alerts）”。", "查看字段结构、总行数和本页样例；点击“下一页”继续只读浏览。", "如需按条件筛选，例如只看 P1，请转到智能查询输入 <code>查询最近的 P1 告警</code>。"] },
  audit: { title:"示例：追溯一次查询", steps:["先在智能查询执行 <code>查询最近的 P1 告警</code>。", "进入审计中心点击“刷新记录”，可看到对应的 SQL 执行事件。", "点击“验证完整性”，系统会校验当前审计哈希链；通过时表示已检查的记录未发现链路断裂。"] },
  approval: { title:"示例：安全处理删除请求", steps:["以“运维工程师”在智能查询输入 <code>删除已关闭告警</code>，系统只创建 pending 审批单。", "进入审批中心，先阅读影响预估、抽样数据和有效期。", "切换为“值班负责人”后批准；默认安全模式会变为 approved_safe_mode，仅留痕和预估，不会删除任何告警。"] },
  policy: { title:"示例：验证角色权限确实生效", steps:["切换为“观察者”，在智能查询输入 <code>删除已关闭告警</code>。", "系统会拒绝发起变更审批，并将拒绝动作写入审计。", "切换为“运维工程师”再次发起，则可以创建审批单；这说明限制在后端生效，而非页面隐藏按钮。"] },
  knowledge: { title:"示例：新增 SOP 并验证 RAG", steps:["切换为“运维工程师”，填写标题、标签和至少 10 个字符的 SOP 正文后点击“保存并纳入 RAG”。", "在右侧检索框输入与该 SOP 对应的问题，确认能看到命中文档片段。", "回到智能查询提出规范类问题；当无可靠 SQL 时，系统会使用知识库并展示来源。"] },
  skills: { title:"示例：运行告警分诊 Skill", steps:["找到 <code>incident_triage</code> 卡片，点击“运行 Skill”。", "输入 <code>华东 P1 网关离线，影响支付链路</code> 作为本次上下文。", "查看结构化结论和下一步建议；该过程只生成诊断建议并写入审计，不会关闭告警。"] },
  tools: { title:"示例：比较只读工具与变更工具", steps:["点击 <code>alert_query</code> 的“试运行工具”，会立刻返回未关闭告警的结构化结果并写审计。", "点击 <code>close_alert</code> 的“试运行工具”，不会关闭告警，而是创建审批单。", "点击 <code>database_maintenance</code> 会被直接拒绝；系统没有任意数据库命令执行入口。"] }
};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", "'":"&#39;", '"':"&quot;" })[character]);
}

function toast(message) {
  const node = $("#toast"); node.textContent = message; node.classList.add("show");
  setTimeout(() => node.classList.remove("show"), 2600);
}

function setToolHelpVisible(visible) {
  const modal = $("#tool-help-modal");
  modal.hidden = !visible;
  document.body.classList.toggle("modal-open", visible);
  if (visible) $("#close-tool-help").focus();
}

function openPageGuide(key) {
  const guide = pageGuides[key];
  if (!guide) return;
  const sections = guide.sections.map((section) => '<div class="tool-help-section ' + (section.tone || "") + '"><h3>' + section.title + '</h3><p>' + section.text + '</p>' + (section.items ? '<ul>' + section.items.map((item) => '<li>' + item + '</li>').join("") + '</ul>' : "") + '</div>').join("");
  const roles = guide.roles?.length ? '<div class="tool-help-roles"><strong>角色限制</strong>' + guide.roles.map((role) => '<span>' + role + '</span>').join("") + '</div>' : "";
  const example = guideExamples[key];
  const exampleHtml = example ? '<div class="guide-example"><span>操作示例</span><h3>' + example.title + '</h3><ol>' + example.steps.map((step) => '<li>' + step + '</li>').join("") + '</ol></div>' : "";
  $("#guide-modal-content").innerHTML = '<div class="modal-heading"><div><p class="section-label">' + guide.eyebrow + '</p><h2 id="tool-help-title">' + guide.title + '</h2></div><button id="close-tool-help" class="modal-close" aria-label="关闭使用说明">×</button></div><p class="modal-lead">' + guide.lead + '</p>' + sections + roles + exampleHtml;
  $("#close-tool-help").addEventListener("click", () => setToolHelpVisible(false));
  setToolHelpVisible(true);
}

function formatTraceValue(key, value) {
  if (value === undefined || value === null || value === "") return "—";
  if (key === "tools" && Array.isArray(value)) return value.map((item) => item.name + "（" + item.reason + "）").join("；") || "未选择工具";
  if (key === "confidence" && typeof value === "number") return Math.round(value * 100) + "%";
  if (key === "context" && typeof value === "object") {
    const parts = [];
    if (value.before !== undefined) parts.push("原始 " + value.before + " tokens");
    if (value.after !== undefined) parts.push("压缩后 " + value.after + " tokens");
    if (value.before !== undefined && value.after !== undefined) parts.push("减少 " + Math.max(0, Math.round((1 - value.after / Math.max(1, value.before)) * 100)) + "%");
    if (value.strategy) parts.push(value.strategy === "not_needed" ? "无需压缩" : "已归档压缩");
    return parts.join(" · ") || JSON.stringify(value);
  }
  if (Array.isArray(value)) return value.map((item) => typeof item === "object" ? JSON.stringify(item) : item).join("、") || "无";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function traceSummary(event) {
  const entries = Object.entries(event.details || {});
  if (!entries.length) return event.message;
  const preferred = entries.find(([key]) => ["tools", "tool", "reason", "tables", "sql", "issues", "row_count", "result_count", "mode", "error"].includes(key)) || entries[0];
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
  const toolPlan = findStage("tool_plan"); const toolEvents = events.filter((event) => event.stage === "tool");
  const tables = recall?.details?.tables;
  const toolDetail = toolEvents.length ? toolEvents.map((event) => event.details.tool + "（" + event.details.result_count + " 条）").join("、") : (toolPlan ? "未命中适用 AUTO 工具，未调用任何工具" : "正在分析可用工具");
  const toolState = toolEvents.length ? "pass" : (inProgress ? "waiting" : "hold");
  const toolVerdict = toolEvents.length ? "已调用白名单" : (inProgress ? "规划中" : "未调用");
  const toolGuard = guardItem("工具白名单编排", toolDetail, toolState, toolVerdict);
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
  $("#guard-list").innerHTML = toolGuard + metadata + guardItem("SQL 审查", reviewDetail, reviewState, reviewLabel) + guardItem("风险分级", riskReason, riskState, riskLabel) + guardItem("全链路审计", inProgress ? "将在本次工作流结束后写入防篡改审计链" : auditDetail, inProgress ? "waiting" : "pass", inProgress ? "等待结束" : "已留痕");
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
    const preview = (row) => {
      const payload = row.payload || {};
      if (row.action === "agent_tool_plan") {
        const tools = (payload.tools || []).map((item) => item.name).join("、") || "未选择工具";
        return (payload.planner || "工具规划") + " 选择 " + tools + (payload.latency_ms !== undefined ? " · " + payload.latency_ms + "ms" : "");
      }
      if (row.action === "agent_tool_invoked") {
        return (payload.tool || "未知工具") + " · " + (payload.status || "未知状态") + " · 返回 " + (payload.result_count ?? 0) + " 条";
      }
      if (row.action === "agent_chat_stream_completed") {
        return (payload.provider || "模型") + " 流式对话完成 · 上下文 " + (payload.context_messages ?? 0) + " 条";
      }
      if (row.action === "chat_conversation_created") return "创建聊天会话 · " + (payload.role || "未标注角色");
      if (row.action === "sql_executed") return (payload.question || "查询") + " · 返回 " + (payload.row_count ?? 0) + " 条";
      return payload.question || payload.sql || (Array.isArray(payload.sources) ? payload.sources.join("、") : "") || "系统事件";
    };
    target.innerHTML = rows.length ? rows.map((row) => `<div class="audit-row"><span class="audit-action">${escapeHtml(row.action)}</span><span class="audit-payload">${escapeHtml(preview(row))}</span><span class="audit-time">${new Date(row.created_at).toLocaleString("zh-CN", {hour12:false})}</span></div>`).join("") : "<div class='empty-state'><strong>暂无审计记录</strong></div>";
    $("#audit-page-note").textContent = "第 " + (Math.floor(auditOffset / recordPageSize) + 1) + " 页 · 本页 " + rows.length + " 条 / 共 " + summary.total + " 条";
    $("#prev-audit").disabled = auditOffset === 0; $("#next-audit").disabled = auditOffset + rows.length >= summary.total;
  } catch { target.innerHTML = "<div class='empty-state'><strong>无法加载审计记录</strong></div>"; }
}

function renderSkills() {
  const target = $("#skills-list");
  const kind = $("#skill-filter").value;
  const risk = $("#skill-risk-filter").value;
  const skills = allSkills.filter((skill) => (!kind || (kind === "runnable" ? skill.runnable : !skill.runnable)) && (!risk || skill.risk === risk));
  $("#skill-count-note").textContent = "显示 " + skills.length + " / " + allSkills.length + " 个能力";
  target.innerHTML = skills.length ? skills.map((skill, index) => {
      const suggestions = (skill.suggestions || []).map((item) => '<button class="skill-suggestion" data-run-skill="' + escapeHtml(skill.name) + '" data-skill-input="' + escapeHtml(item) + '">' + escapeHtml(item) + '</button>').join("");
      const run = skill.runnable ? '<button class="primary-button skill-run" data-run-skill="' + escapeHtml(skill.name) + '">运行 Skill ↗</button>' : '<span class="skill-tag">规范型能力</span>';
      return `<article class="card skill-card"><span class="skill-symbol">${index ? "⌘" : "◈"}</span><span class="skill-risk ${escapeHtml(skill.risk || "auto")}">${escapeHtml((skill.category || "通用") + " · " + (skill.risk || "auto").toUpperCase())}</span><h3>${escapeHtml(skill.name)}</h3><p>${escapeHtml(skill.description || "可复用的运维领域能力")}</p><div class="skill-suggestions">${suggestions}</div><div class="skill-actions"><button class="text-button" data-skill="${escapeHtml(skill.name)}">查看完整 SOP</button>${run}</div></article>`;
  }).join("") : "<div class='empty-state'><strong>没有符合筛选条件的 Skill</strong><p>调整类型或风险筛选后重试。</p></div>";
  document.querySelectorAll("[data-skill]").forEach((button) => button.addEventListener("click", () => viewSkill(button.dataset.skill)));
  document.querySelectorAll("[data-run-skill]").forEach((button) => button.addEventListener("click", () => runSkill(button.dataset.runSkill, button.dataset.skillInput || "")));
}

function renderSkillHistory(rows) {
  $("#skill-history-list").innerHTML = rows.length ? rows.map((row) => '<div><strong>' + escapeHtml(row.payload.skill || "未知 Skill") + '</strong><span>' + escapeHtml(row.payload.input || "未填写业务上下文") + '</span><em>' + escapeHtml(row.payload.status || "—") + ' · ' + new Date(row.created_at).toLocaleString("zh-CN", {month:"numeric", day:"numeric", hour:"2-digit", minute:"2-digit", hour12:false}) + '</em></div>').join("") : "<div class='empty-state'><strong>暂未运行过 Skill</strong><p>选择一个可运行 Skill 后，记录会显示在这里。</p></div>";
}

async function loadSkillHistory() {
  try {
    const response = await fetch("/api/v1/skills/history?role=" + encodeURIComponent(currentRole())); const rows = await response.json();
    if (!response.ok) throw new Error(rows.detail || "加载失败"); renderSkillHistory(rows);
  } catch { $("#skill-history-list").innerHTML = "<div class='empty-state'><strong>无法加载 Skill 运行记录</strong></div>"; }
}

async function loadSkills() {
  try {
    const response = await fetch("/api/v1/skills"); allSkills = await response.json();
    if (!response.ok) throw new Error("加载失败"); renderSkills(); loadSkillHistory();
  } catch { $("#skills-list").innerHTML = "<p>加载 Skills 失败。</p>"; }
}

async function viewSkill(name) {
  try {
    const skill = await (await fetch(`/api/v1/skills/${encodeURIComponent(name)}`)).json();
    $("#skill-content").textContent = skill.content; $("#skill-detail").hidden = false;
    $("#skill-detail").scrollIntoView({ behavior:"smooth", block:"start" });
  } catch { toast("无法读取 Skill 内容"); }
}

function runSkill(name, suggestedInput) {
  activeAuditAction = "skill";
  activeSkillRun = { name, suggestedInput: suggestedInput || "" };
  renderAuditAction("运行 " + name, "SKILL RUNTIME", '<p class="modal-lead">填写本次业务上下文后，Skill 会执行其注册的只读诊断流程，并将输入摘要、结果状态写入审计记录。</p><label class="action-field">业务上下文<textarea id="skill-run-input" maxlength="500" placeholder="例如：华东 P1 网关离线，影响支付链路">' + escapeHtml(suggestedInput || "") + '</textarea></label><div class="action-callout"><strong>安全边界</strong><span>Skill 只读取授权数据并输出建议，不会关闭告警、创建工单、修改数据库或直接执行命令。</span></div>', "运行 Skill");
  setAuditActionVisible(true);
}

async function executeSkill(name, input) {
  try {
    const response = await fetch("/api/v1/skills/" + encodeURIComponent(name) + "/run", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ role:currentRole(), requester:"Lenovo", user_input:input }) });
    const result = await response.json(); if (!response.ok) throw new Error(result.detail || "Skill 运行失败");
    const steps = (result.next_steps || []).map((item) => "- " + item).join("\n");
    $("#skill-content").textContent = "# " + name + " 运行结果\n\n状态：" + result.status + "\n风险：" + result.risk + "\n\n## 结论\n" + result.summary + "\n\n## 下一步\n" + (steps || "无") + "\n\n## 结构化数据\n" + JSON.stringify(result.data, null, 2);
    $("#skill-detail").hidden = false; $("#skill-detail").scrollIntoView({ behavior:"smooth", block:"start" });
    toast(name + " 运行完成"); loadAudit(); loadSkillHistory();
  } catch (error) { toast(error.message || "Skill 运行失败"); }
}

function renderKnowledge(documents, total) {
  const target = $("#knowledge-list");
  knowledgeRecordCache = new Map(documents.map((item) => [String(item.id), item]));
  target.innerHTML = documents.length ? documents.map((item) => { const statusLabel = item.status === "expired" ? "已过期" : item.status === "expiring" ? "7天内过期" : "有效"; const expiryHint = item.expires_in_days !== null && item.expires_in_days !== undefined && item.status !== "expired" ? " · " + (item.status === "expiring" ? "即将过期" : "剩余 " + item.expires_in_days + " 天") : ""; return `<article class="knowledge-item"><div class="knowledge-item-actions"><button class="text-button" data-edit-knowledge="${item.id}">编辑</button><button class="text-button knowledge-versions" data-knowledge-versions="${item.id}">版本</button><button class="text-button knowledge-chunks" data-knowledge-chunks="${item.id}">分块</button><button class="text-button knowledge-delete" data-delete-knowledge="${item.id}">删除</button></div><strong>${escapeHtml(item.title)} <em class="knowledge-status ${item.status}">${statusLabel}</em></strong><small>版本 v${item.version} · 更新于 ${new Date(item.updated_at).toLocaleDateString("zh-CN")}${item.expires_at ? " · 有效至 " + item.expires_at + expiryHint : ""}</small>${item.status === "expiring" ? '<div class="knowledge-expiry-warning">⚠ 文档即将过期，建议编辑后延长有效期。</div>' : item.status === "expired" ? '<div class="knowledge-expiry-warning expired">⚠ 文档已过期，不会参与 RAG 检索。</div>' : ""}<p class="knowledge-content-preview">${escapeHtml(item.content)}</p>${item.content.length > 180 ? '<button class="text-button knowledge-expand" data-knowledge-expand>展开全文 ↓</button>' : ""}<div>${String(item.tags || "未分类").split(/[,，]/).filter(Boolean).map((tag) => `<span>${escapeHtml(tag.trim())}</span>`).join("")}</div></article>`; }).join("") : "<div class='empty-state'><strong>知识库为空</strong><p>新增一份 SOP 后即可在 RAG 中使用。</p></div>";
  document.querySelectorAll("[data-delete-knowledge]").forEach((button) => button.addEventListener("click", () => deleteKnowledge(button.dataset.deleteKnowledge)));
  document.querySelectorAll("[data-knowledge-chunks]").forEach((button) => button.addEventListener("click", () => showKnowledgeChunks(button.dataset.knowledgeChunks)));
  document.querySelectorAll("[data-edit-knowledge]").forEach((button) => button.addEventListener("click", () => startKnowledgeEdit(button.dataset.editKnowledge)));
  document.querySelectorAll("[data-knowledge-versions]").forEach((button) => button.addEventListener("click", () => showKnowledgeVersions(button.dataset.knowledgeVersions)));
  document.querySelectorAll("[data-knowledge-expand]").forEach((button) => button.addEventListener("click", () => { const preview = button.previousElementSibling; const expanded = preview.classList.toggle("expanded"); button.textContent = expanded ? "收起全文 ↑" : "展开全文 ↓"; }));
  const page = total ? Math.floor(knowledgeOffset / knowledgePageSize) + 1 : 1;
  const pages = Math.max(1, Math.ceil(total / knowledgePageSize));
  $("#knowledge-page-note").textContent = `第 ${page} / ${pages} 页 · 共 ${total} 份`;
  $("#prev-knowledge").disabled = knowledgeOffset === 0;
  $("#next-knowledge").disabled = knowledgeOffset + knowledgePageSize >= total;
}

async function loadKnowledge() {
  try {
    if (!$("#run-knowledge-evaluation")) { const button = document.createElement("button"); button.id = "run-knowledge-evaluation"; button.className = "text-button"; button.textContent = "运行检索评测"; button.addEventListener("click", runKnowledgeEvaluation); document.querySelector(".knowledge-actions").prepend(button); }
    const tag = $("#knowledge-tag-filter").value; const status = $("#knowledge-status-filter").value;
    const response = await fetch("/api/v1/knowledge?role=" + encodeURIComponent(currentRole()) + "&limit=" + knowledgePageSize + "&offset=" + knowledgeOffset + "&tag=" + encodeURIComponent(tag) + "&status=" + encodeURIComponent(status));
    const payload = await response.json(); if (!response.ok) throw new Error(payload.detail);
    if (!payload.items.length && knowledgeOffset > 0) { knowledgeOffset = Math.max(0, knowledgeOffset - knowledgePageSize); return loadKnowledge(); }
    renderKnowledge(payload.items, payload.total);
  } catch {
    $("#knowledge-list").innerHTML = "<div class='empty-state'><strong>无法加载知识库</strong></div>";
    $("#knowledge-page-note").textContent = "加载失败";
  }
}

async function runKnowledgeEvaluation() {
  const button = $("#run-knowledge-evaluation"); button.disabled = true; button.textContent = "评测中…";
  try { const response = await fetch("/api/v1/knowledge/evaluation", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({role:currentRole(), requester:"Lenovo"})}); const report = await response.json(); if (!response.ok) throw new Error(report.detail); const target = $("#knowledge-search-result"); target.hidden = false; target.innerHTML = '<div class="rag-evaluation"><div class="rag-evaluation-heading"><strong>RAG 检索质量评测</strong><small>' + report.total + ' 条用例 · 评测 ID ' + escapeHtml(report.evaluation_id.slice(0, 8)) + '…</small></div><div class="rag-eval-metrics"><span>Recall@3<strong>' + report.recall_at_3 + '%</strong></span><span>Hit@1<strong>' + report.hit_at_1 + '%</strong></span><span>MRR<strong>' + report.mrr + '</strong></span><span>引用准确率<strong>' + report.citation_accuracy + '%</strong></span><span>证据覆盖率<strong>' + report.evidence_coverage + '%</strong></span><span>通过<strong>' + report.passed + '/' + report.total + '</strong></span></div><div class="rag-eval-results">' + report.results.map((item, index) => '<div class="rag-eval-case ' + (item.passed ? 'passed' : 'failed') + '"><div><b>' + (item.passed ? '✓' : '×') + ' ' + escapeHtml(item.question) + '</b><small>期望：' + escapeHtml(item.expected) + ' · 命中位置：' + (item.rank ? 'Top-' + item.rank : '未命中') + ' · Top 分数：' + item.top_score + '</small><small>证据：' + (item.evidence || []).map((e) => 'Top-' + e.rank + ' ' + escapeHtml(e.title) + '（' + e.score + '）').join('；') + '</small></div><button class="text-button rag-feedback" data-eval-id="' + escapeHtml(report.evaluation_id) + '" data-eval-question="' + escapeHtml(item.question) + '">人工评分</button></div>').join('') + '</div><div class="rag-manual-summary" id="rag-manual-summary">人工评分：尚未评分</div></div>'; document.querySelectorAll(".rag-feedback").forEach((feedbackButton) => feedbackButton.addEventListener("click", () => submitKnowledgeFeedback(feedbackButton))); toast("RAG 检索评测已完成"); loadAudit(); } catch (error) { toast(error.message || "检索评测失败"); } finally { button.disabled = false; button.textContent = "运行检索评测"; }
}

async function submitKnowledgeFeedback(button) {
  const score = Number(prompt("请为这条检索结果评分（1-5分）", "5")); if (!Number.isInteger(score) || score < 1 || score > 5) return toast("评分必须是 1 到 5 的整数");
  const comment = prompt("可选：填写证据是否准确、是否需要补充文档", "") || "";
  try { const response = await fetch("/api/v1/knowledge/evaluation/feedback", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({evaluation_id:button.dataset.evalId, question:button.dataset.evalQuestion, score, comment, role:currentRole(), requester:"Lenovo"})}); const result = await response.json(); if (!response.ok) throw new Error(result.detail || "评分保存失败"); $("#rag-manual-summary").textContent = "人工评分：" + result.summary.average_score + " / 5（已评 " + result.summary.count + " 条）"; button.textContent = "已评分 " + score + " 分"; button.disabled = true; toast("人工评分已保存"); loadAudit(); } catch (error) { toast(error.message || "评分保存失败"); }
}

async function loadKnowledgeTags() {
  try { const response = await fetch("/api/v1/knowledge/tags?role=" + encodeURIComponent(currentRole())); const tags = await response.json(); if (!response.ok) throw new Error(); $("#knowledge-tag-filter").innerHTML = '<option value="">全部标签</option>' + tags.map((tag) => '<option value="' + escapeHtml(tag) + '">' + escapeHtml(tag) + '</option>').join(""); } catch { /* keep default filter */ }
}

async function showKnowledgeChunks(documentId) {
  try {
    const response = await fetch("/api/v1/knowledge/" + documentId + "/chunks?role=" + encodeURIComponent(currentRole())); const chunks = await response.json(); if (!response.ok) throw new Error(chunks.detail || "加载失败");
    activeAuditAction = "";
    renderAuditAction("文档分块预览", "KNOWLEDGE CHUNKS", '<p class="modal-lead">共 ' + chunks.length + ' 个分块。每个分块是 RAG 检索、评分和引用的最小证据单元。</p><div class="knowledge-chunk-list">' + chunks.map((chunk) => '<div><strong>片段 ' + (chunk.chunk_index + 1) + ' · 约 ' + chunk.token_count + ' Token</strong><p>' + escapeHtml(chunk.content) + '</p></div>').join("") + '</div>', "关闭");
    $("#confirm-audit-action").onclick = () => setAuditActionVisible(false); setAuditActionVisible(true);
  } catch (error) { toast(error.message || "无法加载文档分块"); }
}

async function showKnowledgeVersions(documentId) {
  try {
    const response = await fetch("/api/v1/knowledge/" + documentId + "/versions?role=" + encodeURIComponent(currentRole())); const versions = await response.json();
    if (!response.ok) throw new Error(versions.detail || "版本加载失败");
    const versionMap = new Map(versions.map((item) => [String(item.version), item]));
    const options = versions.map((item) => `<option value="${item.version}">v${item.version} · ${new Date(item.created_at).toLocaleString("zh-CN", {hour12:false})}</option>`).join("");
    const compareFrom = versions[1]?.version || versions[0]?.version || ""; const compareTo = versions[0]?.version || "";
    renderAuditAction("文档版本历史", "VERSION CONTROL", `<p class="modal-lead">共 ${versions.length} 个版本。编辑和回滚都会生成新版本，历史内容不会被覆盖。</p><label class="action-field">对比基准版本<select id="knowledge-version-from">${options}</select></label><label class="action-field">对比目标版本<select id="knowledge-version-to">${options}</select></label><div id="knowledge-version-diff" class="knowledge-version-diff"></div><label class="action-field">回滚目标版本<select id="knowledge-rollback-target">${options}</select></label>`, versions.length ? "回滚到选中版本" : "关闭");
    $("#knowledge-version-from").value = String(compareFrom); $("#knowledge-version-to").value = String(compareTo); $("#knowledge-rollback-target").value = String(compareTo);
    const renderDiff = () => { const from = versionMap.get($("#knowledge-version-from").value); const to = versionMap.get($("#knowledge-version-to").value); if (!from || !to) return; const oldLines = from.content.split(/\r?\n/); const newLines = to.content.split(/\r?\n/); const same = from.title === to.title && from.tags === to.tags && from.content === to.content; $("#knowledge-version-diff").innerHTML = same ? "<strong>两个版本内容一致。</strong>" : '<strong>版本差异预览</strong><pre>' + (from.title !== to.title ? "- 标题：" + escapeHtml(from.title) + "\n+ 标题：" + escapeHtml(to.title) + "\n" : "") + (from.tags !== to.tags ? "- 标签：" + escapeHtml(from.tags) + "\n+ 标签：" + escapeHtml(to.tags) + "\n" : "") + oldLines.map((line) => "- " + escapeHtml(line)).join("\n") + "\n" + newLines.map((line) => "+ " + escapeHtml(line)).join("\n") + "</pre>"; };
    $("#knowledge-version-from").addEventListener("change", renderDiff); $("#knowledge-version-to").addEventListener("change", renderDiff); renderDiff();
    $("#confirm-audit-action").onclick = () => rollbackKnowledge(documentId, Number($("#knowledge-rollback-target").value)); setAuditActionVisible(true);
  } catch (error) { toast(error.message || "无法加载文档版本"); }
}

async function rollbackKnowledge(documentId, version) {
  if (!confirm("确认回滚到 v" + version + "？系统会创建一条新的版本记录，当前版本仍会保留。")) return;
  try { const response = await fetch("/api/v1/knowledge/" + documentId + "/rollback/" + version, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({role:currentRole(), requester:"Lenovo"})}); const result = await response.json(); if (!response.ok) throw new Error(result.detail || "回滚失败"); setAuditActionVisible(false); toast("已回滚，并生成 v" + result.version); loadKnowledge(); loadAudit(); } catch (error) { toast(error.message || "回滚失败"); }
}

function startKnowledgeEdit(documentId) {
  const item = knowledgeRecordCache.get(String(documentId)); if (!item) return toast("当前文档不在页面缓存中，请刷新后重试");
  editingKnowledgeId = Number(documentId); $("#knowledge-form-title").textContent = "编辑运维知识 · 当前 v" + item.version; $("#knowledge-title").value = item.title; $("#knowledge-tags").value = item.tags; $("#knowledge-expires-at").value = item.expires_at || ""; $("#knowledge-content").value = item.content; $("#save-knowledge").innerHTML = "保存新版本 <span>↗</span>"; $("#cancel-edit-knowledge").hidden = false; $("#knowledge-title").scrollIntoView({behavior:"smooth", block:"center"});
}

function cancelKnowledgeEdit() {
  editingKnowledgeId = null; $("#knowledge-form-title").textContent = "新增运维知识"; $("#knowledge-title").value = ""; $("#knowledge-tags").value = ""; $("#knowledge-expires-at").value = ""; $("#knowledge-content").value = ""; $("#knowledge-file").value = ""; $("#save-knowledge").innerHTML = "保存并纳入 RAG <span>↗</span>"; $("#cancel-edit-knowledge").hidden = true;
}

async function uploadKnowledge() {
  const file = $("#knowledge-file").files[0]; if (!file) return toast("请先选择 .txt 或 .md 文件");
  const suffix = file.name.toLowerCase().slice(file.name.lastIndexOf(".")); if (![".txt", ".md"].includes(suffix)) return toast("目前仅支持 .txt 或 .md 文档");
  const content = await file.text(); const title = $("#knowledge-title").value.trim() || file.name.replace(/\.(txt|md)$/i, ""); const button = $("#upload-knowledge"); button.disabled = true; button.textContent = "上传中…";
  try { const response = await fetch("/api/v1/knowledge/upload", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({filename:file.name, title, tags:$("#knowledge-tags").value.trim() || "未分类", expires_at:$("#knowledge-expires-at").value, content, role:currentRole(), requester:"Lenovo"})}); const result = await response.json(); if (!response.ok) throw new Error(result.detail || "上传失败"); cancelKnowledgeEdit(); knowledgeOffset = 0; toast("文档已上传为 v" + result.version + "，并完成 RAG 索引"); loadKnowledge(); loadAudit(); loadMetrics(); } catch (error) { toast(error.message || "上传失败"); } finally { button.disabled = false; button.textContent = "读取并上传文档"; }
}

async function saveKnowledge() {
  const title = $("#knowledge-title").value.trim(); const tags = $("#knowledge-tags").value.trim() || "未分类"; const content = $("#knowledge-content").value.trim(); const expires_at = $("#knowledge-expires-at").value;
  if (title.length < 2 || content.length < 10) return toast("标题至少 2 个字符，正文至少 10 个字符");
  const button = $("#save-knowledge"); button.disabled = true; button.textContent = "保存中…";
  try {
    const editingId = editingKnowledgeId; const url = editingId ? "/api/v1/knowledge/" + editingId : "/api/v1/knowledge"; const method = editingId ? "PUT" : "POST";
    const response = await fetch(url, { method, headers:{"Content-Type":"application/json"}, body:JSON.stringify({ title, tags, content, expires_at, role:currentRole(), requester:"Lenovo" }) });
    const document = await response.json(); if (!response.ok) throw new Error(document.detail || "保存失败");
    cancelKnowledgeEdit(); knowledgeOffset = 0; toast(editingId ? `“${document.title}” 已保存为 v${document.version}` : `“${document.title}” 已纳入 RAG`); loadKnowledge(); loadAudit(); loadMetrics();
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
    target.innerHTML = evidence.length ? evidence.map((item) => '<div class="knowledge-evidence"><strong>' + escapeHtml(item.title) + " · 片段 " + (item.chunk_index + 1) + '</strong><small>混合得分 ' + item.score + " · 词法 " + item.lexical_score + " · 向量 " + item.semantic_score + '</small><p>' + escapeHtml(item.content) + '</p></div>').join("") : '<div class="knowledge-evidence">没有检索到足够匹配的证据。</div>';
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

let activeAuditAction = "";
let activeSkillRun = null;

function setAuditActionVisible(visible) {
  const modal = $("#audit-action-modal");
  modal.hidden = !visible;
  document.body.classList.toggle("modal-open", visible);
  if (visible) setTimeout(() => $("#cancel-audit-action").focus(), 0);
}

function renderAuditAction(title, eyebrow, body, confirmText) {
  $("#audit-action-content").innerHTML = '<div class="modal-heading"><div><p class="section-label">' + eyebrow + '</p><h2 id="audit-action-title">' + title + '</h2></div><button id="close-audit-action" class="modal-close" aria-label="关闭">×</button></div>' + body;
  $("#confirm-audit-action").textContent = confirmText;
  $("#confirm-audit-action").onclick = null;
  $("#close-audit-action").addEventListener("click", () => setAuditActionVisible(false));
}

function openIntegrityDialog() {
  activeAuditAction = "integrity";
  renderAuditAction("验证审计完整性", "AUDIT HASH CHAIN", '<p class="modal-lead">选择要校验的审计范围。校验会重新计算记录的 SHA-256 哈希链，只读执行，不会修改审计或业务数据。</p><label class="action-field">校验范围<select id="integrity-scope"><option value="full">全量哈希链（推荐）</option><option value="recent_100">最近 100 条审计记录</option></select></label><div id="integrity-scope-note" class="action-callout"><strong>全量校验</strong><span>从第一条记录遍历至今，适合提交前或故障复盘时确认完整审计链。</span></div><p class="action-footnote">说明：这是本地演示环境的哈希链校验，不等同于外部不可篡改存证。</p>', "开始校验");
  $("#integrity-scope").addEventListener("change", (event) => {
    const recent = event.target.value === "recent_100";
    $("#integrity-scope-note").innerHTML = recent
      ? "<strong>最近 100 条</strong><span>仅校验最近一段记录及其前序锚点，速度更快；它不能证明更早历史记录的完整性。</span>"
      : "<strong>全量校验</strong><span>从第一条记录遍历至今，适合提交前或故障复盘时确认完整审计链。</span>";
  });
  setAuditActionVisible(true);
}

function expectedStatusLabel(status) {
  return ({ completed:"完成只读查询", answered_by_rag:"知识库回答", approval_required:"创建审批单", blocked:"策略阻断" })[status] || status;
}

function evaluationCasesMarkup(cases) {
  return cases.map((item) => '<label class="evaluation-case"><input type="checkbox" data-evaluation-case value="' + escapeHtml(item.id) + '" checked /><span><strong>' + escapeHtml(item.name) + '</strong><small>' + escapeHtml(item.question) + '</small></span><em class="evaluation-source ' + escapeHtml(item.source) + '">' + (item.source === "baseline" ? "内置基线" : "自定义") + '</em><b>期望：' + escapeHtml(expectedStatusLabel(item.expected_status)) + '</b>' + (item.source === "custom" ? '<button type="button" class="evaluation-delete" data-delete-evaluation-case="' + escapeHtml(item.id) + '">删除</button>' : "") + '</label>').join("");
}

async function openEvaluationDialog() {
  activeAuditAction = "evaluation";
  try {
    const response = await fetch("/api/v1/evaluations/cases?role=" + encodeURIComponent(currentRole()));
    const cases = await response.json();
    if (!response.ok) throw new Error(cases.detail || "无法读取评测用例");
    const canManage = currentRole() !== "viewer";
    const customForm = canManage
      ? '<div class="evaluation-case-form"><h3>新增自定义用例</h3><div class="evaluation-form-grid"><label>用例名称<input id="evaluation-case-name" maxlength="80" placeholder="例如：华东离线资产查询" /></label><label>期望结果<select id="evaluation-case-expected"><option value="completed">完成只读查询</option><option value="answered_by_rag">知识库回答</option><option value="approval_required">创建审批单</option><option value="blocked">策略阻断</option></select></label></div><label>用户问题<textarea id="evaluation-case-question" maxlength="500" placeholder="例如：查询华东区离线设备"></textarea></label><button id="create-evaluation-case" type="button" class="secondary-button">保存到用例库</button></div>'
      : '<div class="action-callout"><strong>观察者为只读模式</strong><span>可运行内置或已有自定义用例；切换为运维工程师后，可新增或删除自定义评测用例。</span></div>';
    renderAuditAction("运行评测", "EVALUATION CONSOLE", '<p class="modal-lead">评测会实际走一遍当前安全工作流，结果与预期状态逐条比较。写操作类用例只会创建审批单，默认安全模式不会改动业务表。</p><label class="action-field">运行范围<select id="evaluation-scope"><option value="baseline">只运行内置安全基线（5 条）</option><option value="all">运行全部用例（含自定义）</option><option value="selected">运行我勾选的用例</option></select></label><div id="evaluation-scope-note" class="action-callout"><strong>内置基线</strong><span>覆盖结构化查询、RAG 回答与危险写请求拦截，用于每次改动后的基础回归。</span></div><div class="evaluation-case-heading"><strong>用例库（' + cases.length + ' 条）</strong><small>仅“运行我勾选的用例”会采用下方勾选项。</small></div><div class="evaluation-case-list">' + evaluationCasesMarkup(cases) + '</div>' + customForm + '<p class="action-footnote">每次运行都会新增审计事件；期望为“创建审批单”的用例还会留下待处理审批单，便于验证人工介入链路。</p>', "运行评测");
    $("#evaluation-scope").addEventListener("change", (event) => {
      const selected = event.target.value === "selected";
      $("#evaluation-scope-note").innerHTML = selected
        ? "<strong>指定用例</strong><span>只运行当前勾选的用例，适合针对某一条新规则或一次回归失败进行复测。</span>"
        : event.target.value === "all"
          ? "<strong>全部用例</strong><span>运行内置基线和全部自定义用例，适合功能版本验收。</span>"
          : "<strong>内置基线</strong><span>覆盖结构化查询、RAG 回答与危险写请求拦截，用于每次改动后的基础回归。</span>";
    });
    $("#create-evaluation-case")?.addEventListener("click", createEvaluationCase);
    document.querySelectorAll("[data-delete-evaluation-case]").forEach((button) => button.addEventListener("click", () => deleteEvaluationCase(button.dataset.deleteEvaluationCase)));
    setAuditActionVisible(true);
  } catch (error) {
    toast(error.message || "无法打开评测配置");
  }
}

async function createEvaluationCase() {
  const name = $("#evaluation-case-name").value.trim();
  const question = $("#evaluation-case-question").value.trim();
  const expected_status = $("#evaluation-case-expected").value;
  if (name.length < 2 || question.length < 2) return toast("请填写至少 2 个字符的用例名称和问题");
  const response = await fetch("/api/v1/evaluations/cases", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ name, question, expected_status, role:currentRole(), requester:"Lenovo" }) });
  const result = await response.json();
  if (!response.ok) return toast(result.detail || "保存用例失败");
  toast("自定义用例已保存");
  openEvaluationDialog();
}

async function deleteEvaluationCase(caseId) {
  if (!confirm("确认删除这条自定义评测用例？不会影响历史审计和评测记录。")) return;
  const response = await fetch("/api/v1/evaluations/cases/" + encodeURIComponent(caseId), { method:"DELETE", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ role:currentRole(), requester:"Lenovo" }) });
  const result = await response.json();
  if (!response.ok) return toast(result.detail || "删除用例失败");
  toast("自定义用例已删除");
  openEvaluationDialog();
}

async function runEvaluation(scope, caseIds) {
  const button = $("#run-evaluation"); button.disabled = true; button.textContent = "评测中…";
  try {
    const response = await fetch("/api/v1/evaluations/run", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ scope, case_ids:caseIds, role:currentRole(), requester:"Lenovo" }) });
    const report = await response.json();
    if (!response.ok) throw new Error(report.detail || "评测执行失败");
    $("#metric-eval").textContent = report.success_rate + "%";
    const structuredRate = report.structured_query_pass_rate === null ? "不适用" : report.structured_query_pass_rate + "%";
    const safetyRate = report.high_risk_interception_rate === null ? "不适用" : report.high_risk_interception_rate + "%";
    const target = $("#evaluation-result"); target.hidden = false; target.className = "evaluation-result";
    target.innerHTML = '<strong>评测完成：' + report.passed + '/' + report.dataset_size + ' 通过（' + report.success_rate + '%）</strong><span>结构化查询用例通过率 ' + structuredRate + ' · 高风险拦截率 ' + safetyRate + '</span><div class="eval-case-results">' + report.cases.map((item) => '<div class="' + (item.passed ? "passed" : "failed") + '"><b>' + (item.passed ? "✓" : "×") + '</b><span>' + escapeHtml(item.name) + '：期望 ' + escapeHtml(expectedStatusLabel(item.expected)) + '，实际 ' + escapeHtml(expectedStatusLabel(item.actual)) + '</span></div>').join("") + '</div>';
    toast("评测已完成"); loadAudit(); loadMetrics();
  } catch (error) { toast(error.message || "评测执行失败"); }
  finally { button.disabled = false; button.textContent = "运行评测"; }
}

async function verifyAuditIntegrity(scope) {
  const button = $("#verify-audit"); button.disabled = true; button.textContent = "校验中…";
  try {
    const response = await fetch("/api/v1/audit/integrity?scope=" + encodeURIComponent(scope));
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "审计完整性校验失败");
    const full = result.scope === "full";
    const target = $("#evaluation-result"); target.hidden = false;
    target.className = "evaluation-result " + (result.valid ? "integrity-ok" : "integrity-broken");
    target.innerHTML = result.valid
      ? '<strong>审计校验通过：已校验 ' + result.checked_events + '/' + result.total_events + ' 条记录。</strong><span>' + (full ? "全量哈希链未发现断裂。" : "已校验最近 100 条及前序锚点；更早历史未在本次范围内。") + '</span>'
      : '<strong>审计链异常：校验范围内存在哈希断裂。</strong><span>请停止依赖该链进行合规判断，并核查数据库与运行日志。</span>';
    toast(result.valid ? "审计完整性校验通过" : "发现审计链异常");
  } catch (error) { toast(error.message || "审计完整性校验失败"); }
  finally { button.disabled = false; button.textContent = "验证完整性"; }
}

document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll(".nav-item").forEach((node) => node.classList.remove("active")); button.classList.add("active");
  document.querySelectorAll(".page").forEach((page) => page.classList.remove("active-page")); $(`#${button.dataset.page}-page`).classList.add("active-page");
  const titles = { agent:"智能运维查询", chat:"Agent 聊天", monitoring:"监控中心", metrics:"指标中心", data:"数据浏览器", audit:"审计中心", approval:"审批中心", policy:"权限中心", knowledge:"知识库", tools:"工具中心", skills:"Skills 与 SOP" }; $("#page-title").textContent = titles[button.dataset.page];
  if (button.dataset.page === "chat") loadChat(); if (button.dataset.page === "monitoring") loadMonitoring(); if (button.dataset.page === "metrics") loadMetricCatalog(); if (button.dataset.page === "data") loadDataExplorer(); if (button.dataset.page === "audit") loadAudit(); if (button.dataset.page === "approval") loadApprovals(); if (button.dataset.page === "policy") loadPolicies(); if (button.dataset.page === "skills") loadSkills(); if (button.dataset.page === "knowledge") { loadKnowledgeTags(); loadKnowledge(); } if (button.dataset.page === "tools") loadTools();
}));
$("#run-query").addEventListener("click", runQuery);
document.querySelectorAll("[data-guide]").forEach((button) => button.addEventListener("click", () => openPageGuide(button.dataset.guide)));
$("#tool-help-got-it").addEventListener("click", () => setToolHelpVisible(false));
$("#tool-help-modal").addEventListener("click", (event) => { if (event.target === event.currentTarget) setToolHelpVisible(false); });
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!$("#tool-help-modal").hidden) setToolHelpVisible(false);
  if (!$("#audit-action-modal").hidden) setAuditActionVisible(false);
});
$("#guard-open-audit").addEventListener("click", () => document.querySelector('.nav-item[data-page="audit"]').click());
$("#guard-open-approval").addEventListener("click", () => document.querySelector('.nav-item[data-page="approval"]').click());
$("#new-chat").addEventListener("click", createChat);
$("#send-chat").addEventListener("click", sendChat);
$("#chat-input").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); sendChat(); } });
document.querySelectorAll("[data-query]").forEach((button) => button.addEventListener("click", () => { document.querySelector('.nav-item[data-page="agent"]').click(); $("#question").value = button.dataset.query; runQuery(); }));
$("#copy-sql").addEventListener("click", async () => { await navigator.clipboard.writeText(latestSql); toast("SQL 已复制到剪贴板"); });
$("#cancel-audit-action").addEventListener("click", () => setAuditActionVisible(false));
$("#audit-action-modal").addEventListener("click", (event) => { if (event.target === event.currentTarget) setAuditActionVisible(false); });
$("#confirm-audit-action").addEventListener("click", async () => {
  if (activeAuditAction === "integrity") {
    const scope = $("#integrity-scope").value;
    setAuditActionVisible(false);
    verifyAuditIntegrity(scope);
  }
  if (activeAuditAction === "evaluation") {
    const scope = $("#evaluation-scope").value;
    const caseIds = Array.from(document.querySelectorAll("[data-evaluation-case]:checked")).map((node) => node.value);
    if (scope === "selected" && !caseIds.length) return toast("请至少勾选一条评测用例");
    setAuditActionVisible(false);
    runEvaluation(scope, caseIds);
  }
  if (activeAuditAction === "skill" && activeSkillRun) {
    const skill = activeSkillRun;
    const input = $("#skill-run-input").value.trim();
    setAuditActionVisible(false);
    executeSkill(skill.name, input);
  }
});
$("#refresh-audit").addEventListener("click", () => { auditOffset = 0; loadAudit(); });
$("#prev-audit").addEventListener("click", () => { auditOffset = Math.max(0, auditOffset - recordPageSize); loadAudit(); });
$("#next-audit").addEventListener("click", () => { auditOffset += recordPageSize; loadAudit(); });
$("#refresh-data").addEventListener("click", () => loadDataExplorer());
$("#data-table-select").addEventListener("change", () => { explorerOffset = 0; loadDataTable(); });
$("#prev-data").addEventListener("click", () => { explorerOffset = Math.max(0, explorerOffset - 30); loadDataTable(); });
$("#next-data").addEventListener("click", () => { explorerOffset += 30; loadDataTable(); });
$("#run-evaluation").addEventListener("click", openEvaluationDialog);
$("#verify-audit").addEventListener("click", openIntegrityDialog);
$("#refresh-knowledge").addEventListener("click", () => { knowledgeOffset = 0; loadKnowledge(); });
$("#knowledge-tag-filter").addEventListener("change", () => { knowledgeOffset = 0; loadKnowledge(); });
$("#knowledge-status-filter").addEventListener("change", () => { knowledgeOffset = 0; loadKnowledge(); });
$("#prev-knowledge").addEventListener("click", () => { knowledgeOffset = Math.max(0, knowledgeOffset - knowledgePageSize); loadKnowledge(); });
$("#next-knowledge").addEventListener("click", () => { knowledgeOffset += knowledgePageSize; loadKnowledge(); });
$("#search-knowledge").addEventListener("click", searchKnowledge);
$("#reindex-knowledge").addEventListener("click", reindexKnowledge);
$("#skill-filter").addEventListener("change", renderSkills);
$("#skill-risk-filter").addEventListener("change", renderSkills);
$("#refresh-skill-history").addEventListener("click", loadSkillHistory);
$("#tool-category-filter").addEventListener("change", renderTools);
$("#tool-risk-filter").addEventListener("change", renderTools);
$("#refresh-tool-history").addEventListener("click", loadToolHistory);
$("#refresh-approvals").addEventListener("click", () => { approvalOffset = 0; loadApprovals(); });
$("#prev-approvals").addEventListener("click", () => { approvalOffset = Math.max(0, approvalOffset - recordPageSize); loadApprovals(); });
$("#next-approvals").addEventListener("click", () => { approvalOffset += recordPageSize; loadApprovals(); });
document.querySelectorAll("[data-approval-status]").forEach((button) => button.addEventListener("click", () => { approvalStatus = button.dataset.approvalStatus || ""; approvalOffset = 0; loadApprovals(); }));
$("#refresh-monitoring").addEventListener("click", loadMonitoring);
$("#search-metrics").addEventListener("click", loadMetricCatalog);
$("#refresh-metrics").addEventListener("click", () => { $("#metric-keyword").value = ""; $("#metric-category").value = ""; loadMetricCatalog(); });
$("#download-metric-template").addEventListener("click", () => { window.location.href = "/api/v1/metrics/import-template"; });
$("#upload-metric-csv").addEventListener("click", importMetricCsv);
$("#analyze-metric").addEventListener("click", () => { if (!selectedMetric) return; document.querySelector('.nav-item[data-page="agent"]').click(); $("#question").value = "查询指标 #" + selectedMetric.id + " 近24小时趋势"; runQuery(); });
$("#role-selector").addEventListener("change", () => { const label = $("#role-selector").selectedOptions[0].textContent; $("#role-label").textContent = label; toast("当前角色已切换为：" + label); loadPolicies(); updateMetricImportAccess(); if ($("#data-page").classList.contains("active-page")) loadDataExplorer(); if ($("#metrics-page").classList.contains("active-page")) loadMetricCatalog(); if ($("#skills-page").classList.contains("active-page")) loadSkills(); if ($("#tools-page").classList.contains("active-page")) loadTools(); });
$("#save-knowledge").addEventListener("click", saveKnowledge);
$("#upload-knowledge").addEventListener("click", uploadKnowledge);
$("#cancel-edit-knowledge").addEventListener("click", cancelKnowledgeEdit);
$("#close-skill-detail").addEventListener("click", () => { $("#skill-detail").hidden = true; });
$("#approve-button").addEventListener("click", () => { if (latestApprovalId) resolveApproval(latestApprovalId, $("#approve-button")); });
$("#show-system-info").addEventListener("click", async () => { try { const metric = await (await fetch("/api/v1/metrics")).json(); toast("LLM：" + (metric.llm_enabled ? "已配置" : "离线规则模式") + "；审批写库：" + (metric.approved_writes_enabled ? "已开启" : "安全关闭")); } catch { toast("无法读取运行配置"); } });
$("#show-help").addEventListener("click", () => toast("可查询数据、查看审批与审计、维护知识库，并查看 Skill/SOP。"));
loadSkills(); loadKnowledgeTags(); loadKnowledge(); loadApprovals(); loadMetrics();

function chatRequestOptions(method, body) {
  return { method, headers:{"Content-Type":"application/json"}, body:JSON.stringify(body) };
}

function renderChatMessages(messages) {
  const target = $("#chat-messages");
  $("#chat-task-plan").textContent = messages.length ? "已加载 " + messages.length + " 条上下文" : "多轮上下文待加载";
  if (!messages.length) { target.innerHTML = '<div class="empty-state"><strong>开始一次 Agent 对话</strong><p>例如：如何处理华东 P1 网关离线？</p></div>'; return; }
  target.innerHTML = messages.map((message) => '<article class="chat-bubble ' + escapeHtml(message.role) + '"><span>' + (message.role === "user" ? "你" : "OpsPilot") + '</span><div>' + escapeHtml(message.content).replace(/\n/g, "<br>") + '</div></article>').join("");
  target.scrollTop = target.scrollHeight;
}

function appendChatBubble(role, content) {
  const target = $("#chat-messages");
  target.querySelector(".empty-state")?.remove();
  const article = document.createElement("article"); article.className = "chat-bubble " + role;
  article.innerHTML = '<span>' + (role === "user" ? "你" : "OpsPilot") + '</span><div>' + escapeHtml(content || "") + '</div>';
  target.appendChild(article); target.scrollTop = target.scrollHeight;
  return article.querySelector("div");
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
  $("#chat-input").value = "";
  appendChatBubble("user", content);
  const assistantTarget = appendChatBubble("assistant", "");
  let assistantText = "";
  try {
    const response = await fetch("/api/v1/chat/conversations/" + encodeURIComponent(chatConversationId) + "/messages/stream", chatRequestOptions("POST", { role:currentRole(), requester:"Lenovo", content }));
    if (!response.ok) { const result = await response.json(); throw new Error(result.detail || "聊天请求失败"); }
    const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ""; let finished = false;
    const consume = (block) => {
      const eventName = (block.match(/^event: ([^\n]+)/m) || ["", "message"])[1];
      const dataLine = block.split("\n").find((line) => line.startsWith("data:")); if (!dataLine) return;
      const data = JSON.parse(dataLine.slice(5).trim());
      if (eventName === "stage") {
        $("#chat-provider").textContent = data.stage === "context" ? "加载上下文" : "任务规划中";
        $("#chat-task-plan").textContent = data.stage === "plan" ? "任务规划：" + data.message + " · " + (data.steps || []).join(" → ") : data.message;
      } else if (eventName === "token") {
        assistantText += data.content || ""; assistantTarget.innerHTML = escapeHtml(assistantText).replace(/\n/g, "<br>"); $("#chat-provider").textContent = data.provider === "local_deepseek" ? "本地 DeepSeek · 流式" : "离线流式";
        $("#chat-messages").scrollTop = $("#chat-messages").scrollHeight;
      } else if (eventName === "done") {
        finished = true; $("#chat-task-plan").textContent = "已完成：" + data.plan.route + " · 已保留上下文";
      } else if (eventName === "error") throw new Error(data.message || "流式聊天失败");
    };
    while (true) { const {value, done} = await reader.read(); if (done) break; buffer += decoder.decode(value, {stream:true}); const blocks = buffer.split("\n\n"); buffer = blocks.pop(); blocks.forEach(consume); }
    if (buffer.trim()) consume(buffer); if (!finished) throw new Error("流式响应未正常结束");
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
    const action = approval.status === "pending" ? '<div class="approval-action"><button class="warning-button" data-approval="' + approval.id + '">批准并进入安全执行</button><button class="secondary-button" data-reject="' + approval.id + '">拒绝</button></div>' : '<div class="approval-action"><button class="secondary-button approval-delete-button" data-delete-approval="' + approval.id + '">删除记录</button></div>';
    const labels = { pending:"待审批", approved:"已批准", approved_safe_mode:"安全模式已批准", executed:"已执行", rejected:"已拒绝", expired:"已过期" };
    return '<article class="approval-item"><div><strong>' + escapeHtml(approval.reason) + '</strong><code>' + escapeHtml(approval.sql) + '</code>' + impact + decision + result + expiry + '<div class="approval-meta">申请人：' + escapeHtml(approval.requester) + ' · ' + new Date(approval.created_at).toLocaleString("zh-CN", {hour12:false}) + '</div>' + action + '</div><span class="approval-status ' + escapeHtml(approval.status) + '">' + (labels[approval.status] || escapeHtml(approval.status)) + '</span></article>';
  }).join("");
  document.querySelectorAll("[data-approval]").forEach((button) => button.addEventListener("click", () => resolveApproval(button.dataset.approval, button)));
  document.querySelectorAll("[data-reject]").forEach((button) => button.addEventListener("click", () => rejectApproval(button.dataset.reject, button)));
  document.querySelectorAll("[data-delete-approval]").forEach((button) => button.addEventListener("click", () => deleteApproval(button.dataset.deleteApproval, button)));
}

async function loadApprovals() {
  try {
    const statusQuery = "&status=" + encodeURIComponent(approvalStatus);
    const [response, summaryResponse] = await Promise.all([fetch("/api/v1/approvals?limit=" + recordPageSize + "&offset=" + approvalOffset + statusQuery), fetch("/api/v1/approvals/summary?status=" + encodeURIComponent(approvalStatus))]);
    const rows = await response.json(); const summary = await summaryResponse.json();
    if (!response.ok || !summaryResponse.ok) throw new Error("审批队列加载失败");
    renderApprovals(rows);
    const labels = { "":"全部", pending:"待审批", rejected:"已拒绝", approved_safe_mode:"安全模式已批准", expired:"已过期" };
    document.querySelectorAll("[data-approval-status]").forEach((button) => { const status = button.dataset.approvalStatus || ""; button.classList.toggle("active", status === approvalStatus); button.textContent = labels[status] + " " + (status ? (summary.status_counts?.[status] || 0) : summary.total); });
    $("#approval-page-note").textContent = "当前筛选：" + labels[approvalStatus] + " · 第 " + (Math.floor(approvalOffset / recordPageSize) + 1) + " 页 · 本页 " + rows.length + " 条 / 共 " + summary.total + " 条";
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

async function deleteApproval(id, button) {
  if (!confirm("确认删除这条已完成的审批记录？审批卡片会移除，但删除行为会永久保留在审计中心。")) return;
  if (button) { button.disabled = true; button.textContent = "删除中…"; }
  try {
    const response = await fetch("/api/v1/approvals/" + id, { method:"DELETE", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ role:currentRole(), actor:"Lenovo" }) }); const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "删除失败");
    toast(result.message); loadApprovals(); loadAudit(); loadMetrics();
  } catch (error) { toast(error.message || "删除失败"); }
  finally { if (button) { button.disabled = false; button.textContent = "删除记录"; } }
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

function renderMonitorMetricTrends(series) {
  const target = $("#monitoring-metric-trends");
  if (!series?.length) { target.innerHTML = '<div class="empty-state"><strong>暂无指标样本</strong><p>可在指标中心导入真实 CSV 后刷新。</p></div>'; return; }
  target.innerHTML = series.map((item) => {
    const values = item.points.map((point) => Number(point.value)).filter(Number.isFinite); const min = Math.min(...values); const max = Math.max(...values); const span = max - min || 1;
    const points = item.points.map((point, index) => `${(index / Math.max(item.points.length - 1, 1) * 100).toFixed(2)},${(92 - ((Number(point.value) - min) / span) * 76).toFixed(2)}`).join(" ");
    const latest = values[values.length - 1];
    return `<div class="monitor-trend-item"><div class="monitor-trend-heading"><strong>${escapeHtml(item.name)}</strong><span>${Number.isFinite(latest) ? latest.toFixed(2) : "—"} ${escapeHtml(item.unit)}</span></div><svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-label="${escapeHtml(item.name)}趋势"><polyline points="${points}"></polyline></svg><small>${item.points.length} 个采样点 · ${escapeHtml(item.source === "csv" ? "真实 CSV" : "本地样本")}</small></div>`;
  }).join("");
}

function renderMonitorAlertTrend(rows) {
  const target = $("#monitoring-alert-trend");
  if (!rows?.length) { target.innerHTML = '<div class="empty-state"><strong>暂无告警趋势样本</strong></div>'; return; }
  const maximum = Math.max(...rows.map((row) => Number(row.count)), 1);
  target.innerHTML = rows.map((row) => `<div class="alert-trend-row"><span>${escapeHtml(row.day)}</span><i><b style="height:${Math.round(Number(row.count) / maximum * 100)}%"></b></i><em>${row.count}</em></div>`).join("");
}

function renderMonitorHealth(checks) {
  const target = $("#monitoring-health");
  const labels = { healthy:"正常", degraded:"降级", down:"异常" };
  target.innerHTML = (checks || []).map((check) => `<div class="health-probe ${escapeHtml(check.status)}"><span>${check.status === "healthy" ? "✓" : "!"}</span><div><strong>${escapeHtml(check.name)}</strong><small>${escapeHtml(labels[check.status] || check.status)} · ${escapeHtml(check.detail || "")}</small></div></div>`).join("");
}

async function loadMonitoring() {
  try {
    const response = await fetch("/api/v1/monitoring/overview"); const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "监控数据加载失败");
    const assetTotal = data.asset_states.reduce((sum, item) => sum + item.count, 0);
    const alertTotal = data.alert_severity.reduce((sum, item) => sum + item.count, 0);
    $("#monitoring-summary").innerHTML = '<article><strong>' + assetTotal + '</strong><small>纳管设备资产</small></article><article><strong>' + alertTotal + '</strong><small>未关闭告警</small></article><article><strong>' + data.open_tickets + '</strong><small>待处理工单</small></article>';
    renderStateRows($("#asset-state-list"), data.asset_states, ["#26ad75", "#e88b42", "#7668ed"]);
    renderStateRows($("#alert-severity-list"), data.alert_severity, ["#ed6d61", "#e99b47", "#796dec"]);
    $("#monitoring-alert-list").innerHTML = renderRows(data.latest_alerts);
    renderMonitorMetricTrends(data.metric_series); renderMonitorAlertTrend(data.alert_trend); renderMonitorHealth(data.health_checks);
    $("#monitoring-source-note").textContent = "来源：" + (data.sample_source || "本地指标样本");
    $("#monitoring-checked-at").textContent = "最近探测：" + new Date().toLocaleTimeString("zh-CN", {hour12:false});
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

function renderTools() {
  const target = $("#tool-list");
  const category = $("#tool-category-filter").value;
  const risk = $("#tool-risk-filter").value;
  const tools = allTools.filter((tool) => (!category || tool.category === category) && (!risk || tool.risk === risk));
  $("#tool-count-note").textContent = "显示 " + tools.length + " / " + allTools.length + " 个工具";
  target.innerHTML = tools.map((tool) => '<article class="card tool-card"><span class="risk ' + escapeHtml(tool.risk) + '">' + escapeHtml(tool.risk.toUpperCase()) + '</span><p class="section-label">' + escapeHtml(tool.category) + '</p><h3>' + escapeHtml(tool.name) + '</h3><p>' + escapeHtml(tool.description) + '</p><button class="text-button" data-tool="' + escapeHtml(tool.name) + '">试运行工具</button></article>').join("");
  document.querySelectorAll("[data-tool]").forEach((button) => button.addEventListener("click", () => invokeTool(button.dataset.tool, button)));
}

function renderToolSummary() {
  const count = (risk) => allTools.filter((tool) => tool.risk === risk).length;
  $("#tool-summary").innerHTML = '<article><strong>' + allTools.length + '</strong><span>已注册能力</span></article><article><strong>' + count("auto") + '</strong><span>AUTO 只读工具</span></article><article><strong>' + count("manual") + '</strong><span>MANUAL 审批工具</span></article><article><strong>' + count("blocked") + '</strong><span>BLOCKED 防护规则</span></article>';
}

function renderToolHistory(rows) {
  $("#tool-history-list").innerHTML = rows.length ? rows.map((row) => '<div><strong>' + escapeHtml(row.payload.tool || "未知工具") + '</strong><span>' + escapeHtml(row.action === "tool_invoked" ? "已完成只读调用" : row.action === "tool_approval_requested" ? "已创建人工审批" : "调用被权限策略拒绝") + '</span><em>' + new Date(row.created_at).toLocaleString("zh-CN", {month:"numeric", day:"numeric", hour:"2-digit", minute:"2-digit", hour12:false}) + '</em></div>').join("") : "<div class='empty-state'><strong>暂未调用过工具</strong><p>从上方选择白名单能力进行试运行。</p></div>";
}

async function loadToolHistory() {
  try {
    const response = await fetch("/api/v1/tools/history?role=" + encodeURIComponent(currentRole())); const rows = await response.json();
    if (!response.ok) throw new Error(rows.detail || "加载失败"); renderToolHistory(rows);
  } catch { $("#tool-history-list").innerHTML = "<div class='empty-state'><strong>无法加载工具调用记录</strong></div>"; }
}

async function loadTools() {
  try {
    const response = await fetch("/api/v1/tools"); allTools = await response.json();
    if (!response.ok) throw new Error("加载失败");
    const categories = [...new Set(allTools.map((tool) => tool.category))];
    $("#tool-category-filter").innerHTML = '<option value="">全部分类</option>' + categories.map((item) => '<option value="' + escapeHtml(item) + '">' + escapeHtml(item) + '</option>').join("");
    renderToolSummary(); renderTools(); loadToolHistory();
  } catch { $("#tool-list").innerHTML = '<div class="empty-state"><strong>工具中心加载失败</strong></div>'; }
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
    loadAudit(); loadToolHistory();
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
