"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  // Appearance is browser-local presentation only; it never changes a task or provider.
  const appearanceKey = "ark-archive.appearance.v1";
  const readingAreas = ["conversation-scroll", "detail-body", "saved-view", "library-view", "tools-view"];
  let rememberReadingLayout = () => {};
  // Keep content in place when spacing or wrapping changes. A raw scrollTop
  // drifts by the accumulated height difference of every preceding answer.
  function captureReadingPosition(area) {
    const viewport = area?.getBoundingClientRect?.();
    if (!viewport?.height) return null;
    const position = {area, top: area.scrollTop, end: area.scrollTop > 0 && area.scrollHeight - area.clientHeight - area.scrollTop <= 2};
    const elements = area.querySelectorAll("p,h1,h2,h3,h4,li,blockquote,summary,button,.user-bubble,.history-turn-label");
    for (const element of elements) {
      const rect = element.getBoundingClientRect();
      if (rect.height && rect.bottom > viewport.top + 12 && rect.top < viewport.bottom) {
        position.anchor = element; position.offset = rect.top - viewport.top; break;
      }
    }
    return position;
  }
  function restoreReadingPosition(position) {
    if (!position?.area.isConnected) return false;
    const {area, anchor, offset} = position;
    if (!area.getBoundingClientRect?.().height) return false;
    if (position.end) area.scrollTop = Math.max(0, area.scrollHeight - area.clientHeight);
    else if (anchor?.isConnected && area.contains(anchor) && anchor.getClientRects().length) {
      area.scrollTop = Math.max(0, area.scrollTop + anchor.getBoundingClientRect().top - area.getBoundingClientRect().top - offset);
    } else area.scrollTop = position.top;
    return true;
  }
  function applyAppearance(value, persist = false) {
    const skin = value === "ark" ? "ark" : "popucom";
    const positions = readingAreas.map(id => captureReadingPosition($(id)));
    $("app-root").dataset.arkTheme = skin;
    $("app-root").dataset.arkDepth = skin === "popucom" ? "maximal" : "moderate";
    // Synchronous layout: no queued restoration can later steal a new scroll
    // gesture or a different conversation. CSS scroll anchoring is disabled on
    // these owned surfaces so it cannot apply the height delta a second time.
    positions.forEach(restoreReadingPosition);
    rememberReadingLayout();
    for (const name of ["popucom", "ark"]) $("skin-" + name).setAttribute("aria-pressed", String(name === skin));
    if (persist) {
      try { localStorage.setItem(appearanceKey, skin); $("appearance-feedback").textContent = "已保存到当前浏览器。"; }
      catch { $("appearance-feedback").textContent = "已切换；浏览器未允许保存，下次打开将使用默认皮肤。"; }
    }
  }
  try { applyAppearance(localStorage.getItem(appearanceKey)); } catch { applyAppearance("popucom"); }
  $("open-appearance").addEventListener("click", () => { closeContextBubble(); $("appearance-dialog").showModal(); });
  $("close-appearance").addEventListener("click", () => $("appearance-dialog").close());
  $("appearance-dialog").addEventListener("close", () => $("open-appearance").focus({preventScroll: true}));
  for (const name of ["popucom", "ark"]) $("skin-" + name).addEventListener("click", () => applyAppearance(name, true));
  globalThis.addEventListener?.("storage", event => {
    if (event.key === appearanceKey) applyAppearance(event.newValue);
    else if (event.key === null) applyAppearance("popucom");
  });
  function newUuid() {
    if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
    // LAN HTTP has getRandomValues but may not expose randomUUID.
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
    return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join("-");
  }
  const historyKey = "ark-archive.sessions.v1";
  const pendingKey = "ark-archive.pending.v1";
  const unresolvedKey = "ark-archive.unresolved.v1";
  const draftsKey = "ark-archive.drafts.v1";
  const savedKey = "ark-archive.saved.v1";
  let accountOverview = null, accountLoading = false, accountTimer = null, hideAmounts = false;
  let accountRevision = 0, accountRefreshQueued = false;
  let credentialInfo = null, credentialBusy = false, credentialConsent = false;
  try { hideAmounts = localStorage.getItem("ark-archive.amounts-hidden.v1") === "true"; } catch { /* In-page preference still works. */ }
  const balanceAlertKey = "ark-archive.balance-alert.v1";
  let balanceAlert = {enabled: true, thresholdCents: 500, snoozedUntil: 0}, balanceAlertVolatile = false;
  try {
    const saved = JSON.parse(localStorage.getItem(balanceAlertKey) || "null");
    if (saved && typeof saved.enabled === "boolean" && Number.isSafeInteger(saved.thresholdCents)
      && saved.thresholdCents >= 1 && saved.thresholdCents <= 1000000) {
      balanceAlert = {enabled: saved.enabled, thresholdCents: saved.thresholdCents,
        snoozedUntil: Number.isFinite(saved.snoozedUntil) && saved.snoozedUntil > Date.now()
          && saved.snoozedUntil <= Date.now() + 3600000 ? saved.snoozedUntil : 0};
    }
  } catch { /* Invalid preferences use the default, without changing chat state. */ }
  let savedReading = null, savedOpenToken = 0, savedListTop = 0;
  const modules = {roguelike: "集成战略", operator: "干员资料", recruitment: "公开招募", garrison: "卫戍协议", auto: "自动识别"};
  // Presentation labels mirror roguelike_sources.THEME_NAMES. They never resolve
  // questions or change the authoritative record identity sent to the server.
  const themeNames = {act12d6: "刻俄柏的灰蕈迷境", rogue_1: "傀影与猩红孤钻", rogue_2: "水月与深蓝之树", rogue_3: "探索者的银凇止境", rogue_4: "萨卡兹的无终奇语", rogue_5: "岁的界园志异", rogue_6: "沉沦者的黑流树海"};
  const recordKinds = {roguelike_theme: "主题", roguelike_buff: "效果配置", roguelike_relic: "收藏品", roguelike_item: "道具", roguelike_squad: "分队", roguelike_ending: "结局", roguelike_node: "节点", roguelike_stage: "关卡", roguelike_zone: "区域", roguelike_event: "事件", roguelike_choice: "选项", roguelike_month_squad: "月度小队", roguelike_challenge: "挑战", roguelike_recruit_group: "招募分组", roguelike_ticket: "票券", roguelike_rule: "规则配置", roguelike_difficulty: "难度", roguelike_initial: "初始配置", roguelike_wiki: "Wiki资料"};
  Object.assign(recordKinds, {operator: "干员", skill: "技能", module: "模组", module_phase: "模组等级", recruitment_tag: "公招标签", recruitment_pool: "公招名单", regulation_bond: "盟约", regulation_band: "开局策略", regulation_mode: "模式", regulation_equipment: "装备", regulation_effect: "效果", regulation_unit: "棋子", regulation_unit_shop: "干员商店", regulation_mission: "目标", regulation_shop_good: "兑换", regulation_season: "赛季", regulation_activity: "活动", regulation_notice: "公告"});
  const statusLabels = {answered: "资料回答", computed: "计算结果", needs_clarification: "等待澄清", needs_verification: "资料待核实", out_of_scope: "超出资料范围", unknown: "资料未记载", refused: "暂无法回答", not_found: "未找到资料", unsupported: "暂不支持", timeout: "请求超时"};
  const state = {release: null, ready: false, modelReady: false, modelAvailability: null, modelChecking: false, historyReadOnly: false, historyIncomplete: false, historyLoading: false, taskPhase: null, busy: false, blocked: false, uncertain: false, pending: null, pendingArchive: null, conversationId: newUuid(), view: "chat", session: null, previous: null, sessions: [], historyQuery: "", library: null, drawerToken: 0};
  let contextCapacity = null, contextSerial = 0;
  let composerModel = null, modelSwitching = false, modelMenuLoading = false, modelMenuSerial = 0, modelMenuError = "";
  let providerReturnToComposer = false;
  let contextView = {key: null, data: null, loading: false, error: false, attempted: false};
  function contextTarget() {
    const binding = state.pending || state.previous?.agent_runtime?.http_task;
    const task = binding?.task_id, conversation = binding?.conversation_id;
    const valid = (v) => typeof v === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(v);
    return {key: `${state.conversationId}:${state.pending ? "pending" : "saved"}:${valid(task) ? task : state.session ? "unrecorded" : "new"}`,
      task: valid(task) && valid(conversation) ? task : null, conversation, historical: !!state.session};
  }
  function tokenCount(value, compact = false) {
    if (!Number.isSafeInteger(value) || value < 0) return "—";
    if (compact && value >= 1024) return `${(value / 1024).toLocaleString("en-US", {maximumFractionDigits: 1})}K`;
    return value.toLocaleString("en-US");
  }
  function closeContextBubble() {
    $("context-bubble").hidden = true;
    $("context-meter").setAttribute("aria-expanded", "false");
  }
  function renderContextMeter() {
    const data = contextView.data, target = contextTarget();
    const fresh = !target.task && !target.historical;
    // Without explanatory text, stale/unknown receipts must not look current.
    const used = fresh ? 0 : !contextView.error && data?.state === "ready" ? data.used_tokens : null;
    const limit = data?.limit_tokens || contextCapacity;
    const known = Number.isSafeInteger(used) && used >= 0;
    const percent = known && Number.isSafeInteger(limit) && limit > 0 ? used / limit * 100 : null;
    const label = contextView.error ? "暂不可读" : fresh ? "尚未开始" : data?.state === "waiting" ? "等待回执" : data?.state === "unknown" ? "用量未确认" : known ? "最近调用" : "尚无回执";
    $("context-ring-fill").style.strokeDashoffset = String(percent === null ? 100 : 100 - Math.min(100, percent));
    $("context-meter").dataset.tone = !known ? "unknown" : percent >= 80 ? "high" : "normal";
    $("context-bubble").textContent = `${tokenCount(used, true)}/${tokenCount(limit, true)}`;
    $("context-meter").setAttribute("aria-label", `上下文用量，${label}，已用 ${tokenCount(used)}，上限 ${tokenCount(limit)} tokens。点击查看用量`);
  }
  async function syncContextUsage(force = false) {
    const target = contextTarget();
    if (target.key !== contextView.key) { closeContextBubble(); contextSerial++; contextView = {key: target.key, data: null, loading: false, error: false, attempted: false}; }
    renderContextMeter();
    if (!target.task || contextView.loading || contextView.attempted && !force || document.hidden) return;
    const serial = ++contextSerial;
    contextView.loading = true; contextView.attempted = true; renderContextMeter();
    try {
      const data = await request(`/v1/agent/context-usage?task_id=${encodeURIComponent(target.task)}&conversation_id=${encodeURIComponent(target.conversation)}`);
      if (serial !== contextSerial || contextTarget().key !== target.key) return;
      if (!data || data.task_id !== target.task || data.conversation_id !== target.conversation
        || !["empty", "ready", "waiting", "unknown", "unavailable"].includes(data.state)
        || !Number.isSafeInteger(data.limit_tokens) || data.limit_tokens <= 0
        || (data.used_tokens !== null && (!Number.isSafeInteger(data.used_tokens) || data.used_tokens < 0
          || !Number.isSafeInteger(data.input_tokens) || data.input_tokens < 0
          || !Number.isSafeInteger(data.output_tokens) || data.output_tokens < 0
          || data.used_tokens !== data.input_tokens + data.output_tokens))
        || (data.state === "ready" && data.used_tokens === null)) throw new Error("invalid context receipt");
      contextView.data = data; contextView.error = false; contextCapacity = data.limit_tokens;
    } catch { if (serial === contextSerial && contextTarget().key === target.key) contextView.error = true; }
    finally { if (serial === contextSerial && contextTarget().key === target.key) { contextView.loading = false; renderContextMeter(); } }
  }
  let draftScope = "new", draftCache = [], draftStorageFailed = false, composing = false, draftRevision = 0;
  let draftParent = null;
  let followLatest = true, unseenContent = null, unseenAnswer = null;
  let automaticScroll = false, scrollGeneration = 0, readingRevision = 0;
  // Reading navigation is transient presentation state. It never replaces the
  // authoritative parent, conversation, pending task, or persisted session.
  const renderedResults = new WeakMap();
  let readingTarget = null, turnSequence = 0, answerSequence = 0, turnIndexSignature = "";
  let visibleTurns = [];
  let turnIndexQuery = "", turnIndexScope = null;
  let sourceReturnContext = null, taskRecovery = null;
  // A portrait/landscape change rewraps all older turns. Retain the last visible
  // anchors before the width changes; do not restore over replacement content.
  let readingWidth = $("app-root").clientWidth, readingLayout = [], readingFrameQueued = false;
  rememberReadingLayout = () => {
    if (readingWidth !== $("app-root").clientWidth) return;
    readingLayout = readingAreas.map(id => captureReadingPosition($(id)));
  };
  for (const id of readingAreas) $(id)?.addEventListener("scroll", () => {
    if (readingFrameQueued) return;
    readingFrameQueued = true;
    requestAnimationFrame(() => { readingFrameQueued = false; rememberReadingLayout(); });
  }, {passive: true});
  globalThis.addEventListener?.("resize", () => {
    const width = $("app-root").clientWidth;
    if (width === readingWidth) return;
    readingWidth = width;
    for (const position of readingLayout) {
      if (position?.anchor?.isConnected) restoreReadingPosition(position);
    }
    rememberReadingLayout();
  });
  try {
    const saved = JSON.parse(localStorage.getItem(historyKey) || "[]");
    if (Array.isArray(saved)) state.sessions = saved.filter((s) => s && typeof s.title === "string" && typeof s.id === "string" && Array.isArray(s.turns)).slice(0, 12);
  } catch { /* Storage may be disabled; chat remains usable. */ }

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = String(text);
    return element;
  }
  function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    svg.setAttribute("aria-hidden", "true"); use.setAttribute("href", `#i-${name}`); svg.append(use); return svg;
  }
  function button(label, action, className = "text-action") {
    const b = node("button", className, label); b.type = "button"; b.addEventListener("click", action); return b;
  }
  function printable(value) { return typeof value === "string" ? value : JSON.stringify(value, null, 2); }
  function displayLabel(labels, value) { return Object.prototype.hasOwnProperty.call(labels, value) ? labels[value] : value; }
  function displayAnswerText(result) {
    const original = typeof result.answer === "string" ? result.answer : "";
    const version = result.version;
    const citations = result.citations;
    const claims = result.claims;
    if (!version || version.release_id !== result.release_id || version.manifest_hash !== result.manifest_hash
      || !Array.isArray(citations) || !citations.length
      || typeof version.source_revision !== "string"
      || !citations.some((c) => c.source_revision === version.source_revision)) return original;
    const hasTool = (name) => Array.isArray(result.tool_trace) && result.tool_trace.some((item) => item.tool === name);
    // Only exact, source-backed server template metadata is omitted from this
    // display copy. The audited result and its original answer are never edited.
    if (result.module === "recruitment" && result.status === "computed" && hasTool("recruitment.calculate")
      && Array.isArray(result.combinations) && Array.isArray(result.sources)) {
      const evidenceIds = new Set(citations.map((c) => c.evidence_id));
      const snapshotIds = new Set(citations.map((c) => c.snapshot_id));
      let lines = original.split("\n");
      for (const combo of result.combinations.slice(0, 3)) {
        if (!Array.isArray(combo.selected_tags) || !Array.isArray(combo.candidates)
          || !Array.isArray(combo.evidence_ids) || !combo.evidence_ids.length
          || !combo.evidence_ids.every((id) => evidenceIds.has(id))) continue;
        const label = combo.selected_tags.join(" + ") || "无标签";
        let outcome = "在标签保留条件下无候选";
        if (combo.candidates.length) {
          let names = combo.candidates.slice(0, 8).map((candidate) => candidate.name).join("、");
          if (combo.candidates.length > 8) names += `等${combo.candidate_count}名干员`;
          outcome = `最低${combo.minimum_rarity_if_tags_retained}星，候选：${names}`;
        }
        const readable = `- ${label}：${outcome}。`;
        const metadataLine = `${readable}证据ID：${combo.evidence_ids.slice(0, 3).join(", ")}。`;
        lines = lines.map((line) => line === metadataLine ? readable : line);
      }
      const versionLine = `版本：CN 来源修订 ${version.source_revision}；知识发布 ${result.release_id}。未自动确认线上生效时间。`;
      lines = lines.map((line) => line === versionLine ? "未自动确认线上生效时间。" : line);
      if (result.sources.length && result.sources.every((source) => snapshotIds.has(source.snapshot_id)
        && typeof source.source_key === "string" && typeof (source.page_url || source.url) === "string")) {
        const sourcesLine = "来源：" + result.sources.map((source) => `[${source.source_key}](${source.page_url || source.url})（修订 ${source.revision}）`).join("；") + "。";
        lines = lines.filter((line) => line !== sourcesLine);
      }
      const fieldsLine = "完整组合、每位候选与规则证据见 combinations、citations 和 calculation_inputs。";
      if (result.calculation_inputs && result.combination_count === result.combinations.length) lines = lines.filter((line) => line !== fieldsLine);
      return lines.join("\n");
    }
    const citationIds = citations.map((c) => c.id);
    if (!Array.isArray(claims) || !claims.length || !citationIds.every((id) => typeof id === "string")
      || !claims.every((claim) => Array.isArray(claim.citation_ids) && claim.citation_ids.every((id) => citationIds.includes(id)))) return original;
    const evidenceLine = "证据：" + citationIds.map((id) => `[${id}]`).join("、");
    if (result.module === "operator" && result.status === "answered" && hasTool("verify_field_evidence")) {
      const footer = `\n\n版本：${result.release_id}；来源修订：${version.source_revision}。\n${evidenceLine}`;
      return original.endsWith(footer) ? original.slice(0, -footer.length) : original;
    }
    const garrisonTools = ["garrison_documented_mechanics_gate", "garrison_cross_checked_field_gate", "garrison_verified_field_gate"];
    if (result.module === "garrison" && result.status === "answered" && garrisonTools.some(hasTool)) {
      const lines = original.split("\n");
      const versionLine = `版本：${result.release_id}；历史配置修订：${version.source_revision}。`;
      if (lines.at(-1) !== evidenceLine || lines.at(-3) !== versionLine) return original;
      const sourceLine = lines.at(-2);
      if (!sourceLine.startsWith("来源：") || !sourceLine.endsWith("。")) return original;
      const allowedUrls = new Set(citations.flatMap((c) => [c.source_url, c.page_url].filter(Boolean)));
      for (const citation of citations) {
        try {
          if (new URL(citation.source_url).hostname === "prts.wiki" && /^\d+$/.test(String(citation.source_revision)))
            allowedUrls.add(`https://prts.wiki/index.php?oldid=${citation.source_revision}`);
        } catch { /* An unrecognized source remains in the original display. */ }
      }
      const links = sourceLine.slice("来源：".length, -1).split("；");
      if (!links.length || !links.every((link) => {
        const match = /^\[[^\]\n]+\]\((https?:\/\/[^\s)]+)\)$/.exec(link);
        return match && allowedUrls.has(match[1]);
      })) return original;
      return lines.slice(0, -3).join("\n");
    }
    return original;
  }
  function garrisonReadingPresentation(result, text) {
    const version = result.version, scope = result.scope;
    // These are source phase labels from ingestion/garrison.ANNOUNCEMENTS,
    // scoped to this retained activity. They are not client version numbers.
    const phases = {V2_1: "初始阶段", V2_2: "第二阶段"};
    const phase = version?.phase_id;
    const citations = result.citations, claims = result.claims;
    if (result.module !== "garrison" || result.status !== "answered"
      || !Object.prototype.hasOwnProperty.call(phases, phase)
      || scope?.activity_id !== "act2autochess" || version?.activity_id !== scope.activity_id
      || !Array.isArray(scope.verified_phase_ids) || !scope.verified_phase_ids.includes(phase)
      || scope.period_label !== "盟约下半期活动"
      || typeof result.release_id !== "string" || !result.release_id || version.release_id !== result.release_id
      || typeof result.manifest_hash !== "string" || !result.manifest_hash || version.manifest_hash !== result.manifest_hash
      || typeof version.source_revision !== "string" || !version.source_revision
      || !Array.isArray(citations) || !citations.length
      || !citations.some(c => c.source_revision === version.source_revision)
      || !Array.isArray(claims) || !claims.length) return null;
    const citationIds = new Set(citations.map(c => c.id));
    if (!claims.every(claim => Array.isArray(claim.citation_ids) && claim.citation_ids.length
      && claim.citation_ids.every(id => citationIds.has(id)))) return null;
    const gates = ["garrison_documented_mechanics_gate", "garrison_cross_checked_field_gate", "garrison_verified_field_gate"];
    if (!Array.isArray(result.tool_trace) || !result.tool_trace.some(item => gates.includes(item.tool)
      && item.phase_id === phase && item.source_revision === version.source_revision)) return null;
    const prefix = `${phase}，`;
    if (!text.startsWith(prefix)) return null;
    let body = text.slice(prefix.length), note = "";
    const lines = body.split("\n");
    // Move only exact server-template scope metadata; keep every other word.
    const scopePrefixes = ["范围：act2autochess 盟约下半期。", "范围：act2autochess 盟约下半期的配置及固定原始描述。"];
    if (lines.length === 2) {
      const range = scopePrefixes.find(value => lines[1].startsWith(value));
      if (range && lines[1].length > range.length) { body = lines[0]; note = lines[1].slice(range.length); }
    }
    const label = `${scope.period_label} · ${phases[phase]}`;
    return {body, note, label, phase, activity: scope.activity_id,
      revision: version.source_revision,
      limitations: Array.isArray(result.limitations) ? result.limitations.filter(value => typeof value === "string") : [],
      copyText: `本次查询采用：${label}（历史资料）\n\n${body}${note ? `\n\n${note}` : ""}`};
  }
  function answerContext(presentation) {
    const context = node("div", "answer-context");
    const scope = node("div", "answer-context-scope");
    scope.append(node("span", "answer-context-label", "本次查询采用"), node("strong", "answer-context-value", presentation.label));
    context.append(scope, node("span", "answer-context-kind", "历史资料"));
    return context;
  }
  function answerProvenance(presentation) {
    const details = node("details", "answer-provenance");
    details.append(node("summary", null, "资料标识与完整说明"));
    const content = node("div", "answer-provenance-body");
    const list = node("dl", "answer-provenance-values");
    for (const [label, value] of [["资料阶段标识", presentation.phase], ["活动标识", presentation.activity], ["配置修订", presentation.revision]]) {
      const row = node("div"); row.append(node("dt", null, label), node("dd", null, value)); list.append(row);
    }
    content.append(list, node("p", "answer-provenance-note", "阶段标识用于追溯来源，不代表客户端版本。"));
    if (presentation.limitations.length) {
      const limits = node("ul", "answer-provenance-limits");
      presentation.limitations.forEach(value => limits.append(node("li", null, value))); content.append(limits);
    }
    details.append(content); return details;
  }
  const sourceTextFields = [
    ["name", "名称"], ["title", "标题"], ["desc", "效果说明"],
    ["description", "说明"], ["effectText", "效果"], ["unlockText", "解锁条件"],
    ["text", "原文"],
  ];
  function structuredValue(value) { return value !== null && typeof value === "object"; }
  function sourceHighlights(value) {
    if (!structuredValue(value) || Array.isArray(value)) return [];
    const rows = [];
    const seen = new Set();
    for (const [field, label] of sourceTextFields) {
      const text = value[field];
      if (typeof text !== "string" || !text.trim() || seen.has(text)) continue;
      seen.add(text);
      rows.push({label, text: sourceText(text)});
    }
    return rows;
  }
  function appendAnswerInline(target, text, emphasis = true) {
    // Keep emphasis within the prose, including older unformatted answers
    // whose verified values already have a readable game unit.
    const pattern = emphasis
      ? /\[([^\]\n]{1,100})\]\((https?:\/\/[^\s)]+)\)|\*\*([^*\n]+)\*\*/g
      : /\[([^\]\n]{1,100})\]\((https?:\/\/[^\s)]+)\)/g;
    const matches = [...text.matchAll(pattern)];
    const plain = text.replace(pattern, (token, label, url, marked) => marked ? "" : label);
    const size = value => [...value.matchAll(/[\p{L}\p{N}]/gu)].length;
    const markedSize = matches.reduce((sum, match) => sum + (match[3] ? size(match[3]) : 0), 0);
    const localized = markedSize > 0 && size(plain) >= Math.max(1, markedSize / 3);
    let highlights = 0;
    const appendText = (value, fallback) => {
      if (!fallback || /[<>{}`]|:\/\//.test(value)) {
        target.append(document.createTextNode(value));
        return;
      }
      const quantities = /(?<![\w.\-–—~～])(?:\d+(?:\.\d+)?\s*(?:至|到|[-–—~～])\s*)?\d+(?:\.\d+)?[ \t]*(?:[%％]|个百分点|名(?:不同)?干员|名|人|层|回合|分钟|秒|小时|星|级|次)(?![\d.])/g;
      let start = 0;
      for (const match of value.matchAll(quantities)) {
        if (highlights >= 2) break;
        if (size(match[0]) > size(value) / 2) continue;
        target.append(document.createTextNode(value.slice(start, match.index)), node("strong", "answer-emphasis", match[0]));
        start = match.index + match[0].length;
        highlights++;
      }
      target.append(document.createTextNode(value.slice(start)));
    };
    const fallback = emphasis && !localized;
    let offset = 0;
    for (const match of matches) {
      appendText(text.slice(offset, match.index), fallback);
      if (match[3]) {
        if (localized && highlights < 3 && match[3].length <= 40 && !/[。！？!?；;]/.test(match[3])) {
          target.append(node("strong", "answer-emphasis", match[3]));
          highlights++;
        } else appendText(match[3], fallback);
      } else {
        const link = node("a", null, match[1]);
        link.href = match[2]; link.target = "_blank"; link.rel = "noopener noreferrer";
        target.append(link);
      }
      offset = match.index + match[0].length;
    }
    appendText(text.slice(offset), fallback);
  }
  function answerDataBlock(lines, index) {
    const line = lines[index].trim();
    const fenced = /^\x60{3}(?:json)?\s*$/i.test(line);
    if (!fenced && !line.startsWith("{") && !line.startsWith("[")) return null;
    let end = index;
    if (fenced) {
      end += 2;
      while (end < lines.length && !/^\s*\x60{3}\s*$/.test(lines[end])) end += 2;
      if (end >= lines.length) return null;
    } else if (line === "{" || line === "[") {
      const close = line === "{" ? "}" : "]";
      end += 2;
      while (end < lines.length && lines[end].trim() !== close) end += 2;
      if (end >= lines.length) return null;
    }
    const raw = lines.slice(fenced ? index + 2 : index, fenced ? end : end + 1).join("").trim();
    try {
      const value = JSON.parse(raw);
      return structuredValue(value) ? {value, end, raw} : null;
    } catch { return null; }
  }
  function citationMarkers(result) {
    return new Set(answerArray(result.citations).map(citation => citation?.id)
      .filter(id => typeof id === "string" && /^(?:q\d+_)?c\d+$/i.test(id)).map(id => id.toLowerCase()));
  }
  function withoutCitationMarkers(line, ids) {
    if (!ids?.size) return line;
    // Only remove IDs belonging to this answer, in citation syntax. Retain
    // identifiers in facts, Markdown links, inline code, and raw data.
    const markers = /(?:\[([\t ]*(?:q\d+_)?c\d+[\t ]*)\](?![ \t]*\()|【([\t ]*(?:q\d+_)?c\d+[\t ]*)】|〔([\t ]*(?:q\d+_)?c\d+[\t ]*)〕)/gi;
    function removeMarkers(value) {
      return value.replace(markers, (token, a, b, c) => ids.has((a || b || c).trim().toLowerCase()) ? "" : token);
    }
    const label = /^\s*(?:[-*•]\s+)?(?:\*\*)?(?:证据|引用|来源)(?:编号|ID)?(?:\*\*)?\s*[：:]\s*(?:\*\*)?/i;
    const separators = /^[\s、，,；;。．.／/]+$/;
    const rawRefs = value => value.replace(/[`]?\b(?:q\d+_)?c\d+\b[`]?/gi, token => ids.has(token.replaceAll("`", "").toLowerCase()) ? "" : token);
    const refsOnly = (value, loose = false) => {
      const reduced = removeMarkers(value);
      const rest = loose ? rawRefs(reduced) : reduced;
      return rest !== value && (!rest || separators.test(rest));
    };
    if (label.test(line) && refsOnly(line.replace(label, "").replace(/\*\*$/, ""), true)) return "";
    if (refsOnly(line)) return "";
    // The last non-citation segment owns punctuation; adjacent reference
    // marks and their separators are a unit.
    const group = /(?:[ \t]*)(?:(?:\[[ \t]*(?:q\d+_)?c\d+[ \t]*\](?![ \t]*\()|【[ \t]*(?:q\d+_)?c\d+[ \t]*】|〔[ \t]*(?:q\d+_)?c\d+[ \t]*〕)(?:[ \t]*[、，,；;／/][ \t]*|[ \t]*(?=[\[【〔]))?)+/gi;
    return line.split(/(`+[^`]*`+|\[[^\]\n]+\]\(https?:\/\/[^\s)]+\))/g)
      .map((part, index) => index % 2 ? part : part.replace(group, value => {
        // Keep unknown references and any punctuation that connects text.
        if (!refsOnly(value)) return removeMarkers(value);
        return /[、，,；;/／][ \t]*$/.exec(value)?.[0] || "";
      })).join("");
  }
  function answerContent(text, {format = true, markerIds = null} = {}) {
    const content = node("div", "answer-text answer-reading");
    const lines = String(text).split(/(\r\n|\r|\n)/);
    let first = true, list = null;
    for (let index = 0; index < lines.length; index += 2) {
      const separator = lines[index + 1] || "";
      const data = format && answerDataBlock(lines, index);
      const line = data ? lines[index] : withoutCitationMarkers(lines[index], markerIds);
      if (!data && !line && lines[index]) continue;
      const bullet = format && /^\s{0,3}(?:(\d{1,3})(?:[.)]\s+|、\s*)|[-*•·]\s+)(.+)$/.exec(line);
      if (data) {
        list = null;
        const highlights = sourceHighlights(data.value);
        if (highlights.length) {
          const excerpt = node("div", "answer-data-excerpt");
          appendSourceHighlights(excerpt, highlights);
          content.append(excerpt);
        }
        content.append(answerDisclosure("查看原始数据", node("pre", "detail-json", data.raw)));
        index = data.end;
      } else if (bullet) {
        const kind = bullet[1] ? "ol" : "ul";
        if (!list || list.tagName.toLowerCase() !== kind) {
          list = node(kind, "answer-list");
          if (bullet[1]) list.setAttribute("start", bullet[1]);
          content.append(list);
        }
        const item = node("li");
        if (bullet[1]) item.setAttribute("value", bullet[1]);
        appendAnswerInline(item, bullet[2]); list.append(item); first = false;
        continue;
      } else {
        list = null;
        if (line.trim()) {
          const title = format && /^\s{0,3}#{1,5}\s+(.+?)\s*#*\s*$/.exec(line);
          const block = title ? node("h3", "answer-section-title")
            : node("p", `answer-paragraph${first ? " answer-lead" : ""}`);
          appendAnswerInline(block, title ? title[1] : line, format);
          content.append(block); first = false;
        } else if (line) content.append(document.createTextNode(line));
      }
      if (!data && separator) content.append(document.createTextNode(separator));
      else if (data && lines[index + 1]) content.append(document.createTextNode(lines[index + 1]));
    }
    return content;
  }
  function copyReadingText(text, markerIds) {
    if (!markerIds.size) return text;
    const lines = text.split(/(\r\n|\r|\n)/);
    let output = "";
    for (let index = 0; index < lines.length; index += 2) {
      const data = answerDataBlock(lines, index);
      if (data) {
        output += lines.slice(index, data.end + 2).join("");
        index = data.end;
      } else {
        const line = withoutCitationMarkers(lines[index], markerIds);
        if (line || !lines[index]) output += line + (lines[index + 1] || "");
      }
    }
    return output === text ? text : output.trimEnd();
  }
  function appendSourceHighlights(container, rows) {
    const summary = node("dl", "readable-source-fields");
    for (const {label, text} of rows) {
      const row = node("div");
      row.append(node("dt", null, label), node("dd", null, text));
      summary.append(row);
    }
    container.append(summary);
  }
  function citationValue(citation) {
    return Object.prototype.hasOwnProperty.call(citation, "field_value") ? citation.field_value : citation.quote;
  }
  function citationSite(citation) {
    const aliases = {"prts.wiki": "PRTS Wiki", "ak.hypergryph.com": "明日方舟官网", "raw.githubusercontent.com": "GitHub"};
    for (const value of [citation.source_url, citation.page_url]) {
      try {
        const href = new URL(value);
        if (["http:", "https:"].includes(href.protocol)) return Object.hasOwn(aliases, href.hostname) ? aliases[href.hostname] : href.hostname;
      } catch { /* A citation without a URL still has a place in the list. */ }
    }
    return "";
  }
  function citationFact(citation, result) {
    // Join by the returned citation identity, never by question wording or value similarity.
    const claims = answerArray(result?.claims);
    const bound = claims.filter(c => answerArray(c.citation_ids).includes(citation.id));
    const fact = bound.find(c => typeof c.label === "string" && c.label.trim());
    if (!fact) return null;
    const labels = {"duration 原始字段": "持续时间", "干员技能槽关联": "技能关联", "技能原始描述黑板": "技能参数"};
    const label = (labels[fact.label] || fact.label).replace(/^V\d+_\d+\s*/, "");
    const levelIndex = /\/levels\/(\d+)(?:\/|$)/.exec(citation.pointer || "")?.[1];
    const citations = answerArray(result?.citations);
    const related = claims.filter(c => c.record_id === fact.record_id && answerArray(c.citation_ids).some(id => {
      const ref = citations.find(item => item.id === id);
      return ref?.snapshot_id === citation.snapshot_id && (levelIndex === undefined || ref?.pointer?.startsWith(`${citation.pointer.split("/levels/")[0]}/levels/${levelIndex}/`) || c.derived?.source_index === Number(levelIndex));
    }));
    const names = related.filter(c => /^(技能名称|干员名称|名称)$/.test(c.label)).map(c => readableFact(c.value)).filter(Boolean);
    const levels = related.filter(c => c.label === "技能等级");
    const level = levels.length === 1 ? levels[0] : null;
    const context = [...new Set(names), level?.derived?.display || (level ? `等级 ${readableFact(level.value)}` : "")].filter(Boolean).join(" · ");
    const text = readableFact(fact.derived?.display || fact.value);
    const technical = /关联|黑板|参数/.test(label) || /[{}]|<@/.test(text) || /^\w+_[\w_]+$/.test(text);
    return {label, context, text: technical ? "" : sourceText(text), technical};
  }
  function citationTitle(citation, index, result = null) {
    const fact = citationFact(citation, result);
    if (fact) return [fact.context, fact.label].filter(Boolean).join(" · ");
    const name = sourceHighlights(citationValue(citation)).find(row => row.label === "名称" || row.label === "标题")?.text;
    const fallback = index >= 0 ? `来源 ${index + 1}` : "来源详情";
    const title = [name, citation.page_title, citation.source_title, citation.title].find(value =>
      typeof value === "string" && value.trim() && !/^(?:q\d+_)?c\d+$/i.test(value.trim()));
    return title?.trim() || fallback;
  }
  // Presentation adapters project verified response fields into the same groups
  // and cards. This layer never parses the user's question or changes the answer.
  const answerArray = value => Array.isArray(value) ? value : [];
  const answerText = value => typeof value === "string" && value.trim() ? value : "";
  const readableFact = value => typeof value === "string" ? answerText(value) :
    typeof value === "number" || typeof value === "boolean" ? String(value) : "";
  function citationIndex(result) {
    const all = answerArray(result.citations);
    const ids = new Map(all.filter(item => item.id).map(item => [item.id, item]));
    const key = item => item?.snapshot_id && typeof item.pointer === "string" && item.field_hash
      ? JSON.stringify([item.snapshot_id, item.pointer, item.field_hash]) : null;
    const byOrigin = new Map(all.map(item => [key(item), item]).filter(([origin]) => origin));
    const byEvidence = new Map();
    for (const item of all) {
      if (!item.evidence_id) continue;
      if (!byEvidence.has(item.evidence_id)) byEvidence.set(item.evidence_id, []);
      byEvidence.get(item.evidence_id).push(item);
    }
    return {
      byIds: values => [...new Set(answerArray(values).map(id => ids.get(id)).filter(Boolean))],
      byEvidence: values => [...new Set(answerArray(values).flatMap(id => byEvidence.get(id) || []))],
      fromSource: items => [...new Set(answerArray(items).map(item => byOrigin.get(key(item))).filter(Boolean))],
    };
  }
  function answerCatalog(source, references) {
    const catalog = source.catalog;
    // Legacy field adapters describe only the shape of the verified payload.
    // Neither the component nor the selection logic has a season or topic ID.
    const rows = catalog?.items || catalog?.members || source.bond_catalog;
    if (!Array.isArray(rows) || !rows.length || rows.some(row => !answerText(row?.name))) return null;
    const claims = answerArray(source.claims);
    const sourceCitations = answerArray(source.citations);
    const local = references || citationIndex(source);
    const cards = rows.map(row => {
      const namedClaims = claims.filter(claim => claim.record_id && (
        claim.value === row.name && answerArray(claim.citation_ids).some(id =>
          sourceCitations.some(citation => citation.id === id && citation.pointer?.endsWith("/name")))
        || claim.value?.name === row.name && claim.value.rarity_stars === row.rarity_stars
      ));
      const recordId = new Set(namedClaims.map(claim => claim.record_id));
      const bound = recordId.size === 1 ? claims.filter(claim => recordId.has(claim.record_id)) : [];
      const matching = new Set([row.citation_id, ...bound.flatMap(claim => answerArray(claim.citation_ids))].filter(Boolean));
      const refs = sourceCitations.filter(citation => matching.has(citation.id) || recordId.size === 1 && recordId.has(citation.record_id));
      const fields = [];
      if (Number.isInteger(row.rarity_stars)) fields.push({label: "星级", value: `${row.rarity_stars} 星`});
      if (Array.isArray(row.tag_names) && row.tag_names.length) fields.push({label: "标签", value: row.tag_names.join("、")});
      if (!fields.length && bound.length) {
        const values = new Map();
        for (const claim of bound) {
          const value = readableFact(claim.value);
          const own = sourceCitations.filter(citation => answerArray(claim.citation_ids).includes(citation.id));
          if (value && value !== row.name && answerText(claim.label) && own.some(citation => !/Id(?:\/|$)/.test(citation.pointer || ""))) {
            values.set(claim.label, value);
          }
        }
        for (const [label, value] of values) fields.push({label, value});
      }
      return {
        title: row.name, fields, refs: references ? local.fromSource(refs) : refs,
        meta: activationLabel(row.activation_scope, row.activation_count),
        text: sourceText(row.description),
      };
    });
    if (references && !cards.every(card => card.refs.length)) return null;
    const complete = !catalog?.has_more && (!catalog || catalog.complete_for_published_scope === true);
    const total = Number.isInteger(catalog?.total_count) ? catalog.total_count : cards.length;
    return {
      title: answerText(source.scope?.title) || "资料目录", kind: "catalog", cards,
      summary: complete ? `共 ${cards.length} 条` : `本页 ${cards.length} 条 · 共 ${total} 条`,
      pagination: references ? null : catalog,
    };
  }
  function activationLabel(scope, count) {
    const activation = scope === "BOARD" ? "在场" : scope === "BOARD_AND_DECK" ? "场上及整备区" : "";
    return activation && Number.isInteger(count) && count > 0 ? `${activation} · ${count} 人` : "";
  }
  function sourceText(value) {
    return answerText(value).replace(/<@(?:ba|autochess)\.[\w.]+>|<\/>/g, "").replace(/\\n/g, "\n");
  }
  function catalogVisibleInAnalysis(cards, sections) {
    return cards.every(card => sections.some(section => section.text.includes(card.title)
      && card.refs.some(ref => answerArray(section.citation_ids).includes(ref.id))));
  }
  function catalogFromVerifiedSources(result, index) {
    const entries = Object.entries(result.verified_query_sources || {});
    if (!entries.length) return null;
    const sections = answerArray(result.model_analysis);
    if (!sections.length || sections.some(section => !answerText(section?.text) || !Array.isArray(section.evidence_refs))) return null;
    const explicit = entries.filter(([, entry]) => entry?.answer?.catalog || Array.isArray(entry?.answer?.bond_catalog));
    // A catalog can lead a composed answer when the model used it throughout.
    // A directory pulled only to support a narrower answer remains in its sources.
    if (explicit.length === 1) {
      const [[ref, {answer}]] = explicit;
      if (sections.some(section => section?.evidence_refs?.length === 1 && section.evidence_refs[0] === ref)
        && sections.every(section => answerArray(section?.evidence_refs).includes(ref))) {
        const group = answerCatalog(answer, index);
        if (group && catalogVisibleInAnalysis(group.cards, sections)) {
          return {...group, title: `来源目录 · ${group.title}`};
        }
      }
    }
    if (explicit.length) return null;
    // Historical composed catalogs did not carry rows. Only recover a complete
    // set of named records from explicitly referenced queries and their parent
    // citations; counts, types, and scope must agree for the entire set.
    const referenced = new Set(sections.flatMap(section => answerArray(section.evidence_refs)));
    const types = new Set(entries.filter(([ref]) => referenced.has(ref)).map(([, item]) => item.query?.regulation_kind).filter(Boolean));
    if (types.size !== 1) return null;
    const [type] = types;
    const packages = entries.filter(([ref, entry]) => referenced.has(ref) && entry.query?.regulation_kind === type);
    if (!packages.length) return null;
    const published = packages[0][1].answer;
    const count = published.coverage?.record_counts?.[type];
    const title = answerText(published.scope?.title);
    if (!title || !Number.isInteger(count) || count < 2 || packages.some(([, item]) =>
      item.answer.scope?.title !== title || item.answer.coverage?.record_counts?.[type] !== count)) return null;
    const cards = new Map();
    for (const [, {answer}] of packages) {
      const local = citationIndex(answer);
      for (const claim of answerArray(answer.claims)) {
        const value = claim.value;
        if (!answerText(value?.name) || !answerText(value.desc)) continue;
        const refs = index.fromSource(local.byIds(claim.citation_ids)).filter(citation =>
          citation.field_value?.name === value.name && citation.field_value.desc === value.desc
          && citation.field_value.activeCount === value.activeCount && citation.field_value.activeCondition === value.activeCondition);
        for (const citation of refs) cards.set(JSON.stringify([citation.snapshot_id, citation.pointer]), {
          title: value.name, text: sourceText(value.desc), refs: [citation],
          meta: activationLabel(value.activeCondition, value.activeCount),
        });
      }
    }
    return cards.size === count && catalogVisibleInAnalysis([...cards.values()], sections)
      ? {title: `来源目录 · ${title}`, summary: `共 ${count} 条`, kind: "catalog", cards: [...cards.values()]} : null;
  }
  function compareAnswerFacts(result, index) {
    const proposal = result.query_interpretation?.proposal;
    if (proposal?.compare !== true && !answerArray(result.tool_trace).some(step => step.plan?.compare === true)) return null;
    const groups = new Map();
    for (const claim of answerArray(result.claims)) {
      const refs = index.byIds(claim.citation_ids);
      const value = readableFact(claim.value);
      if (!claim.record_id || !value || !answerText(claim.label) || !refs.length) continue;
      if (!groups.has(claim.record_id)) groups.set(claim.record_id, {title: "", fields: [], refs: new Set()});
      const group = groups.get(claim.record_id);
      if (refs.some(citation => citation.pointer?.endsWith("/name") && citation.field_value === claim.value)
          && typeof claim.value === "string") {
        if (group.title !== null) group.title = group.title && group.title !== value ? null : value;
        refs.forEach(ref => group.refs.add(ref));
      } else if (!refs.some(citation => /Id(?:\/|$)/.test(citation.pointer || ""))) {
        group.fields.push({label: claim.label, value, refs});
      }
    }
    const named = [...groups.values()].filter(card => card.title && card.fields.length);
    if (named.length < 2) return null;
    const shared = new Set(named[0].fields.map(field => field.label));
    for (const card of named.slice(1)) for (const label of shared) {
      if (!card.fields.some(field => field.label === label)) shared.delete(label);
    }
    if (!shared.size) return null;
    const cards = named.map(card => {
      const fields = card.fields.filter(field => shared.has(field.label));
      return {title: card.title, fields, refs: [...new Set([...card.refs, ...fields.flatMap(field => field.refs)])]};
    });
    return {title: "资料对照", kind: "comparison", cards};
  }
  function combinationCards(result, index) {
    const combinations = answerArray(result.recruitment_display?.combinations || result.combinations);
    if (!combinations.length) return null;
    const cards = combinations.map(row => {
      const minimum = row.minimum_rarity_if_tags_retained;
      const count = row.candidate_count;
      const fields = [];
      if (minimum === null) fields.push({label: "标签保留时", value: "没有候选"});
      if (Number.isInteger(minimum)) fields.push({label: "标签保留时最低", value: `${minimum} 星`});
      if (Number.isInteger(count)) fields.push({label: "候选人数", value: `${count} 名`});
      if (row.special_tags_protected_at_this_duration === true) fields.push({label: "稀有标签", value: "该时长受保护"});
      return {
        title: answerArray(row.selected_tags).join(" + ") || "无标签", fields,
        meta: answerText(row.condition), refs: index.byEvidence(row.evidence_ids),
        detail: answerArray(row.candidates).length ? {
          label: "查看候选干员",
          text: row.candidates.map(candidate => `${candidate.name}（${candidate.rarity_stars} 星）`).join("、"),
        } : null,
      };
    });
    const minutes = result.input?.duration_minutes;
    const duration = Number.isInteger(minutes) && minutes > 0 ? `招募时长 ${minutes} 分钟。` : "";
    return {
      title: "公招结果", summary: `共 ${cards.length} 个组合`, kind: "comparison", cards,
      notice: duration + (result.recruitment_display?.scope === "exact_selected_tags" ? "仅显示所选标签同时保留时的交集。" : ""),
    };
  }
  function answerChoices(result) {
    const groups = [];
    const candidates = answerArray(result.candidates).filter(candidate => answerText(candidate?.question));
    if (candidates.length) groups.push({
      title: "选择具体场景", summary: `${candidates.length} 个候选`, kind: "choice",
      cards: candidates.map(candidate => {
        const title = answerText(candidate.name) || "未命名场景";
        return {
          title, number: candidate.number, meta: "候选场景", text: answerText(candidate.description),
          action: {label: "选择并继续查询", run: () => send(candidate.question, result, `查看场景 ${candidate.number}：「${title}」`)},
        };
      }),
    });
    const options = answerArray(result.event_menu?.options);
    if (options.length) groups.push({
      title: "查看选项条件与奖励", summary: `${options.length} 个选项`, kind: "choice",
      cards: options.map((option, index) => ({
        title: answerText(option.label) || `选项 ${index + 1}`, number: index + 1, meta: "事件选项",
        text: structuredValue(option.condition) ? "查看选项详情可核对条件。"
          : option.condition != null && option.condition !== "" ? `条件：${printable(option.condition)}` : "",
        action: {label: "查询条件与奖励", className: "menu-choice", run: () => send(`第${index + 1}个按钮的条件和奖励是什么？`, result)},
      })),
    });
    return groups;
  }
  function makeAnswerLayout(result, bodyText, hasCandidates, presentation) {
    const index = citationIndex(result);
    const choices = answerChoices(result);
    if (hasCandidates || presentation || !["answered", "partial", "computed"].includes(result.status)) return {choices};
    const entries = answerArray(result.model_analysis);
    const analysis = entries.length && entries.every(section => answerText(section?.text)) ? entries : [];
    const primary = result.status !== "partial" && (answerCatalog(result) || catalogFromVerifiedSources(result, index)
      || combinationCards(result, index) || (!analysis.length && compareAnswerFacts(result, index)));
    if (primary) {
      // Keep a brief model introduction above a result grid when provided.
      // In every case the exact full answer stays available for the same turn.
      const lead = analysis[0]?.text;
      return {groups: [primary], choices, original: true,
        summary: lead && lead.length <= 220 && !/\r|\n/.test(lead)
          ? {text: lead, refs: index.byIds(analysis[0].citation_ids)} : null};
    }
    if (analysis.length) {
      const sectionText = analysis.map(card => card.text).join("\n\n");
      const limitations = answerArray(result.limitations);
      const composedText = sectionText + (limitations.length ? `\n\n尚未核实：${limitations.join("；")}` : "");
      return {prose: sectionText, choices, original: bodyText !== sectionText && bodyText !== composedText};
    }
    return {choices};
  }
  function answerDisclosure(label, content) {
    const details = node("details", "detail-disclosure answer-disclosure");
    details.append(node("summary", null, label), content); return details;
  }
  const workbenchTools = new Set([
    "query_records", "read_evidence", "rag_search", "analysis_search", "analysis_read",
    "read_source_query", "source_query", "roguelike_query",
  ]);
  const publicWebTools = new Set(["web_search", "public_wiki_read"]);
  const successfulQueryStates = new Set(["ok", "source_verified_pending_semantic_review", "source_verified_and_delivered"]);

  function verificationSources(values) {
    const seen = new Set();
    return answerArray(values).filter(source => {
      if (typeof source?.title !== "string" || !source.title.trim()) return false;
      const key = JSON.stringify([source.title.trim(), source.site || ""]);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    }).slice(0, 8);
  }
  function renderVerificationSources(sources) {
    return sources.map(source => {
      const row = node("li", null, source.title);
      if (typeof source.site === "string" && source.site) row.append(node("small", null, source.site));
      return row;
    });
  }
  const verificationStepLabels = {
    planning: "确定查询范围", searching: "查询资料", writing: "整理资料",
    reviewing: "独立复核", revising: "根据复核修订", saving: "保存已复核的回答",
  };
  const verificationOutcomes = {
    found: "已找到资料", no_match: "本次未找到匹配项", lookup_failed: "本次读取未完成",
    needs_clarification: "当前范围待确认", partial: "找到部分资料", lookup_done: "读取完成",
  };
  function verificationSteps(values) {
    return answerArray(values).filter(step => step && Object.hasOwn(verificationStepLabels, step.phase)).slice(-12);
  }
  function verificationScope(step) {
    return [...new Set([step.module, ...answerArray(step.targets).slice(0, 3)])]
      .filter(value => typeof value === "string" && value.trim()).join(" · ");
  }
  function verificationOutcome(step) {
    if (step.phase === "reviewing" && Number.isInteger(step.count) && step.count > 0) return `复核 ${step.count} 条引用`;
    if (!Object.hasOwn(verificationOutcomes, step.outcome)) return "";
    if (Number.isInteger(step.count) && step.count > 0) {
      return step.outcome === "partial" ? `找到 ${step.count} 条资料，部分内容待核实` : `返回 ${step.count} 条资料`;
    }
    return verificationOutcomes[step.outcome];
  }
  function verificationStepRow(step) {
    const row = node("li", "verification-step");
    row.append(node("strong", null, step.activity || verificationStepLabels[step.phase]));
    const outcome = verificationOutcome(step);
    if (outcome) row.append(node("span", "verification-step-detail", outcome));
    const scope = verificationScope(step);
    if (scope) row.append(node("div", "verification-step-scope", scope));
    return row;
  }
  function answerVerification(result) {
    const runtime = result.agent_runtime || {};
    const steps = answerArray(runtime.steps);
    const successful = steps.filter(step => successfulQueryStates.has(step?.status));
    const localReads = successful.filter(step => workbenchTools.has(step.tool)).length;
    const webReads = successful.filter(step => publicWebTools.has(step.tool)).length;
    const foundSources = verificationSources(runtime.public_source_candidates);
    const citations = answerArray(result.citations);
    const reviews = answerArray(runtime.semantic_reviews);
    const publicSteps = verificationSteps(runtime.public_verification_steps);
    const revisions = reviews.filter(review => review?.decision === "revise").length;
    const accepted = runtime.server_committed === true && (reviews.length
      ? reviews.at(-1)?.decision === "accept"
      : steps.some(step => step?.status === "semantic_accepted_and_delivered"));
    if (!publicSteps.length && !localReads && !webReads && !citations.length && !accepted && !foundSources.length) return null;

    const list = node("ol", "verification-steps");
    function add(title, detail, action = null) {
      const row = node("li", "verification-step");
      row.append(node("strong", null, title));
      if (detail) row.append(node("span", "verification-step-detail", detail));
      if (action) row.append(action);
      list.append(row);
    }
    const candidates = foundSources.length ? node("ul", "verification-candidates") : null;
    if (candidates) {
      candidates.setAttribute("aria-label", "查到的候选来源");
      candidates.append(...renderVerificationSources(foundSources));
    }
    if (publicSteps.length) list.append(...publicSteps.filter(step => step.phase !== "saving").map(verificationStepRow));
    else {
      if (localReads) add("查询资料库", `${localReads} 次`);
      if (webReads) add("检索公开网页", `${webReads} 次`);
    }
    if (candidates) add("候选来源", "", candidates);
    if (citations.length) {
      const action = button("查看引用", () => openSources(result, {origin: action}), "source-button verification-source");
      add("采用资料", `${citations.length} 条`, action);
    }
    if (accepted) add("独立复核通过", revisions ? `修订 ${revisions} 次` : "");
    const disclosure = node("details", "detail-disclosure answer-verification");
    disclosure.append(node("summary", null, "查证过程"), list);
    return disclosure;
  }
  function cardSource(refs, result, label) {
    const control = button(refs.length === 1 ? "查看来源" : `查看来源 · ${refs.length}`, () => refs.length === 1
      ? openCitation(refs[0], result, reading()) : openSources(result, reading()), "answer-card-source text-action");
    const reading = () => ({sourceQuery: "", sourceKind: "all", listScrollTop: 0, focusIndex: null, citationSubset: refs, origin: control});
    if (label) control.setAttribute("aria-label", `查看「${label}」的来源（${refs.length} 条）`);
    control.prepend(icon("source")); return control;
  }
  function renderAnswerCard(card, group, result) {
    if (card.action) {
      const choice = button("", card.action.run, `answer-card candidate-choice choice-card ${card.action.className || ""}`);
      const top = node("span", "choice-card-top"); top.append(node("span", "choice-number", String(card.number).padStart(2, "0")), node("span", "choice-card-kind", card.meta));
      const body = node("span", "choice-body"); body.append(node("strong", "choice-title", card.title));
      if (card.text) body.append(node("span", "choice-description", card.text));
      const action = node("span", "choice-card-action"); action.append(node("span", null, card.action.label), icon("arrow"));
      choice.append(top, body, action); return choice;
    }
    const item = node("article", `answer-card answer-card--${group.kind}`);
    if (card.title || card.meta) {
      const header = node("div", "answer-card-heading");
      if (card.title) header.append(node(group.title ? "h4" : "h3", null, card.title));
      if (card.meta) header.append(node("span", "answer-card-meta", card.meta));
      item.append(header);
    }
    if (card.fields?.length) {
      const fields = node("dl", "answer-card-fields");
      for (const {label, value} of card.fields) fields.append(node("dt", null, label), node("dd", null, value));
      item.append(fields);
    }
    const markerIds = citationMarkers(result);
    if (card.text) {
      if (group.compact && card.text.length > 160) {
        item.append(node("p", "answer-card-preview", card.text.slice(0, 110) + "…"));
        item.append(answerDisclosure("完整内容与条件", answerContent(card.text, {markerIds})));
      } else item.append(answerContent(card.text, {markerIds}));
    }
    if (card.detail) item.append(answerDisclosure(card.detail.label, answerContent(card.detail.text, {markerIds})));
    if (card.refs?.length) item.append(cardSource(card.refs, result, card.title));
    return item;
  }
  function renderAnswerGroup(group, result) {
    const section = node(group.title ? "section" : "div", `answer-card-group answer-card-group--${group.kind}`);
    if (group.title) {
      const heading = node("div", "answer-card-group-heading");
      heading.append(node("h3", null, group.title));
      if (group.summary) heading.append(node("span", null, group.summary));
      section.append(heading);
    }
    if (group.notice) section.append(node("p", "answer-card-notice", group.notice));
    const grid = node("div", "answer-card-grid");
    const fold = group.cards.length > 5 && group.kind !== "choice";
    const displayGroup = {...group, compact: fold && group.kind === "catalog"};
    group.cards.slice(0, fold ? 3 : group.cards.length).forEach(card => grid.append(renderAnswerCard(card, displayGroup, result)));
    section.append(grid);
    if (fold) {
      const more = node("details", "detail-disclosure answer-group-more");
      const rest = node("div", "answer-card-grid");
      more.append(node("summary", null, `展开其余 ${group.cards.length - 3} 项`), rest);
      // Build once, only on demand. Native details preserves state across source reading.
      more.addEventListener("toggle", () => {
        if (more.open && !rest.children.length) group.cards.slice(3).forEach(card => rest.append(renderAnswerCard(card, displayGroup, result)));
      });
      section.append(more);
    }
    return section;
  }
  function short(value) { return String(value || "").slice(0, 12); }
  function apiPath(path, releaseId, params = {}) {
    const query = new URLSearchParams({...params, ...(releaseId ? {release_id: releaseId} : {})});
    return `${path}${query.size ? `?${query}` : ""}`;
  }
  async function request(path, options) {
    // No transport-level POST retries: a disconnected request may still be running.
    const controller = new AbortController();
    let expired = false, timer;
    const timeoutError = () => {
      const error = new Error("读取服务响应超时，请检查连接后重新读取原记录。");
      error.code = "client_timeout"; error.uncertain = true; return error;
    };
    const deadline = new Promise((_, reject) => {
      timer = setTimeout(() => { expired = true; controller.abort(); reject(timeoutError()); }, path === "/v1/account/providers" ? 65000 : 15000);
    });
    try { return await Promise.race([read(), deadline]); }
    catch (error) {
      if (error.uncertain || error.httpStatus === 503) {
        $("connection").dataset.state = "error";
        $("connection-label").textContent = "连接待恢复";
      }
      throw error;
    }
    finally { clearTimeout(timer); }
    async function read() {
      let response;
      try { response = await fetch(path, {...options, signal: controller.signal, cache: "no-store"}); }
      catch { if (expired) throw timeoutError(); const error = new Error("连接中断，无法确认这次请求的最终结果。请先检查服务；页面没有自动重发。"); error.uncertain = true; throw error; }
      let data;
      try { data = await response.json(); }
      catch { if (expired) throw timeoutError(); throw new Error(`服务返回了无法读取的内容（HTTP ${response.status}）。`); }
      if (!response.ok) {
        const messages = {409: "查询上下文或资料版本不一致，请另起话题后再试。", 422: "输入未通过服务端校验，请检查问题长度及查询模块。", 404: "这条记录不在当前资料版本中。", 503: "资料服务暂不可用，请检查本地服务。", 504: "请求已超时，最终结果尚未确认。页面没有自动重发，请先查看服务状态。"};
        const error = new Error(messages[response.status] || `查询未完成（HTTP ${response.status}）。请检查服务后再操作。`);
        if (path === "/v1/account/credential") {
          const codes = ["credential_owner_required", "credential_storage_unavailable", "credential_invalid_token", "credential_invalid_request", "credential_check_failed", "credential_management_unavailable"];
          error.code = codes.includes(data.detail) ? data.detail : "credential_request_failed";
        }
        if (path.startsWith("/v1/account/providers") && typeof data.detail === "string" && /^(provider_|credential_owner_required$)/.test(data.detail)) error.code = data.detail;
        error.httpStatus = response.status;
        const preflightReasons = ["runtime_policy_unavailable","runtime_profile_unavailable","live_disabled","request_inflight","campaign_frozen","stage40_request_limit_reached","campaign_request_limit_reached","proof_refresh_in_progress","proof_refresh_failed","price_source_changed","price_verification_expired","price_verification_missing","price_verification_invalid","price_verification_in_future","price_not_verified","live_authorization_unverified","configuration_missing","budget_path_mismatch"];
        error.definitelyRejected = response.status === 503 && typeof data.detail === "string" && preflightReasons.includes(data.detail);
        error.uncertain = response.status >= 500; throw error;
      }
      return data;
    }
  }
  function toast(text) {
    $("toast").textContent = text; $("toast").hidden = false;
    clearTimeout(toast.timer); toast.timer = setTimeout(() => { $("toast").hidden = true; }, 3200);
  }
  function renderCredential() {
    const labels = {environment: "已配置 · 环境配置", encrypted: "已配置 · 加密保存", disabled: "已移除本机配置", missing: "尚未配置", unavailable: "加密配置暂不可读取"};
    $("credential-status").textContent = credentialInfo ? labels[credentialInfo.source] || "配置状态暂不可用" : "配置状态暂不可用";
    $("credential-access-note").textContent = credentialInfo?.can_manage
      ? "请在部署电脑的本机页面管理 Token。手机局域网页面不接收密钥。"
      : "当前页面不能修改密钥。请在部署电脑打开 http://127.0.0.1:8000/；其他启动方式需启用密钥管理。";
    $("credential-understand").disabled = !credentialInfo?.can_manage || credentialBusy;
    $("credential-input").disabled = credentialBusy || !credentialInfo?.can_manage || !credentialConsent;
    $("credential-save").disabled = credentialBusy || !credentialInfo?.can_manage || !credentialConsent;
    $("credential-save").textContent = credentialBusy ? "正在处理…" : "校验并加密保存";
    $("credential-existing").textContent = credentialInfo?.configured
      ? `当前 Token：••••••••。${credentialInfo.source === "environment" ? "原值来自环境配置；新保存的 Token 将优先使用。" : "已加密保存，填写新 Token 可更换。"}`
      : "尚无可用 Token，填写后保存。";
    $("credential-remove-section").hidden = !credentialInfo?.configured;
    $("credential-remove").disabled = credentialBusy || !credentialConsent || !credentialInfo?.can_manage || !$("credential-remove-confirm").checked;
  }
  function credentialFeedback(text, error = false) {
    $("credential-feedback").textContent = text; $("credential-feedback").dataset.error = String(error);
  }
  async function refreshCredentials(showSetup = false) {
    try {
      const data = await request("/v1/account/credential", {redirect: "error", credentials: "same-origin", mode: "same-origin"});
      if (!data || typeof data.configured !== "boolean" || typeof data.can_manage !== "boolean") throw new Error();
      credentialInfo = data; renderCredential();
      if (showSetup && state.view === "chat" && !state.pending && !state.busy && !state.session
          && !$("question").value && !document.querySelector("dialog[open]")
          && (!document.activeElement || document.activeElement === document.body)) {
        if (data.tour_pending) startTour();
        else if (data.setup_required && (!data.can_manage || data.tour_pending === undefined)) openCredential();
      }
      return true;
    } catch {
      credentialInfo = null; renderCredential(); return false;
    }
  }
  function openCredential() {
    if ($("credential-dialog").open) { renderCredential(); return; }
    credentialConsent = false;
    $("credential-input").value = ""; $("credential-remove-confirm").checked = false;
    $("credential-title").textContent = "先了解密钥安全";
    $("credential-warning").hidden = false; $("credential-editor").hidden = true;
    $("credential-recheck").hidden = false; credentialFeedback("");
    if ($("account-dialog").open) $("account-dialog").close();
    renderCredential(); $("credential-dialog").showModal(); $("close-credential").focus();
  }
  async function changeCredential(action) {
    if (credentialBusy || !credentialConsent || !credentialInfo?.can_manage) return;
    if (action === "remove" && !$("credential-remove-confirm").checked) return;
    let token = action === "save" ? $("credential-input").value.trim() : null;
    if (action === "save" && (!token || token.length < 16 || token.length > 4096 || /[^\x21-\x7e]/.test(token))) {
      credentialFeedback("请填写有效的 API Token，不要输入说明文字或空格。", true); return;
    }
    credentialBusy = true; $("credential-input").value = "";
    credentialFeedback(action === "save" ? "正在核验并加密保存…" : "正在移除本机配置…"); renderCredential();
    try {
      const pending = request("/v1/account/credential", {method: "POST", redirect: "error", credentials: "same-origin", mode: "same-origin", referrerPolicy: "same-origin",
        headers: {"Content-Type": "application/json", "X-Credential-Nonce": credentialInfo.nonce || ""},
        body: JSON.stringify({action, token, acknowledged: true})});
      token = null;
      const data = await pending;
      if (!data || typeof data.configured !== "boolean") { const error = new Error(); error.uncertain = true; throw error; }
      credentialInfo = data;
      $("credential-remove-confirm").checked = false;
      credentialFeedback(action === "save" ? "已加密保存。后续新请求使用此 Token；模型状态会单独核验。" : "本机配置已移除，历史记录与挂账保留。");
      refreshAccount(true); refreshModelReadiness();
    } catch (error) {
      const messages = {credential_owner_required: "此操作仅限部署电脑本机页面，请刷新配置状态后重试。", credential_storage_unavailable: "加密存储不可用，未保存；请检查部署目录权限和主密钥。", credential_check_failed: "Token 未通过余额接口核验，或服务商暂不可达。原配置未替换。", credential_invalid_token: "Token 格式无效，原配置未替换。", credential_management_unavailable: "当前启动方式未启用密钥管理。"};
      credentialFeedback(error.uncertain ? "保存结果尚未确认。请刷新配置状态，不要重复提交；页面未自动重试。" : messages[error.code] || "配置未保存，请检查输入后重试。", true);
    } finally { token = null; credentialBusy = false; renderCredential(); }
  }
  function accountMoney(value, detailed = false) {
    if (hideAmounts) return "••••";
    if (typeof value !== "string" || !/^\d+(\.\d+)?$/.test(value) || !Number.isFinite(Number(value))) return "—";
    const amount = Number(value);
    if (detailed && amount > 0 && amount < .0001) return "< ¥0.0001";
    return `¥${amount.toLocaleString("zh-CN", {minimumFractionDigits: 2, maximumFractionDigits: detailed ? 4 : 2})}`;
  }
  function saveBalanceAlert() {
    try { localStorage.setItem(balanceAlertKey, JSON.stringify(balanceAlert)); balanceAlertVolatile = false; }
    catch { balanceAlertVolatile = true; }
  }
  function renderBalanceAlert() {
    const account = accountOverview?.account, raw = account?.balance?.total_balance_cny;
    const amount = typeof raw === "string" && /^\d+(\.\d+)?$/.test(raw) ? Number(raw) : NaN;
    const age = Date.now() - Date.parse(account?.checked_at || "");
    const fresh = account?.state === "ready" && Number.isFinite(amount) && Number.isFinite(age) && age >= -60000 && age <= 120000;
    const low = fresh && balanceAlert.enabled && amount < balanceAlert.thresholdCents / 100;
    if (fresh && !low && balanceAlert.snoozedUntil) { balanceAlert.snoozedUntil = 0; saveBalanceAlert(); }
    const snoozed = balanceAlert.snoozedUntil > Date.now();
    $("open-account").dataset.low = String(low);
    $("balance-alert").hidden = !low || snoozed;
    const message = "余额偏低，请留意账户余量。";
    if ($("balance-alert-text").textContent !== message) $("balance-alert-text").textContent = message;
    $("balance-alert-enabled").checked = balanceAlert.enabled;
    $("balance-alert-threshold").disabled = !balanceAlert.enabled;
    $("balance-alert-save").disabled = !balanceAlert.enabled;
    $("balance-alert-resume").hidden = !balanceAlert.enabled || !snoozed;
    $("balance-alert-state").textContent = !balanceAlert.enabled ? "已关闭" : snoozed ? "提醒已暂缓" : low ? "余额偏低" : "已开启";
    $("balance-alert-summary").textContent = !balanceAlert.enabled ? "不会显示低余额提示。"
      : snoozed ? `将在 ${new Date(balanceAlert.snoozedUntil).toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit", hour12: false})} 后恢复提醒。`
      : `低于 ${accountMoney((balanceAlert.thresholdCents / 100).toFixed(2))} 时在页面提醒。`;
    $("balance-alert-storage").hidden = !balanceAlertVolatile;
    return low;
  }
  function renderAccount() {
    const service = ({deepseek:"DeepSeek",openai:"OpenAI",anthropic:"Anthropic",compatible:"通用兼容"})[accountOverview?.provider] || "服务账户";
    $("account-provider-title").textContent = `${service} · 可用余额`;
    $("account-balance-section").setAttribute("aria-label", `${service} 账户余额`);
    const account = accountOverview?.account, balance = account?.balance;
    const stale = account?.state === "stale";
    const unsupported = account?.state === "unsupported";
    const officialOpenAI = unsupported && accountOverview?.provider === "openai";
    $("account-provider-title").textContent = `${service} · ${unsupported ? "账户" : "可用余额"}`;
    $("account-trigger-label").hidden = unsupported;
    $("account-total").hidden = unsupported; $("account-breakdown").hidden = unsupported;
    $("account-checked").hidden = unsupported;
    $("account-billing-link").hidden = !officialOpenAI;
    $("balance-alert-settings").hidden = unsupported;
    const low = renderBalanceAlert();
    $("account-chip").textContent = balance ? `${accountMoney(balance.total_balance_cny)}${stale ? " · 旧" : low ? " · 低" : ""}` : unsupported ? "用量" : "—";
    $("open-account").dataset.state = stale ? "stale" : account?.state || "loading";
    $("open-account").title = stale ? "上次读取的余额，点击查看详情" : "查看账户余额与本机用量";
    for (const [id, value] of [["account-total", balance?.total_balance_cny], ["account-topup", balance?.topped_up_balance_cny], ["account-granted", balance?.granted_balance_cny]]) $(id).textContent = value === undefined ? "—" : accountMoney(value, true);
    $("account-privacy").textContent = hideAmounts ? "显示金额" : "隐去金额";
    $("account-privacy").setAttribute("aria-pressed", String(hideAmounts));
    $("account-refresh").disabled = accountLoading;
    $("account-refresh").textContent = accountLoading ? "正在刷新…" : account?.state === "unsupported" ? "刷新用量" : "刷新余额";
    const messages = {provider_balance_unsupported: "此服务未提供统一余额接口；下方保留本机用量记录。", account_configuration_missing: "尚未配置账户密钥。", account_invalid_configuration: "账户配置暂不可用。", account_timeout: "余额查询超时。", account_http_error: "服务商暂未返回余额。", account_currency_missing: "当前账户未返回人民币余额。"};
    $("account-message").textContent = unsupported ? officialOpenAI ? "余额请到 OpenAI 账单页查看，下方为本机用量。" : "此服务未提供统一余额接口，下方显示本机用量。" : account?.error ? `${messages[account.error] || "暂时无法读取余额。"}${balance ? " 显示上次结果，稍后可重试。" : " 可稍后刷新。"}`
      : balance?.is_available === false ? "账户余额不足，充值后可刷新查看。" : accountLoading && !balance ? "正在读取账户…" : "包含充值余额与未过期赠送余额";
    $("account-message").dataset.state = !unsupported && (account?.error || balance?.is_available === false) ? "warning" : "ready";
    const time = account?.checked_at ? new Date(account.checked_at) : null;
    $("account-checked").textContent = time && Number.isFinite(time.valueOf()) ? `更新于 ${time.toLocaleString("zh-CN", {hour12: false})}` : "尚未成功读取";
    const usage = accountOverview?.usage, ready = usage?.state === "ready";
    $("account-usage-message").hidden = ready;
    $("account-usage-message").textContent = unsupported ? "本机用量暂不可读取，请稍后刷新。" : "本机用量暂不可读取，余额查询不受影响。";
    for (const [id, key] of [["usage-today", "web_requests_today"], ["usage-total", "web_requests_total"], ["usage-unknown", "unknown_count"], ["usage-tokens", "settled_tokens_total"]]) $(id).textContent = ready && Number.isSafeInteger(usage[key]) ? usage[key].toLocaleString("zh-CN") : "—";
    for (const [id, key] of [["usage-cost-today", "web_accounted_today_cny"], ["usage-cost-total", "web_accounted_total_cny"], ["usage-held", "held_cny"]]) $(id).textContent = ready ? accountMoney(usage[key], true) : "—";
    $("provider-usage").textContent = ready ? Object.entries(usage.configured_provider_usage || {}).map(([name, item]) =>
      `${({deepseek:"DeepSeek",openai:"OpenAI",anthropic:"Anthropic",compatible:"通用兼容"})[name] || "其他服务"} · ${item.requests} 次 · 计提 ${accountMoney(item.accounted_cny)} · 挂账 ${accountMoney(item.held_cny)}`).join("；") : "";
    const days = ready && Array.isArray(usage.days) ? usage.days.slice(-7) : [];
    const maximum = Math.max(1, ...days.map(day => Number.isSafeInteger(day.requests) && day.requests >= 0 ? day.requests : 0));
    $("account-days").replaceChildren(...days.map(day => {
      const count = Number.isSafeInteger(day.requests) && day.requests >= 0 ? day.requests : 0;
      const date = typeof day.date === "string" ? day.date : "";
      const item = node("li", "account-day"), bar = node("span", "account-day-bar");
      item.setAttribute("aria-label", `${date}，${count} 次调用`); bar.setAttribute("aria-hidden", "true");
      bar.style.height = `${count ? Math.max(3, count / maximum * 64) : 1}px`;
      item.append(node("span", "account-day-count", String(count)), bar, node("span", "account-day-date", date.slice(5))); return item;
    }));
  }
  async function refreshAccount(force = false) {
    if (document.hidden) return;
    if (accountLoading) return;
    const revision = accountRevision;
    clearTimeout(accountTimer); accountLoading = true; renderAccount();
    try {
      const data = await request(`/v1/account/overview${force ? "?refresh=true" : ""}`);
      if (!data?.account || !["ready", "stale", "unavailable", "unsupported"].includes(data.account.state)) throw new Error("invalid account response");
      if (revision === accountRevision) accountOverview = data;
    } catch {
      if (revision === accountRevision) accountOverview = {provider: accountOverview?.provider, account: {...accountOverview?.account, state: accountOverview?.account?.balance ? "stale" : "unavailable", error: "account_request_error"}, usage: {state: "unavailable"}};
    } finally {
      accountLoading = false; renderAccount();
      if (accountRefreshQueued) { accountRefreshQueued = false; refreshAccount(true); }
      else if (!document.hidden) accountTimer = setTimeout(() => refreshAccount(), 60000);
    }
  }
  function saveSessions() {
    try { localStorage.setItem(historyKey, JSON.stringify(state.sessions)); }
    catch { toast("本机存储不可用，本次对话仍可继续。"); }
    renderHistory();
  }
  function readDrafts() {
    if (draftStorageFailed) return draftCache;
    try {
      const saved = JSON.parse(localStorage.getItem(draftsKey) || "[]");
      if (Array.isArray(saved)) draftCache = saved.filter((row) => row && typeof row.scope === "string" && row.scope.length <= 180
        && typeof row.text === "string" && row.text.length <= 2000 && Object.prototype.hasOwnProperty.call(modules, row.module)).slice(0, 24);
    } catch { /* Keep the in-page draft when persistent storage cannot be read. */ }
    return draftCache;
  }
  function saveDraft() {
    const text = $("question").value;
    if (!text) draftParent = null;
    const rows = readDrafts().filter((row) => row.scope !== draftScope);
    if (text) rows.unshift({scope: draftScope, text, module: $("module").value, ...(draftParent ? {exploration: draftParent} : {})});
    draftCache = rows.slice(0, 24);
    try { localStorage.setItem(draftsKey, JSON.stringify(draftCache)); draftStorageFailed = false; }
    catch { draftStorageFailed = true; }
    updateDraftStatus();
  }
  function updateDraftStatus() {
    $("draft-status").textContent = $("question").value ? draftStorageFailed ? "草稿暂存于本页" : "草稿已保存在本机" : "";
  }
  function restoreDraft(scope, module = "auto") {
    draftScope = scope;
    const row = readDrafts().find((item) => item.scope === scope);
    $("question").value = row?.text || "";
    draftParent = row?.text ? normalizeDraftParent(row.exploration) : null;
    $("module").value = scope === "new" ? row?.module || module : module;
    updateDraftStatus();
  }
  function normalizeDraftParent(value) {
    if (value === undefined || value === null) return null;
    const root = value.kind === "restored" && value.run_id === null;
    const keys = root ? ["release_id", "conversation_id"] : ["run_id", "release_id", "conversation_id"];
    if (!keys.every(key => typeof value?.[key] === "string" && value[key].length > 0 && value[key].length <= 180)) return {invalid: true};
    return {...Object.fromEntries(keys.map(key => [key, value[key]])), ...(root ? {run_id: null} : {}), ...(value.kind === "restored" ? {kind: "restored"} : {})};
  }
  function boundDraftLabel() { return draftParent?.kind === "restored" ? "恢复草稿" : "探索草稿"; }
  function independentDraft() {
    return draftParent?.kind === "restored" && draftParent.run_id === null
      && (draftParent.conversation_id === state.conversationId || (!state.session && draftScope === "new"));
  }
  function explorationDraftRow() {
    if (!draftParent || draftParent.conversation_id !== state.conversationId) return null;
    return conversationTurns().find(row => row.result?.run_id === draftParent.run_id && row.result?.release_id === draftParent.release_id
      && isModelRun(row.result) && row.result.agent_runtime?.server_committed === true) || null;
  }
  function fillDraft(question, module = $("module").value, valid = () => true, parent = null) {
    const apply = () => {
      if (!valid()) { toast("对话已变化，请从最新回答重新选择。"); return; }
      if (state.busy || state.pending || state.blocked) { toast("请先确认当前任务的结果。"); return; }
      if (!state.session && Object.prototype.hasOwnProperty.call(modules, module)) $("module").value = module;
      switchView("chat"); $("question").value = question; draftParent = normalizeDraftParent(parent); saveDraft(); updateComposer(); $("question").focus();
    };
    if ($("question").value.trim() && $("question").value !== question) {
      const scope = draftScope;
      openDetail("替换当前草稿？");
      const actions = node("div", "draft-choice-actions");
      actions.append(button("使用这个问题", () => { $("detail-dialog").close(); if (scope === draftScope) apply(); }, "text-action"),
        button("保留当前草稿", () => { $("detail-dialog").close(); $("question").focus(); }, "text-action"));
      $("detail-body").append(node("p", "detail-description", "当前输入还没有发送。选择要继续编辑的内容。"),
        node("h3", "detail-subhead", "当前草稿"), node("blockquote", "source-quote", $("question").value),
        node("h3", "detail-subhead", "准备填入"), node("blockquote", "source-quote", question),
        actions);
    } else apply();
  }
  function renderHistory() {
    const container = $("history-list"); container.replaceChildren();
    if ($("history-count")) $("history-count").textContent = String(state.sessions.length).padStart(2, "0");
    if (!state.sessions.length) { container.append(node("p", "history-empty", "暂无对话")); renderUnresolvedHistory(container); return; }
    const query = state.historyQuery.trim().toLocaleLowerCase();
    const sessions = state.sessions.filter((session) => !query || `${session.title} ${modules[session.module] || ""}`.toLocaleLowerCase().includes(query));
    if (!sessions.length) { container.append(node("p", "history-empty", "没有匹配的对话")); renderUnresolvedHistory(container); return; }
    for (const session of sessions) {
      const b = button(session.title, () => restoreSession(session), "history-item");
      b.title = session.title;
      if (state.session?.id === session.id) { b.classList.add("active"); b.setAttribute("aria-current", "true"); }
      b.disabled = state.busy && !state.historyLoading || !state.ready || !!state.pending || state.blocked; container.append(b);
    }
    renderUnresolvedHistory(container);
  }
  // Saved items contain only reading metadata and immutable run references.
  // Opening an item always reads the original audited answer, never a new model run.
  function readSaved() {
    const rows = JSON.parse(localStorage.getItem(savedKey) || "[]");
    if (!Array.isArray(rows)) throw new Error("收藏记录无法读取");
    return rows.filter(row => row && typeof row.run_id === "string" && row.run_id.length <= 180
      && typeof row.release_id === "string" && row.release_id.length <= 180
      && typeof row.question === "string" && typeof row.preview === "string"
      && row.question.length <= 2000 && row.preview.length <= 500
      && typeof row.saved_at === "string" && Object.prototype.hasOwnProperty.call(modules, row.module));
  }
  function sameSaved(a, b) { return a.run_id === b.run_id && a.release_id === b.release_id; }
  function syncSavedButtons() {
    let rows;
    try { rows = readSaved(); $("saved-count").textContent = String(rows.length); }
    catch { $("saved-count").textContent = "—"; }
    for (const control of document.querySelectorAll(".answer-save")) {
      const present = rows?.some(row => row.run_id === control.dataset.runId && row.release_id === control.dataset.releaseId);
      control.textContent = present ? "已收藏" : "收藏";
      control.prepend(icon("bookmark")); control.setAttribute("aria-pressed", String(!!present));
    }
  }
  function writeSaved(rows) {
    if (rows.length > 200) { toast("已收藏 200 条，请先移出不需要的回答。"); return false; }
    try { localStorage.setItem(savedKey, JSON.stringify(rows)); }
    catch { toast("收藏未保存：本机存储不可用或空间不足。"); return false; }
    syncSavedButtons(); return true;
  }
  function toggleSaved(result) {
    let rows;
    try { rows = readSaved(); } catch { toast("收藏记录暂时无法读取，原记录保持不动。"); return; }
    const existing = rows.find(row => sameSaved(row, result));
    if (existing) {
      if (writeSaved(rows.filter(row => !sameSaved(row, result)))) { renderSavedLibrary(); toast("已移出收藏。"); }
      return;
    }
    const context = answerFlowContext(result);
    if (!answerFlowCurrent(context)) return;
    const row = {run_id: result.run_id, release_id: result.release_id, module: modules[result.module] ? result.module : "auto",
      question: context.question.slice(0, 2000), preview: answerContent(displayAnswerText(result), {markerIds: citationMarkers(result)}).textContent.slice(0, 500), saved_at: new Date().toISOString()};
    if (writeSaved([row, ...rows])) { renderSavedLibrary(); toast("已加入我的档案。"); }
  }
  function renderSavedLibrary() {
    const list = $("saved-list"), feedback = $("saved-feedback"); list.replaceChildren(); feedback.replaceChildren();
    let rows;
    try { rows = readSaved(); }
    catch {
      feedback.append(node("span", null, "暂时无法读取收藏，原记录没有改动。"), button("重新读取", renderSavedLibrary));
      syncSavedButtons(); return;
    }
    const query = $("saved-search").value.trim().toLocaleLowerCase(), module = $("saved-module").value;
    const filtered = rows.filter(row => (module === "all" || row.module === module)
      && (!query || `${row.question} ${row.preview}`.toLocaleLowerCase().includes(query)));
    feedback.textContent = rows.length ? `${filtered.length} 条收藏` : "";
    if (!filtered.length) {
      const empty = node("div", "saved-empty");
      empty.append(icon("bookmark"), node("h2", null, rows.length ? "没有匹配的收藏" : "你的下一条好线索，留在这里"),
        node("p", null, rows.length ? "试试其他关键词，或切换到全部模块。" : "在回答下方点「收藏」，不用再翻长对话。"));
      empty.append(button(rows.length ? "清除筛选" : "去问一个问题", () => {
        if (rows.length) { $("saved-search").value = ""; $("saved-module").value = "all"; renderSavedLibrary(); }
        else { switchView("chat"); $("question").focus(); }
      }, "answer-followup")); list.append(empty);
    }
    for (const row of filtered) {
      const card = node("article", "saved-card"); card.dataset.module = row.module;
      const open = button("", () => openSaved(row), "saved-card-open");
      open.append(node("span", "saved-card-kind", modules[row.module]), node("h2", null, row.question || "收藏的回答"),
        node("p", "saved-card-preview", row.preview));
      const bottom = node("div", "saved-card-footer"), date = new Date(row.saved_at);
      bottom.append(node("span", null, Number.isNaN(date.getTime()) ? "已收藏" : date.toLocaleDateString("zh-CN")),
        button("阅读回答", () => openSaved(row), "saved-read"), button("移出收藏", () => removeSaved(row), "saved-remove"));
      card.append(open, bottom); list.append(card);
    }
    syncSavedButtons();
  }
  function removeSaved(row) {
    let rows;
    try { rows = readSaved(); } catch { toast("收藏记录暂时无法读取，原记录保持不动。"); return; }
    if (!writeSaved(rows.filter(item => !sameSaved(item, row)))) return;
    renderSavedLibrary();
    const feedback = $("saved-feedback");
    const undo = button("撤销", () => {
      try {
        const current = readSaved();
        if (current.some(item => sameSaved(item, row)) || writeSaved([row, ...current])) renderSavedLibrary();
      } catch { toast("暂时无法恢复收藏，请重试。"); }
    });
    feedback.replaceChildren(node("span", null, "已移出收藏"), undo); undo.focus({preventScroll: true});
  }
  async function openSaved(row) {
    const token = ++savedOpenToken;
    if (!$("saved-library").hidden) savedListTop = $("saved-view").scrollTop;
    savedReading = null; $("saved-library").hidden = true;
    const reader = $("saved-reader"); reader.hidden = false; reader.replaceChildren();
    const back = button("返回我的档案", () => {
      savedOpenToken++; savedReading = null; reader.hidden = true; reader.replaceChildren(); $("saved-library").hidden = false;
      renderSavedLibrary(); $("saved-view").scrollTop = savedListTop;
      const focus = $(matchMedia("(max-width:760px)").matches ? "saved-title" : "saved-search");
      if (focus === $("saved-title")) focus.tabIndex = -1;
      focus.focus({preventScroll: true});
    }, "saved-reader-back");
    const title = node("h1", "saved-reader-title", row.question); title.tabIndex = -1;
    const pending = node("p", "saved-feedback", "正在读取原回答…"); pending.setAttribute("role", "status");
    reader.append(back, title, pending); $("saved-view").scrollTop = 0; title.focus({preventScroll: true});
    const current = () => state.view === "saved" && token === savedOpenToken && reader.isConnected;
    try {
      const audit = await request(apiPath(`/v1/qa/runs/${encodeURIComponent(row.run_id)}`, row.release_id));
      if (!current()) return;
      const result = verifiedStoredResult(row, audit); pending.remove();
      savedReading = {result, row, target: reader}; renderAnswer(result, reader, true); syncSavedButtons();
    } catch (error) {
      if (!current()) return;
      pending.replaceChildren(node("span", null, error.httpStatus === 404 ? "原回答暂时无法找到，收藏仍保留。" : "原回答未能读取或校验，收藏仍保留。"),
        button("重新读取", () => openSaved(row)));
    }
  }
  function explorationAvailable(result) {
    return state.view === "chat" && result === state.previous && !state.busy && !state.pending && !state.blocked
      && !state.uncertain && !state.historyReadOnly && !state.historyIncomplete;
  }
  function openExploration(result) {
    if (!explorationAvailable(result)) { toast("请从最新一轮回答继续探索。"); return; }
    const context = answerFlowContext(result), scope = state.conversationId;
    const token = openDetail("继续探索");
    const current = () => token === state.drawerToken && scope === state.conversationId && explorationAvailable(result) && answerFlowCurrent(context);
    const panel = node("div", "explore-panel");
    panel.append(node("p", "explore-context", context.question), node("p", "explore-note", "选个方向，编辑后再发送。"));
    const options = [
      {label: "换个条件", hint: "看看结论会怎样变化", field: "新的条件", placeholder: "例如：难度改为 15，其他条件不变", make: value => `接着上一轮，条件改为：${value}。哪些结论需要调整？请核对来源，区分确定结果和推测。`},
      {label: "对比方案", hint: "把两种选择放在一起", field: "想对比的对象或方案", placeholder: "输入另一个干员、藏品或方案", make: value => `接着上一轮，把它与「${value}」比较：关键差别、适用条件和选择建议是什么？查不到的保持未知。`},
      {label: "找适用边界", hint: "什么情况下不成立", make: () => "接着上一轮，这个结论在哪些情况下不成立？优先列出资料支持的限制和反例，没有证据的不要当成事实。"},
      {label: "做张速记卡", hint: "带走最值得记住的三点", make: () => "把上一轮回答整理成一张速记卡：最多三个要点，保留关键数字、触发条件和必要限制，不增加新事实。"}
    ];
    const choices = node("div", "explore-choices"), editor = node("div", "explore-editor");
    let selected = options[0];
    const label = node("label", "explore-label"), input = node("input", "explore-input"); input.maxLength = 400;
    const fill = button("填入草稿", () => {
      if (!current()) { toast("对话已变化，请从最新回答重新选择。"); return; }
      if (selected.field && !input.value.trim()) { input.focus(); toast("先写下要探索的对象或条件。"); return; }
      const question = selected.make(input.value.trim());
      $("detail-dialog").close();
      fillDraft(question, $("module").value, () => scope === state.conversationId && explorationAvailable(result),
        {run_id: result.run_id, release_id: result.release_id, conversation_id: scope});
    }, "explore-fill");
    label.append(input); editor.append(label, fill);
    const controls = options.map(option => {
      const control = button("", () => select(option), "explore-choice");
      control.append(node("strong", null, option.label), node("span", null, option.hint)); choices.append(control); return control;
    });
    function select(option) {
      selected = option; input.value = "";
      controls.forEach((control, index) => control.setAttribute("aria-pressed", String(options[index] === option)));
      label.hidden = !option.field; input.placeholder = option.placeholder || "";
      input.setAttribute("aria-label", option.field || "探索条件");
      fill.textContent = option.field ? "填入草稿" : `用「${option.label}」追问`;
    }
    select(selected); panel.append(choices, editor); $("detail-body").append(panel);
  }
  function readUnresolved() {
    const records = JSON.parse(localStorage.getItem(unresolvedKey) || "[]");
    if (!Array.isArray(records)) throw new Error("待确认记录格式无法读取。");
    return records;
  }
  function renderUnresolvedHistory(container) {
    try {
      const records = readUnresolved();
      if (records.length) container.append(button(`待确认记录 · ${records.length}`, openUnresolvedHistory, "history-unresolved"));
    } catch { /* A storage error must never remove the active pending task. */ }
  }
  function openUnresolvedHistory() {
    if ($("sidebar").classList.contains("nav-open")) {
      closeNav();
      $("mobile-menu").focus();
    }
    openDetail("待确认记录");
    const body = $("detail-body");
    let records;
    try { records = readUnresolved(); }
    catch { body.append(node("p", "detail-description", "暂时无法读取本机记录。")); return; }
    if (!records.length) { body.append(node("p", "detail-description", "暂无待确认记录。")); return; }
    const dateFormat = new Intl.DateTimeFormat("zh-CN", {month: "long", day: "numeric", hour: "2-digit", minute: "2-digit"});
    for (const record of [...records].reverse()) {
      const pending = record?.pending;
      const title = pending?.display_label || pending?.question;
      const card = node("article", "archived-task");
      const heading = node("header", "archived-task-heading");
      heading.append(node("p", "archived-task-status", record?.reason === "provider_balance_rejected" ? "旧查询已中断" : "原任务待确认"));
      if (typeof record?.saved_at === "string" && !Number.isNaN(Date.parse(record.saved_at))) {
        const time = node("time", null, dateFormat.format(new Date(record.saved_at)));
        time.dateTime = record.saved_at;
        heading.append(time);
      }
      card.append(heading, node("h3", null, typeof title === "string" && title.trim() ? title : "未完成的查询"));
      const actions = node("div", "archived-task-actions");
      if (typeof pending?.task_id === "string" && pending.task_id) {
        actions.append(button("查看原任务", () => openTaskAudit(pending.task_id, openUnresolvedHistory), "text-action"));
      }
      const draft = record?.composer_draft;
      if (typeof draft === "string" && draft.trim() && draft.length <= 2000) {
        card.append(node("p", "archived-task-draft-label", "未发送的草稿"), node("p", "archived-task-draft", draft));
        const restore = button("在新对话中编辑", () => {
          if (state.busy || state.pending || state.blocked) { toast("请先确认当前任务的结果。"); return; }
          newChat(false);
          if (!$("question").value.trim() || $("question").value === draft) $("detail-dialog").close();
          fillDraft(draft, record.composer_module);
        }, "text-action");
        restore.dataset.restoreDraft = "";
        restore.disabled = state.busy || !!state.pending || state.blocked;
        actions.append(restore);
      }
      card.append(actions);
      disclosure(card, "技术详情", record);
      body.append(card);
    }
  }
  function viewControls() {
    return [...document.querySelectorAll("[data-view]")].filter((element) => element.tagName.toLowerCase() === "button");
  }
  function updateSurface() {
    const active = !!(state.session || state.previous || state.pending || state.busy || $("welcome").hidden);
    const queryState = state.blocked || state.uncertain || state.pending && !state.busy ? "blocked" : state.busy ? "busy" : "idle";
    for (const surface of [document.body, $("workspace")].filter(Boolean)) {
      surface.dataset.view = state.view;
      surface.dataset.conversation = active ? "active" : "empty";
      surface.dataset.queryState = queryState;
    }
    syncConversationIndex();
    if (!$("session-dossier")) return;
    $("session-dossier").hidden = state.view !== "chat";
    $("welcome-dossier").hidden = active;
    $("active-dossier").hidden = !active;
    if (!active) return;
    const result = state.previous;
    $("dossier-title").textContent = state.session?.title || "当前对话";
    $("dossier-title").title = $("dossier-title").textContent;
    $("dossier-module").textContent = modules[result?.module] || modules[state.session?.module] || modules[$("module").value] || "自动识别";
    $("dossier-turns").textContent = String(state.session?.turns.length || 0).padStart(2, "0");
    $("dossier-state").textContent = state.taskPhase === "checking" ? "检查原任务中" : state.blocked || state.uncertain ? "结果待确认" : state.taskPhase === "waiting" ? "原查询仍在进行" : state.pending ? "查询中" : state.busy ? "正在读取对话" : state.historyIncomplete ? "对话记录尚未读全" : state.historyReadOnly ? "历史只读" : result ? statusLabels[result.status] || "查询已保存" : "尚未保存回答";
    const selected = visibleTurns.find((row) => row.target === readingTarget);
    const sourceResult = selected ? selected.result : result;
    const heading = selected ? `第 ${selected.number} 轮 · 来源与记录` : "最新一轮 · 来源与记录";
    $("dossier-sources-heading").textContent = heading;
    const sources = $("dossier-sources");
    // Do not replace focused read-only actions on each keystroke or status poll.
    if (sources.sourceOwner !== sourceResult || sources.sourceHeading !== heading) {
      sources.sourceOwner = sourceResult; sources.sourceHeading = heading; sources.replaceChildren();
      if (Array.isArray(sourceResult?.citations) && sourceResult.citations.length) sources.append(button(`本轮来源 · ${sourceResult.citations.length}`, () => openSources(sourceResult), "dossier-action"));
      if (sourceResult?.run_id && sourceResult?.release_id) sources.append(button("本轮查询记录 ↗", () => openAudit(sourceResult), "dossier-action quiet"));
      if (!sources.children.length) sources.append(node("span", "dossier-empty", selected && !selected.result ? "这轮暂无已读取的来源" : "本轮暂无来源"));
    }
  }
  function conversationTurns() {
    const rows = [];
    const add = (target, question, result = null, missing = false, owner = null) => {
      if (!target.id) target.id = `reading-turn-${++turnSequence}`;
      rows.push({target, question, result, missing, owner, number: rows.length + 1});
    };
    const walk = (container) => {
      for (const item of container.children) {
        if (item.classList.contains("history-turn-slot")) {
          const question = item.querySelector(".user-message"), stored = item.querySelector(".stored-turn");
          add(item, question?.textContent || `第 ${item.dataset.turn} 轮记录`, stored ? renderedResults.get(stored) : null, !stored, item);
        } else if (item.classList.contains("user-message")) {
          if (!item.hidden) add(item, item.textContent, null, false, container);
        } else if (item.classList.contains("assistant-message")) {
          if (rows.length && rows.at(-1).owner === container) rows.at(-1).result = renderedResults.get(item) || null;
        } else if (renderedResults.has(item)) {
          if (rows.length && rows.at(-1).owner === container) rows.at(-1).result = renderedResults.get(item);
        } else if (["history-recovery", "pending-history", "stored-turn"].some((name) => item.classList.contains(name))) walk(item);
      }
    };
    walk($("messages")); return rows;
  }
  function turnIndexEntry(row, onSelect) {
    const entry = button("", onSelect, "turn-index-entry");
    entry.dataset.turn = String(row.number);
    entry.title = row.question; entry.setAttribute("aria-controls", row.target.id);
    if (readingTarget === row.target) entry.setAttribute("aria-current", "location");
    entry.append(node("span", "turn-index-number", String(row.number).padStart(2, "0")));
    const content = node("span", "turn-index-copy");
    content.append(node("strong", null, row.question));
    const count = Array.isArray(row.result?.citations) ? row.result.citations.length : 0;
    const label = row.result?.agent_runtime?.requested === "deepseek" && row.result.agent_runtime.server_committed !== true ? "查询未完成" : statusLabels[row.result?.status] || "已保存记录";
    content.append(node("small", null, row.result ? `${label}${count ? ` · ${count} 条来源` : ""}` : row.missing ? "记录尚未读全" : "尚无已读取回答"));
    entry.append(content); return entry;
  }
  function answerFlowContext(result, origin = document.activeElement) {
    if (state.view === "saved" && savedReading?.result === result) {
      return {saved: savedReading, target: savedReading.target, result, question: savedReading.row.question,
        returnFocus: origin, scrollTop: $("saved-view").scrollTop, reading: captureReadingPosition($("saved-view"))};
    }
    const rows = conversationTurns();
    const row = rows.find((row) => row.result === result);
    if (!row) return null;
    const answerTarget = [...document.querySelectorAll(".assistant-message")].find((article) => renderedResults.get(article) === result);
    const area = $("conversation-scroll"), rect = answerTarget?.getBoundingClientRect?.(), viewport = area.getBoundingClientRect?.();
    const originRect = area.contains?.(origin) ? origin.getBoundingClientRect?.() : null;
    return {scope: state.session?.id || state.pending?.task_id || state.conversationId,
      conversationId: state.conversationId, target: row.target, rowNumber: row.number, question: row.question, result,
      answerTarget, answerWasVisible: !rect || !viewport || rect.bottom > viewport.top && rect.top < viewport.bottom,
      scrollTop: area.scrollTop, reading: captureReadingPosition(area), returnFocus: origin,
      originOffset: originRect && viewport && originRect.bottom > viewport.top && originRect.top < viewport.bottom ? originRect.top - viewport.top : null};
  }
  function answerFlowCurrent(context) {
    if (context?.saved) return state.view === "saved" && context.saved === savedReading && context.target.isConnected;
    if (!context || state.view !== "chat" || !context.target.isConnected
      || context.scope !== (state.session?.id || state.pending?.task_id || state.conversationId)
      || context.conversationId !== state.conversationId) return false;
    return conversationTurns().some((row) => row.target === context.target && row.result === context.result);
  }
  function markAnswerReading(context) {
    if (context?.saved) return;
    if (!answerFlowCurrent(context)) return;
    readingTarget = context.target; updateSurface();
  }
  function restoreAnswerReading(context) {
    if (!answerFlowCurrent(context)) return;
    if (context.saved) {
      if (!restoreReadingPosition(context.reading)) $("saved-view").scrollTop = context.scrollTop;
      const target = context.returnFocus?.isConnected ? context.returnFocus : context.target;
      target.tabIndex = -1; target.focus({preventScroll: true}); return;
    }
    scrollGeneration++; automaticScroll = false; followLatest = false;
    readingTarget = context.target; updateSurface();
    const area = $("conversation-scroll");
    const target = context.answerTarget?.isConnected ? context.answerTarget : context.target;
    const origin = context.returnFocus;
    if (origin?.isConnected && Number.isFinite(context.originOffset) && origin.getClientRects().length) {
      area.scrollTop = Math.max(0, area.scrollTop + origin.getBoundingClientRect().top - area.getBoundingClientRect().top - context.originOffset);
    } else if (context.answerWasVisible && restoreReadingPosition(context.reading)) { /* Visible content restored. */ }
    else if (context.answerWasVisible && Number.isFinite(context.scrollTop)) area.scrollTop = Math.max(0, context.scrollTop);
    else if (target.getBoundingClientRect && area.getBoundingClientRect) {
      const offset = target.getBoundingClientRect().top - area.getBoundingClientRect().top + area.scrollTop;
      area.scrollTop = Math.max(0, offset - 14);
    }
    if (context.answerWasVisible && origin?.isConnected && (Number.isFinite(context.originOffset) || origin.classList.contains("source-button") || origin.classList.contains("answer-card-source"))
      && (!origin.getClientRects || origin.getClientRects().length)) origin.focus({preventScroll: true});
    else { target.setAttribute("tabindex", "-1"); target.focus({preventScroll: true}); }
    updateLatestControl();
  }
  function answerFollowupLabel(context) {
    if (draftParent) return "继续编辑草稿";
    if (state.historyReadOnly || state.historyIncomplete || state.blocked || state.uncertain) return "回到输入框";
    if (context?.result?.run_id !== state.previous?.run_id || context?.result?.release_id !== state.previous?.release_id) return "回到最新追问";
    return state.pending || state.busy ? "编写下一问" : "继续追问";
  }
  function focusAnswerFollowup(context) {
    if (!answerFlowCurrent(context)) return;
    readingRevision++;
    sourceReturnContext = null;
    if ($("detail-dialog").open) $("detail-dialog").close();
    readingTarget = null; updateSurface(); scrollBottom(true);
    $("question").focus({preventScroll: true});
  }
  function syncAnswerActions() {
    const rows = new Map(visibleTurns.filter((row) => row.result).map((row) => [row.result, row]));
    for (const article of document.querySelectorAll(".assistant-message")) {
      const result = renderedResults.get(article), control = article.querySelector(".answer-followup");
      const explore = article.querySelector(".answer-explore");
      if (explore) { explore.hidden = result !== state.previous; explore.disabled = !explorationAvailable(result); }
      if (!control) continue;
      const row = rows.get(result), text = answerFollowupLabel(row ? {result} : null);
      if (control.textContent !== text) control.textContent = text;
      control.title = draftParent ? "定位输入框并保留草稿及其原轮次，不会发送问题。"
        : "定位输入框并保留草稿，不会发送问题；提问仍承接最新一轮。";
      control.disabled = !row;
      const label = article.querySelector(".answer-turn-label");
      const title = row ? `第 ${row.number} 轮回答` : "回答";
      if (label && label.textContent !== title) label.textContent = title;
      if (label) label.hidden = visibleTurns.length <= 1;
    }
  }
  function syncConversationIndex() {
    visibleTurns = conversationTurns();
    const scope = state.session?.id || state.pending?.task_id || state.conversationId;
    if (scope !== turnIndexScope) { turnIndexScope = scope; turnIndexQuery = ""; }
    if ($("turn-index-search").value !== turnIndexQuery) $("turn-index-search").value = turnIndexQuery;
    if (!visibleTurns.some((row) => row.target === readingTarget)) readingTarget = null;
    visibleTurns.forEach((row) => row.target.classList.toggle("reading-turn-selected", row.target === readingTarget));
    const selected = visibleTurns.find((row) => row.target === readingTarget);
    syncAnswerActions();
    const earlier = selected && selected.number < visibleTurns.length;
    $("reading-note").hidden = !earlier;
    const draftRow = draftParent ? explorationDraftRow() : null;
    $("reading-note").textContent = earlier ? `正在回看第 ${selected.number} 轮；${draftParent
      ? independentDraft() ? "恢复草稿保留原问题，不承接前轮。" : draftRow ? `${boundDraftLabel()}基于第 ${draftRow.number} 轮。` : `${boundDraftLabel()}的原回答尚未读到。`
      : "输入框提问仍承接最新一轮。"}` : "";
    $("open-turn-index").hidden = visibleTurns.length < 2;
    updateLatestControl();
    $("turn-index").hidden = visibleTurns.length < 2;
    $("turn-index").dataset.long = String(visibleTurns.length > 8);
    $("turn-index-count").textContent = `${visibleTurns.length} 轮`;
    const reference = $("reading-reference");
    reference.hidden = !selected?.result?.run_id || !selected?.result?.release_id;
    reference.textContent = selected?.result ? `第 ${selected.number} 轮${selected.result.citations?.length ? "来源" : "记录"}` : "";
    const filtered = matchingTurns();
    $("turn-index-feedback").textContent = turnIndexQuery.trim() ? `找到 ${filtered.length} / ${visibleTurns.length} 轮` : "";
    const signature = `${turnIndexScope}\u001e${turnIndexQuery}\u001e` + visibleTurns.map((row) => [row.target.id, row.question, row.result?.run_id, row.result?.status, row.result?.citations?.length, row.missing, row.target === readingTarget].join("\u001f")).join("\u001e");
    if (signature === turnIndexSignature) return;
    turnIndexSignature = signature;
    $("turn-index-list").replaceChildren(...filtered.map((row) => turnIndexEntry(row, () => selectReadingTurn(row))));
    if (!filtered.length && visibleTurns.length) $("turn-index-list").append(node("p", "turn-index-empty", "目录中没有匹配的原问题。"));
  }
  function matchingTurns() {
    const query = turnIndexQuery.trim().toLocaleLowerCase();
    // This is a literal filter over displayed original questions and indices.
    // It never interprets, rewrites, or submits a user's chat question.
    return visibleTurns.filter((row) => !query || `${row.number} 第${row.number}轮 ${row.question}`.toLocaleLowerCase().includes(query));
  }
  function selectReadingTurn(row) {
    if (!row.target.isConnected || !visibleTurns.some((item) => item.target === row.target)) return;
    readingTarget = row.target;
    readingRevision++; scrollGeneration++; automaticScroll = false; followLatest = false;
    const area = $("conversation-scroll");
    const offset = row.target.getBoundingClientRect ? row.target.getBoundingClientRect().top - area.getBoundingClientRect().top + area.scrollTop : row.target.offsetTop || 0;
    area.scrollTop = Math.max(0, offset - 14);
    row.target.setAttribute("tabindex", "-1"); row.target.focus({preventScroll: true});
    updateLatestControl(); updateSurface();
  }
  function openTurnIndex() {
    readingRevision++;
    syncConversationIndex(); const token = openDetail("对话目录"), scope = turnIndexScope;
    const mobile = matchMedia("(max-width:760px)").matches;
    const viewport = $("conversation-scroll").getBoundingClientRect?.();
    const nearby = mobile && viewport ? visibleTurns.findLast((row) => row.target.getBoundingClientRect?.().top <= viewport.top + 24) : null;
    const initialTarget = readingTarget || nearby?.target || visibleTurns.at(-1)?.target;
    const list = node("nav", "turn-index-dialog"); list.setAttribute("aria-label", "选择要回看的轮次");
    const toolbar = node("div", "turn-index-toolbar");
    const label = node("label", "turn-index-search"); label.append(node("span", "sr-only", "查找对话目录"));
    const input = node("input"); input.type = "search"; input.maxLength = 160; input.placeholder = "输入轮次或原问题中的文字"; input.value = turnIndexQuery; input.setAttribute("aria-label", "查找对话目录"); label.append(input);
    const feedback = node("p", "turn-index-feedback"); feedback.setAttribute("role", "status");
    toolbar.append(label, feedback); list.append(toolbar, node("p", "detail-description", "回看不会改变续问对象，输入框提问仍承接最新一轮。"));
    const entries = node("div"); list.append(entries);
    const current = () => token === state.drawerToken && scope === turnIndexScope && list.isConnected && $("detail-dialog").open;
    const render = (locate = false) => {
      if (!current()) return;
      const rows = matchingTurns(); entries.replaceChildren();
      feedback.textContent = turnIndexQuery.trim() ? `找到 ${rows.length} / ${visibleTurns.length} 轮` : `共 ${visibleTurns.length} 轮`;
      const controls = rows.map((row) => {
        const entry = turnIndexEntry(row, () => { $("detail-dialog").close(); selectReadingTurn(row); }); entries.append(entry); return {row, entry};
      });
      if (!rows.length) entries.append(node("p", "turn-index-empty", "目录中没有匹配的原问题。"), button("清空查找", () => { if (current()) { input.value = ""; update(); input.focus(); } }, "text-action"));
      if (locate) requestAnimationFrame(() => {
        if (!current()) return;
        controls.find(({row}) => row.target === initialTarget)?.entry.scrollIntoView?.({block: "nearest"});
      });
    };
    const update = () => {
      if (!current()) return;
      turnIndexQuery = input.value.slice(0, 160); syncConversationIndex(); render();
    };
    input.addEventListener("input", update);
    $("detail-body").append(list);
    render(true);
    if (!mobile) input.focus({preventScroll: true});
  }
  function openReadingReference() {
    const result = visibleTurns.find((row) => row.target === readingTarget)?.result;
    if (!result?.run_id || !result?.release_id) return;
    if (Array.isArray(result.citations) && result.citations.length) openSources(result);
    else openAudit(result);
  }
  function selectModule(module, focus = true) {
    if (!Object.prototype.hasOwnProperty.call(modules, module)) return;
    if (state.busy || state.pending || state.blocked) { toast("请先确认当前任务的结果。"); return; }
    if (state.session && module !== state.session.module) { toast("请另起话题后切换模块。"); if (focus) $("question").focus(); return; }
    $("module").value = module;
    document.querySelectorAll("[data-example-module]").forEach((control) => {
      const selected = control.dataset.exampleModule === module;
      control.classList.toggle("active", selected); control.setAttribute("aria-pressed", String(selected));
    });
    document.querySelectorAll(".suggestion").forEach((control) => { control.hidden = module !== "auto" && !!control.dataset.module && control.dataset.module !== module; });
    switchView("chat"); saveDraft(); updateComposer();
    if (focus) $("question").focus();
  }
  function draftQuestion(control) {
    if (state.busy || state.pending || state.blocked) { toast("请先确认当前任务的结果。"); return; }
    const question = control.dataset.question;
    if (typeof question !== "string" || !question.trim()) return;
    fillDraft(question, control.dataset.module);
  }
  function closeNav() {
    $("sidebar").classList.remove("nav-open"); $("nav-backdrop").hidden = true; $("mobile-menu").setAttribute("aria-expanded", "false");
    $("workspace").inert = false;
    if (matchMedia("(max-width:760px)").matches) $("sidebar").inert = true;
  }
  function switchView(view) {
    if (view !== state.view && (view === "saved" || state.view === "saved")) {
      savedOpenToken++; savedReading = null;
      $("saved-reader").hidden = true; $("saved-reader").replaceChildren(); $("saved-library").hidden = false;
    }
    closeContextBubble();
    state.view = view;
    for (const name of ["chat", "library", "saved", "help", "tools"]) $(`${name}-view`).hidden = view !== name;
    const titles = {chat: "问答工作台", library: "资料检索", saved: "我的档案", help: "使用说明", tools: "战术工具"};
    $("view-title").textContent = titles[view];
    viewControls().forEach((b) => { b.classList.toggle("active", b.dataset.view === view); if (b.dataset.view === view) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current"); });
    closeNav(); updateSurface();
    if (view === "saved") renderSavedLibrary();
    if (view === "tools") window.ArkGameTools?.open();
  }
  function modelChatUnavailable() {
    if (modelSwitching) return "正在切换模型，请稍候。";
    if (state.pending || state.blocked) return "原任务结果尚待确认，请先检查原任务状态。";
    if (state.modelChecking) return "正在检查模型状态，请稍候。";
    if (state.historyIncomplete) return "请先重新读取缺失的对话记录，补齐后再继续追问。";
    if (state.historyReadOnly) return "这是只读历史记录，请点击“开启模型对话”后再提问。";
    if (!state.ready || !state.modelReady) return "模型对话已暂停。可刷新模型状态，或前往资料检索浏览已有资料。";
    return null;
  }
  function isModelRun(result) {
    const runtime = result?.agent_runtime;
    // Failed model tasks may retain used=local because no model answer was
    // committed. Their authoritative task origin still permits a guarded retry.
    return runtime?.requested === "deepseek" && runtime.simulation !== true && (runtime.used === "deepseek"
      || (typeof runtime.http_task?.task_id === "string" && runtime.http_task.task_id.length > 0));
  }
  function modelAvailabilityCopy() {
    // Exact status codes are presentation data, never an interpretation of the
    // user's question or a substitute for the server's ready decision.
    const reason = state.modelAvailability?.reason;
    if (reason === "campaign_frozen") return {title: "有一笔模型请求待确认", note: "上一笔模型请求的结果或费用待确认。", detail: "服务端已暂停接收新的模型请求，直到这笔记录得到处理。你可以继续查看已保存的回答和原始资料。"};
    if (reason === "proof_refresh_in_progress") return {title: "正在核验官方价格", note: "启动时正在核验 DeepSeek 价格。", detail: "通常几秒后完成。可刷新模型状态，资料浏览现在可用。"};
    if (reason === "proof_refresh_failed") return {title: "官方价格暂无法核验", note: "未能连接或读完 DeepSeek 价格页。", detail: "请检查 Mac 的网络连接。后台会再次核验，完成后可刷新模型状态。"};
    if (reason === "price_source_changed") return {title: "官方价格页面需要复核", note: "官方价格页有变更，模型暂不可用。", detail: "请在 Mac 上复核 DeepSeek 的模型和价格。资料浏览仍可用。"};
    if (reason === "runtime_profile_unavailable") return {title: "模型来源校验失败", note: "模型来源校验失败，可先浏览资料。", detail: "请在 Mac 上检查一键启动日志，确认模型凭据完整。"};
    if (reason === "price_verification_expired") return {title: "价格信息需要重新核验", note: "价格校验已过期，模型暂不可用。", detail: "服务端需要更新价格核验信息后，才能继续模型查询。你可以继续查看已保存的回答和原始资料。"};
    if (["price_not_verified", "price_verification_missing", "price_verification_invalid", "price_verification_in_future"].includes(reason)) return {title: "价格信息尚未通过核验", note: "价格信息待核验，模型暂不可用。", detail: "服务端价格信息尚未通过核验。更新服务端配置后，可重新检查状态；已保存的回答和原始资料仍可查看。"};
    if (reason === "configuration_missing") return {title: "模型配置尚未完成", note: "模型尚未配置，可先浏览资料。", detail: "服务端模型配置尚未完成，暂时无法提交新的模型查询。配置完成后可重新检查状态。"};
    if (reason === "live_disabled") return {title: "模型服务尚未启用", note: "模型服务未启用，可先浏览资料。", detail: "服务端当前没有启用真实模型查询。已保存的回答和原始资料仍可查看。"};
    if (reason === "request_inflight") return {title: "已有模型请求正在处理", note: "服务端正在处理已有模型请求。", detail: "请等待原请求完成后重新检查状态。检查状态不会重新发送你的问题。"};
    return {title: "模型暂时不可用", note: "模型对话暂停，可先浏览资料。", detail: "服务端尚未允许新的模型查询。可以重新检查状态，或先浏览已保存的回答和原始资料。"};
  }
  function openModelStatus() {
    if (!state.ready || state.modelReady || state.modelChecking || state.pending || state.busy || state.blocked || state.uncertain || state.historyIncomplete || state.historyReadOnly) return;
    const copy = modelAvailabilityCopy(), snapshot = state.modelAvailability;
    const token = openDetail("模型服务状态"), container = $("detail-body");
    const panel = node("section", "model-availability");
    const current = () => token === state.drawerToken && panel.isConnected && $("detail-dialog").open;
    panel.append(node("p", "model-availability-kicker", "资料服务已连接"), node("h3", "model-availability-title", copy.title), node("p", "model-availability-description", copy.detail));
    const actions = node("div", "model-availability-actions");
    actions.append(button("浏览资料", () => { if (current()) { $("detail-dialog").close(); switchView("library"); $("library-query").focus(); } }, "model-availability-primary"),
      button("重新检查状态", async () => {
        if (!current() || state.pending || state.blocked || state.uncertain) return;
        $("detail-dialog").close();
        const scope = state.conversationId, anchor = $("backend-status");
        anchor.tabIndex = -1; anchor.focus({preventScroll: true});
        await refreshModelReadiness();
        if (document.activeElement === anchor && state.view === "chat" && state.conversationId === scope) $("refresh-model").focus({preventScroll: true});
      }, "text-action"));
    panel.append(actions, node("p", "model-availability-note", "检查状态不会启动模型查询，输入草稿会保留。"));
    const technical = node("details", "model-availability-details"); technical.append(node("summary", null, "查看状态详情"));
    meta(technical, [["网页状态原因", snapshot?.reason || "服务端未提供"], ["模型状态原因", snapshot?.modelReason || "服务端未提供"]]);
    panel.append(technical); container.append(panel);
  }
  function updateComposer() {
    $("workspace").dataset.module = $("module").value;
    $("chat-form").dataset.phase = state.blocked || state.uncertain ? "paused" : state.pending || state.busy ? "busy" : state.modelReady ? "ready" : "paused";
    const length = $("question").value.length;
    $("character-count").textContent = `${length} / 2000`;
    $("send-button").disabled = !length || !$("question").value.trim() || state.busy || !!modelChatUnavailable();
    $("question").style.height = "auto";
    $("question").style.height = `${Math.min(160, $("question").scrollHeight)}px`;
    $("context-strip").hidden = !state.previous && !draftParent;
    const draftRow = explorationDraftRow();
    $("latest-draft").hidden = !draftParent || !state.previous || draftRow?.result === state.previous;
    $("context-strip").classList.toggle("has-draft-choice", !$("latest-draft").hidden);
    $("latest-draft").disabled = state.busy || !!state.pending || state.blocked || state.historyIncomplete || state.historyReadOnly;
    if (state.previous || draftParent) {
      $("context-label").textContent = draftParent ? independentDraft() ? "恢复草稿 · 不承接前轮" : draftRow ? `${boundDraftLabel()} · 基于第 ${draftRow.number} 轮` : `${boundDraftLabel()} · 原回答尚未读到`
        : `继续追问 · ${state.session?.title || modules[state.session?.module] || "当前对话"}`;
      $("context-label").title = $("context-label").textContent;
    }
    $("backend").value = "deepseek";
    const unresolved = state.blocked || state.uncertain;
    const balanceRejected = state.taskPhase === "balance_rejected";
    const label = state.taskPhase === "checking" ? "检查原任务中" : balanceRejected ? "旧任务已中断" : unresolved ? "结果待确认" : state.taskPhase === "waiting" ? "仍在等待结果" : state.pending ? "查询中" : state.busy ? "正在读取" : state.historyIncomplete ? "记录尚未读全" : state.historyReadOnly ? "历史只读" : state.modelChecking ? "检查状态中" : state.modelReady ? "模型就绪" : "模型暂停";
    $("backend-status").textContent = label;
    $("backend-status").dataset.state = unresolved ? "paused" : state.pending || state.busy ? "busy" : state.modelReady && !state.historyReadOnly ? "ready" : "paused";
    const archive = availablePendingArchive();
    const recovery = !archive && taskRecovery?.container.isConnected && taskRecovery.saved.task_id === state.pending?.task_id;
    $("mode-note").textContent = archive ? "旧任务与未确认的费用会保留。" : unresolved ? "只检查原任务状态，不会自动重发。" : state.pending ? "原查询正在进行，可以先编写下一条问题。" : state.historyIncomplete ? "部分记录尚未恢复，请点击“恢复完整对话”。" : state.historyReadOnly ? "旧记录保持原查询方式，开启模型对话后可重新提问。" : state.modelChecking ? "正在检查模型状态，草稿会保留。" : state.modelReady ? "模型查询会产生 API 费用。" : state.ready ? modelAvailabilityCopy().note : state.serviceFailure || "服务未连接，可重新检查状态。";
    $("model-status-details").hidden = !state.ready || state.modelReady || state.modelChecking || !!state.pending || state.busy || unresolved || state.historyIncomplete || state.historyReadOnly;
    $("new-chat").disabled = !archive && (state.busy && !state.historyLoading || !!state.pending || state.blocked);
    $("pending-new-chat").hidden = !archive;
    $("recheck-task").hidden = !recovery;
    $("detach-context").disabled = state.busy || !!state.pending || state.blocked;
    $("module").disabled = state.busy || !!state.session || !!state.pending || state.blocked;
    $("input-hint").textContent = matchMedia("(max-width:760px)").matches ? "回车换行 · 点击发送" : "Enter 发送 · Shift + Enter 换行";
    document.querySelectorAll("[data-example-module]").forEach((control) => { const selected = control.dataset.exampleModule === $("module").value; control.classList.toggle("active", selected); control.setAttribute("aria-pressed", String(selected)); });
    document.querySelectorAll(".suggestion").forEach((control) => { control.hidden = $("module").value !== "auto" && !!control.dataset.module && control.dataset.module !== $("module").value; });
    updateDraftStatus(); syncContextUsage(); renderComposerModel();
    $("refresh-model").hidden = !!archive || !!recovery || state.taskPhase === "checking";
    $("refresh-model").disabled = state.modelChecking;
    document.querySelectorAll(".candidate-choice").forEach((b) => { b.disabled = state.busy || !!modelChatUnavailable(); });
    document.querySelectorAll(".suggestion,[data-example-module]").forEach((b) => { b.disabled = state.busy || !!state.pending || state.blocked; });
    updateSurface();
  }
  function busy(value) {
    state.busy = value;
    $("new-chat").disabled = value; $("module").disabled = value || !!state.session;
    $("detach-context").disabled = value;
    document.querySelectorAll(".suggestion,.candidate-choice").forEach((b) => { b.disabled = value; });
    updateComposer(); renderHistory();
  }
  function newChat(focus = true) {
    if (state.pending || state.blocked) { toast("原任务结果尚待确认，请先检查原任务状态。"); return; }
    if (state.busy && !state.historyLoading) return;
    if (state.historyLoading) { state.historyLoading = false; state.busy = false; }
    scrollGeneration++; automaticScroll=false;
    saveDraft();
    state.session = null; state.previous = null; state.historyReadOnly = false; state.historyIncomplete = false; state.historyLoading = false; state.taskPhase = null; state.conversationId = newUuid();
    $("messages").replaceChildren(); $("welcome").hidden = false;
    $("backend").value = "deepseek"; restoreDraft("new");
    $("module").disabled = false; switchView("chat"); updateComposer(); renderHistory();
    $("conversation-scroll").scrollTop = 0;
    followLatest = true; unseenContent = null; unseenAnswer = null; updateLatestControl();
    if (focus) $("question").focus();
  }
  function availablePendingArchive() {
    const archive = state.pendingArchive;
    if (!state.busy && archive?.container.isConnected && archive.saved.task_id === state.pending?.task_id) return archive;
    return null;
  }
  function startNewChat() {
    const archive = availablePendingArchive();
    if (archive) return archivePendingAndStart(archive.saved, archive.container, archive.reason);
    newChat();
  }
  function updateLatestControl() {
    const jump = $("jump-latest"), tools = $("conversation-tools");
    const hideJump = followLatest || !$("messages").children.length;
    const hideTools = hideJump && visibleTurns.length < 2;
    const label = unseenContent === "answer" ? "有新回答 ↓" : unseenContent === "status" ? "查看最新状态 ↓" : "回到最新 ↓";
    if (jump.hidden !== hideJump) jump.hidden = hideJump;
    if (jump.textContent !== label) jump.textContent = label;
    if (tools.hidden !== hideTools) tools.hidden = hideTools;
  }
  function scrollBottom(force = false, content = null, answerStart = null) {
    if (force) followLatest = true;
    if (!followLatest && content) { unseenContent = content; unseenAnswer = content === "answer" ? answerStart : null; }
    const generation = ++scrollGeneration;
    if (followLatest) automaticScroll = true;
    requestAnimationFrame(() => {
      if (generation !== scrollGeneration) return;
      if (followLatest) {
        const area = $("conversation-scroll");
        const bounds = area.getBoundingClientRect?.();
        const answer = answerStart?.getBoundingClientRect?.();
        const readFromTop = answerStart?.isConnected && bounds && answer && answer.height > area.clientHeight;
        area.scrollTop = readFromTop ? Math.max(0, answer.top - bounds.top + area.scrollTop - 12) : area.scrollHeight;
        if (readFromTop) followLatest = false;
        unseenContent = null; unseenAnswer = null;
      }
      updateLatestControl();
      requestAnimationFrame(() => { if (generation === scrollGeneration) automaticScroll = false; });
    });
  }
  function userMessage(question, target = $("messages")) {
    const item = node("article", "message user-message"); item.setAttribute("aria-label", "你的问题"); item.append(node("div", "user-content", question)); target.append(item);
  }
  function remember(result, question, displayLabel, origin = {}) {
    if (!result.run_id || !result.release_id) return;
    if (!state.session) state.session = {id: result.run_id, title: question, module: origin.module || $("module").value, backend: origin.backend || $("backend").value, conversation_id: origin.conversation_id || state.conversationId, turns: []};
    if (draftScope !== `session:${state.session.id}`) {
      // A first answer turns this draft into a continuation of this exact session.
      const draft = $("question").value, parent = draftParent; $("question").value = ""; saveDraft();
      draftScope = `session:${state.session.id}`; $("question").value = draft; draftParent = parent; saveDraft();
    }
    if (!state.session.turns.some((turn) => turn.run_id === result.run_id)) state.session.turns.push({run_id: result.run_id, release_id: result.release_id, ...(displayLabel ? {display_label: displayLabel} : {})});
    state.sessions = [state.session, ...state.sessions.filter((s) => s.id !== state.session.id)].slice(0, 12); saveSessions();
  }
  async function send(question, parent = state.previous, displayLabel = null, origin = {}) {
    question = question.trim(); if (!question || state.busy || !state.ready) return;
    const unavailable = modelChatUnavailable();
    if (unavailable) { toast(unavailable); return; }
    if (origin.consumeDraft && draftParent) {
      const row = explorationDraftRow();
      if (!row && !independentDraft()) { toast("草稿的原回答尚未读到，请恢复原对话，或主动改为承接最新回答。"); return; }
      parent = row?.result || null;
      origin = {...origin, release_id: draftParent.release_id, exploration: {...draftParent}};
    }
    const backend = "deepseek";
    if (question.length > 2000) { toast("问题最多 2000 字。"); return; }
    readingTarget = null;
    switchView("chat"); $("welcome").hidden = true;
    if (parent && state.previous && parent.run_id !== state.previous.run_id) {
      $("messages").append(node("p", "choice-note", "从此前这条回答继续查询。"));
    }
    userMessage(displayLabel || question);
    if (origin.consumeDraft) { $("question").value = ""; saveDraft(); }
    state.taskPhase = "submitting"; busy(true);
    const pending = node("div", "task-progress"); pending.setAttribute("role", "status"); pending.append(node("span", "pending-indicator"), node("span", null, "正在提交查询…")); $("messages").append(pending); scrollBottom(true);
    const payload = {question, backend, module: origin.module || state.session?.module || $("module").value, release_id: origin.release_id || parent?.release_id || state.session?.turns[0]?.release_id || state.release.id};
    if (parent?.run_id) payload.previous_run_id = parent.run_id;
    const saved = {task_id: newUuid(), conversation_id: origin.conversation_id || state.conversationId, module: payload.module, backend, release_id: payload.release_id, session_id: state.session?.id || null, display_label: displayLabel, draft_scope: draftScope, question, previous_run_id: payload.previous_run_id || null};
    state.pending = saved; savePending(saved); updateComposer();
    try {
      const task = await request("/v1/agent/tasks", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({...payload,request_id:saved.task_id,conversation_id:saved.conversation_id})});
      await followTask(saved, pending, task);
    } catch (error) {
      if ((error.httpStatus && error.httpStatus < 500) || error.definitelyRejected) {
        const restoredParent = origin.exploration || {run_id: parent?.run_id || null, release_id: payload.release_id, conversation_id: saved.conversation_id, kind: "restored"};
        clearPending(); pending.className = "message error-message"; pending.setAttribute("role", "alert"); pending.replaceChildren(node("strong", null, "查询未开始"), node("p", null, error.message), button("放回输入框", () => fillDraft(question, payload.module, () => state.conversationId === saved.conversation_id, restoredParent), "text-action")); busy(false); scrollBottom(false, "status"); refreshModelReadiness();
      } else taskConnectionLost(saved, pending, error);
    }
  }
  function savePending(value) {
    try { localStorage.setItem(pendingKey,JSON.stringify(value)); }
    catch { toast("无法保存任务编号。请保持本页打开，避免丢失状态入口。"); }
  }
  function clearPending() { state.pending = null; state.pendingArchive = null; taskRecovery = null; state.uncertain = false; state.taskPhase = null; try {localStorage.removeItem(pendingKey);} catch {} }
  function checkOriginalTask(saved, container) {
    if (container.dataset.polling === "true" || !container.isConnected || state.pending?.task_id !== saved.task_id) return;
    if (saved.session_id && !state.session) {
      // The initial status GET may have failed before we could validate and
      // display the preceding turns. Re-enter that read-only recovery path.
      container.dataset.polling="true";
      return resumePending(saved);
    }
    return followTask(saved,container);
  }
  function taskConnectionLost(saved, container, error) {
    state.pendingArchive = null;
    taskRecovery = {saved, container};
    if (error.httpStatus === 404) { renderMissingPending(saved, container); return; }
    state.uncertain = true;
    state.taskPhase = "uncertain";
    container.className="task-progress task-uncertain"; container.setAttribute("role", "alert"); container.dataset.taskPhase = "uncertain";
    const title = error.code === "client_timeout" ? "等待服务响应超时，原任务仍待确认" : error.code === "invalid_task_response" ? "任务状态待核对" : "连接中断，结果待确认";
    container.replaceChildren(node("strong",null,title),node("p",null,"原查询可能仍在进行。检查原任务只读取状态，不会重新发送问题。"),button("检查原任务状态",()=>checkOriginalTask(saved,container),"text-action"),button("查看本轮记录",()=>openTaskAudit(saved.task_id),"text-action"));
    // Only GET is offered. A lost POST response is never retried under a new ID.
    busy(true); scrollBottom(false, "status");
  }
  function renderMissingPending(saved, container) {
    state.uncertain = true; state.blocked = true;
    state.taskPhase = "uncertain";
    state.pendingArchive = {saved, container, reason: "task_not_found"};
    container.className = "task-progress task-uncertain"; container.setAttribute("role", "alert");
    container.replaceChildren(node("strong", null, "未找到原任务记录"), node("p", null, "暂无法确认先前结果。可保留待确认记录后，开启新对话。"),
      button("重新检查原任务", () => checkOriginalTask(saved, container), "text-action"),
      button("查看待确认记录", () => { openDetail("原任务 · 结果待确认"); meta($("detail-body"), [["任务 ID", saved.task_id], ["会话 ID", saved.conversation_id], ["资料版本", saved.release_id]]); disclosure($("detail-body"), "完整本机任务记录", saved); }, "text-action"),
      button("保留记录，开启新对话", () => archivePendingAndStart(saved, container, "task_not_found"), "text-action"));
    busy(false); scrollBottom(false, "status");
  }
  function archivePendingAndStart(saved, container, reason) {
    if (state.pending?.task_id !== saved.task_id || container.dataset.polling === "true") return;
    const draft = $("question").value, draftModule = $("module").value;
    try {
      const records = readUnresolved();
      const activeStorage = localStorage.getItem(pendingKey);
      if (activeStorage !== null) {
        const active = JSON.parse(activeStorage);
        if (active?.task_id !== saved.task_id || active?.conversation_id !== saved.conversation_id) throw new Error("另一个页面更新了原任务。");
      }
      const record = {saved_at: new Date().toISOString(), reason, result: "unknown", pending: saved, active_pending_storage: activeStorage, composer_draft: draft, composer_module: draftModule};
      const otherRecords = records.filter((item) => item?.pending?.task_id !== saved.task_id || item.pending.conversation_id !== saved.conversation_id);
      const serialized = JSON.stringify([...otherRecords, record]);
      localStorage.setItem(unresolvedKey, serialized);
      if (localStorage.getItem(unresolvedKey) !== serialized) throw new Error("记录未完整保存。");
      if (localStorage.getItem(pendingKey) !== activeStorage) throw new Error("另一个页面更新了原任务。");
      localStorage.removeItem(pendingKey);
      if (localStorage.getItem(pendingKey) !== null) throw new Error("原任务仍在当前会话中。");
    } catch { toast("无法完整保存待确认记录，已保留原任务。请勿清除浏览器数据。"); return; }
    state.pending = null; state.pendingArchive = null; taskRecovery = null; state.blocked = false; state.uncertain = false;
    busy(false); newChat(false);
    if (draft && !$("question").value) fillDraft(draft, draftModule);
    updateComposer(); $("question").focus();
    toast("原任务已保留在“待确认记录”，结果与费用仍待确认。");
  }
  function rejectedForBalance(reason, runtime) {
    return reason === "http_error" && runtime?.provider_response_shape?.http_status === 402;
  }
  function queryFailureCopy(reason, runtime = {}, unknown = false, continuationAllowed = false) {
    const detail = continuationAllowed ? "这次提问已停止，可以接着问或开启新对话。原请求不会自动重发。" : "请查看本轮记录，原请求不会自动重发。";
    const shape = runtime?.provider_response_shape;
    if (reason === "http_error" && Number.isInteger(shape?.http_status) && shape.http_status !== 402) {
      const labels = {invalid_api_key:"API 密钥未通过验证", model_not_found:"当前模型不可用", unsupported_parameter:"当前模型不支持请求中的参数", unsupported_value:"当前模型不支持请求中的参数值", insufficient_quota:"服务商额度不足", rate_limit_exceeded:"服务商请求限流", context_length_exceeded:"请求超出模型上下文范围", unsupported_country_region_territory:"服务商拒绝了当前地区的请求"};
      return {title:labels[shape.error_code] || (shape.http_status === 400 ? "模型拒绝了这次请求（HTTP 400）" : `模型服务请求失败（HTTP ${shape.http_status}）`), detail, retry:false};
    }
    if (rejectedForBalance(reason, runtime)) {
      return {title:"服务商余额或额度不足", detail, retry:false};
    }
    if (unknown && continuationAllowed) return {title:["timeout", "provider_timeout"].includes(reason) ? "这次提问已超时" : "这次提问已停止", detail, retry:false};
    if (unknown) return {title:"查询结果尚未确认", detail:"已暂停继续付费。请检查原任务状态；费用与挂账保留。", retry:false};
    const limits = {
      stage40_turn_request_limit_reached: "本题查询次数已达上限",
      stage40_request_limit_reached: "本阶段查询次数已达上限",
      campaign_request_limit_reached: "累计查询次数已达上限",
    };
    if (limits[reason]) return {
      title:limits[reason],
      detail:runtime?.model_calls === 0 ? "未新增模型调用，可查看已有记录。" : "可用次数已用完，可查看已有记录。",
      retry:false,
    };
    if (reason === "campaign_frozen") return {title:"模型查询已暂停", detail:"存在待确认的费用或任务，请查看已有记录。", retry:false};
    if (["time_limit", "provider_timeout", "timeout"].includes(reason)) return {title:"本次查询已超时", detail:"本轮未完成，可查看已有记录。", retry:false};
    return {title:"这次查询未完成", detail:"可查看本轮记录，了解查询结果。", retry:true};
  }
  function renderQueryFailure(container, result, taskId, reason, unknown = false, continuationAllowed = false) {
    if (result?.run_id && result?.release_id) renderedResults.set(container, result);
    const copy = queryFailureCopy(reason, result?.agent_runtime, unknown, continuationAllowed);
    container.className="message error-message"; container.setAttribute("role","alert");
    container.replaceChildren(node("strong",null,copy.title),node("p",null,copy.detail));
    if (result?.run_id && result?.release_id) container.append(button("查看本轮记录",()=>openAudit(result),"text-action"));
    else if (taskId) container.append(button("查看本轮记录",()=>openTaskAudit(taskId),"text-action"));
    if (copy.retry && taskId && !result?.agent_runtime?.budget?.frozen) container.append(retryKnownTaskButton(taskId));
  }
  function taskContractError() {
    const error = new Error("服务返回的任务状态或回答不完整，请重新核对原任务。");
    error.code = "invalid_task_response"; error.uncertain = true; return error;
  }
  function validateTask(saved, task, resuming = false) {
    if (!task || task.task_id !== saved.task_id || typeof task.terminal !== "boolean") throw taskContractError();
    const allowed = task.terminal ? ["completed", "failed", "unknown"] : ["queued", "running", "waiting"];
    if (!allowed.includes(task.state)) throw taskContractError();
    if (!resuming && ((task.release_id && task.release_id !== saved.release_id)
      || (task.conversation_id && task.conversation_id !== saved.conversation_id))) throw taskContractError();
    const result = task.response;
    if (task.state === "completed") {
      const runtime=result?.agent_runtime;
      if (task.blocked || !result || typeof result.run_id !== "string" || !result.run_id
        || typeof result.release_id !== "string" || !result.release_id || typeof result.answer !== "string"
        || result.status === "timeout" || runtime?.requested !== "deepseek" || runtime.used !== "deepseek"
        || runtime.simulation === true || runtime.server_committed !== true) throw taskContractError();
      if (result.release_id !== (task.release_id || saved.release_id)) throw taskContractError();
    }
    if (result?.agent_runtime?.http_task?.task_id && result.agent_runtime.http_task.task_id !== saved.task_id) throw taskContractError();
    return task;
  }
  const taskVerificationLabels = {
    planning: "正在确定查询范围",
    searching: "正在查资料",
    writing: "正在整理资料",
    reviewing: "正在独立复核",
    revising: "正在根据复核修订",
    saving: "正在保存已复核的回答",
  };
  function updateTaskVerification(container, phase, progress) {
    const current = typeof progress?.phase === "string" && Object.hasOwn(taskVerificationLabels, progress.phase)
      && ["running", "waiting"].includes(phase);
    const panel = container.querySelector(".task-verification");
    if (!panel) return;
    panel.hidden = !current;
    container.querySelector(".task-generic-note").hidden = current && phase === "running";
    container.querySelector(".task-heading").hidden = current && phase === "running";
    if (!current || panel.dataset.revision === String(progress.revision)) return;
    panel.dataset.revision = String(progress.revision);
    const stage = panel.querySelector(".task-verification-stage");
    stage.textContent = progress.phase === "searching" ? progress.activity || taskVerificationLabels.searching : taskVerificationLabels[progress.phase];
    const scope = panel.querySelector(".task-verification-scope");
    scope.textContent = [verificationScope(progress), verificationOutcome(progress)].filter(Boolean).join(" · ");
    const recent = panel.querySelector(".task-verification-recent");
    const steps = verificationSteps(progress.steps).slice(0, -1);
    recent.hidden = !steps.length;
    recent.querySelector(".verification-steps").replaceChildren(...steps.map(verificationStepRow));
    const list = panel.querySelector(".task-verification-sources");
    const sources = verificationSources(progress.sources);
    const signature = JSON.stringify(sources);
    if (list.dataset.sources !== signature) {
      list.dataset.sources = signature;
      list.replaceChildren(...renderVerificationSources(sources));
    }
    panel.querySelector(".task-verification-found").hidden = !sources.length;
  }
  function renderTaskProgress(saved, container, phase, progress = null) {
    const labels = {
      checking: ["正在检查原任务", "只读取原任务状态，不会重新发送问题。"],
      queued: ["等待查询", "问题已提交，等待服务开始处理。"],
      running: ["正在查询…", "可以先编写下一条问题，当前查询完成后再发送。"],
      waiting: ["原查询仍在进行", "结果尚未返回。页面继续检查同一个任务，不会重新提交问题。"],
    };
    state.taskPhase = phase;
    if (container.dataset.taskPhase === phase && container.querySelector(".task-heading")) {
      updateTaskVerification(container, phase, progress);
      scrollBottom(false);
      return;
    }
    container.className = `task-progress${phase === "waiting" ? " task-waiting" : ""}`;
    container.dataset.taskPhase = phase; container.removeAttribute("role");
    const heading = node("div", "task-heading"); heading.append(node("span", "pending-indicator"), node("strong", "task-phase", labels[phase][0]));
    heading.setAttribute("role", "status");
    const panel = node("section", "task-verification");
    panel.setAttribute("aria-label", "实时查证过程");
    const stage = node("p", "task-verification-stage");
    stage.setAttribute("role", "status");
    const found = node("div", "task-verification-found");
    found.append(node("h3", null, "已找到的候选来源"), node("ul", "task-verification-sources"));
    const recent = node("details", "task-verification-recent");
    recent.append(node("summary", null, "已完成的步骤"), node("ol", "verification-steps"));
    panel.append(stage, node("p", "task-verification-scope"), found, recent);
    const actions = node("div", "task-actions"); actions.append(button("查看原任务记录", () => openTaskAudit(saved.task_id), "text-action"));
    container.replaceChildren(heading, node("p", "task-generic-note", labels[phase][1]), panel, actions);
    updateTaskVerification(container, phase, progress);
    updateComposer(); scrollBottom(false);
  }
  async function followTask(saved, container, initial = null) {
    if (container.dataset.polling === "true" || state.pending?.task_id !== saved.task_id || !container.isConnected) return;
    container.dataset.polling="true"; taskRecovery = null; state.uncertain = false;
    if (!initial) renderTaskProgress(saved, container, "checking");
    busy(true);
    try {
      let task=initial;
      while (state.pending?.task_id === saved.task_id) {
        task=task || await request(`/v1/agent/tasks/${encodeURIComponent(saved.task_id)}`);
        validateTask(saved, task);
        syncContextUsage(true);
        if (task.terminal) return completeTask(task,saved,container);
        renderTaskProgress(saved, container, task.state, task.progress);
        await new Promise((resolve)=>setTimeout(resolve,Math.max(600,Math.min(3000,task.poll_after_ms || 1000)))); task=null;
      }
    } catch(error) { taskConnectionLost(saved,container,error); }
    finally { container.dataset.polling="false"; }
  }
  function completeTask(task,saved,container) {
    const result=task.response;
    let answerStart = null;
    if (task.state === "completed" && result && result.run_id && result.release_id && typeof result.answer === "string") {
      state.blocked = false; clearPending(); container.remove(); answerStart = renderAnswer(result); state.previous=result;
      remember(result,task.question || saved.question || saved.display_label || "模型查询",saved.display_label,saved);
    } else {
      const unknown=task.state === "unknown" || task.blocked;
      const reason = task.error_code || task.reason || task.state;
      const isolated = unknown && task.terminal === true && task.continuation_allowed === true;
      const canArchive = unknown && !isolated && rejectedForBalance(reason, result?.agent_runtime);
      state.pendingArchive = canArchive ? {saved, container, reason: "provider_balance_rejected"} : null;
      taskRecovery = unknown ? {saved, container} : null;
      renderQueryFailure(container,result,task.task_id,reason,unknown,isolated);
      if (unknown && !isolated) container.append(button("检查原任务状态",()=>checkOriginalTask(saved,container),"text-action"));
      if (canArchive) container.append(button("保留记录，开启新对话",
        () => archivePendingAndStart(saved, container, "provider_balance_rejected"), "text-action"));
      if (unknown && !isolated) {state.blocked=true; state.taskPhase=canArchive ? "balance_rejected" : "unknown"; savePending(saved);} else {
        if (isolated) {
          try {
            const records = readUnresolved().filter(item => item?.pending?.task_id !== saved.task_id);
            records.push({saved_at:new Date().toISOString(), reason, result:"unknown", pending:saved,
              composer_draft:$("question").value, composer_module:$("module").value});
            localStorage.setItem(unresolvedKey, JSON.stringify(records));
          } catch { toast("本机记录未能保存，原任务仍保留在服务器。"); }
        }
        state.blocked = false; clearPending();
      }
      // An audited failure is still the actual previous turn. Never substitute
      // a cached success when the server recorded a rejected interpretation.
      if (result?.run_id && result?.release_id) {state.previous=result; remember(result,task.question || "未完成查询",saved.display_label,saved);}
    }
    busy(false); scrollBottom(false, task.state === "completed" ? "answer" : "status", answerStart); refreshModelReadiness(); refreshAccount(true);
    return answerStart;
  }
  function retryKnownTaskButton(taskId) {
    return button("重试本次查询（计费）", async () => {
      if (state.busy || !state.ready) return;
      const unavailable = modelChatUnavailable();
      if (unavailable) { toast(unavailable); return; }
      busy(true);
      let task, parent = null;
      try {
        task = await request(`/v1/agent/tasks/${encodeURIComponent(taskId)}`);
        if (task.task_id !== taskId || task.state !== "failed" || task.blocked
          || typeof task.question !== "string" || !task.question.trim() || !Object.prototype.hasOwnProperty.call(modules,task.module)
          || typeof task.release_id !== "string" || typeof task.conversation_id !== "string") throw new Error("原任务尚未确认为可重试的失败。本次只检查了状态，没有重新调用模型。");
        if (task.previous_run_id) {
          const audit = await request(apiPath(`/v1/qa/runs/${encodeURIComponent(task.previous_run_id)}`, task.release_id));
          parent = verifiedStoredResult({run_id:task.previous_run_id,release_id:task.release_id},audit);
        }
      } catch (error) { busy(false); toast(error.message); return; }
      // Restore the original audited parent, rather than adding the failed
      // question to its own context and accidentally changing input identity.
      busy(false);
      await send(task.question, parent, null, {module: task.module, release_id: task.release_id, conversation_id: task.conversation_id});
    }, "text-action");
  }
  async function refreshModelReadiness() {
    if (state.modelChecking) return;
    state.modelChecking = true; updateComposer();
    try {
      if (!state.ready) {
        const [health, release] = await Promise.all([request("/health"), request("/v1/releases/active")]);
        if (health.status !== "ok" || !release.id) throw new Error("资料服务尚未就绪。");
        state.release = release;
      }
      const status=await request("/v1/agent/status"); state.modelReady=status.web_model?.ready === true;
      composerModel = typeof status.model?.model_name === "string" && status.model.model_name
        ? {protocol:status.model.provider, model:status.model.model_name} : null;
      const capacity = status.limits?.context_tokens;
      if (Number.isSafeInteger(capacity) && capacity > 0) contextCapacity = capacity;
      const reason = typeof status.web_model?.reason === "string" ? status.web_model.reason.slice(0, 400) : null;
      const modelReason = typeof status.model?.reason === "string" ? status.model.reason.slice(0, 400) : null;
      state.modelAvailability = {reason, modelReason};
      state.serviceFailure = null;
      state.ready = true;
      $("connection").dataset.state = "ready"; $("connection-label").textContent = "资料已连接";
      $("question").placeholder = "输入问题，或接着上一轮继续问…";
    } catch (error) {
      state.ready = false; state.modelReady = false; state.modelAvailability = null;
      state.serviceFailure = error.httpStatus === 503
        ? "网页已连接，资料库暂不可用。请在 Mac 上重新运行启动器。"
        : "工作台服务未连接。请检查 Mac 是否已启动工作台，手机需连接同一 Wi-Fi。";
      $("connection").dataset.state = "error"; $("connection-label").textContent = "服务未连接";
      $("question").placeholder = "本地服务未连接，点击刷新状态后重试…";
    }
    finally {state.modelChecking = false; updateComposer(); renderHistory();}
  }
  async function resumePending(saved = null) {
    const recheck = saved !== null;
    if (!recheck) {
      try {saved=JSON.parse(localStorage.getItem(pendingKey)||"null");} catch {return;}
    }
    if (!saved || typeof saved.task_id!=="string" || typeof saved.conversation_id!=="string") return;
    const followRecovered = followLatest, initialReadingRevision = readingRevision;
    if (recheck) $("messages").replaceChildren();
    taskRecovery = null;
    saveDraft();
    const originalDraft = saved.draft_scope === "new" || saved.draft_scope === `session:${saved.session_id}` ? saved.draft_scope : `pending:${saved.task_id}`;
    restoreDraft(originalDraft, modules[saved.module] ? saved.module : "auto");
    const restoringDraftRevision = draftRevision;
    state.pending=saved;state.conversationId=saved.conversation_id;
    const candidateSession=state.sessions.find((s)=>s.id===saved.session_id);
    state.session=null; state.previous=null; state.historyIncomplete=false; state.historyReadOnly=false; state.historyLoading=false; state.taskPhase="checking";
    $("backend").value="deepseek";$("module").value=modules[saved.module]?saved.module:"auto";$("welcome").hidden=true;
    const container=node("div","task-progress","正在恢复原任务状态…");$("messages").append(container);busy(true);
    const questionCard=node("article","message user-message");questionCard.setAttribute("aria-label","你的问题");
    const savedQuestion=saved.display_label || saved.question;
    questionCard.hidden=typeof savedQuestion!=="string" || !savedQuestion;
    if (!questionCard.hidden) questionCard.append(node("div","user-content",savedQuestion));
    container.before(questionCard);
    try {
      const task=await request(`/v1/agent/tasks/${encodeURIComponent(saved.task_id)}`);
      validateTask(saved, task, true);
      const matchingSession = candidateSession?.conversation_id === task.conversation_id && candidateSession?.module === task.module
        && candidateSession?.turns.every((ref) => ref.release_id === task.release_id);
      const foreignDraft = (candidateSession && draftScope === `session:${candidateSession.id}` && !matchingSession)
        || ["conversation_id", "module", "release_id"].some(key => saved[key] && task[key] && saved[key] !== task[key]);
      if (foreignDraft) {
        // Preserve the draft in its original scope before adopting the task's
        // authoritative identity. Never migrate another conversation's text.
        saveDraft(); restoreDraft(`pending:${saved.task_id}`, modules[task.module] ? task.module : "auto");
        toast("原任务属于另一段对话，草稿已保留在原会话。");
      }
      saved={...saved, ...(modules[task.module]?{module:task.module}:{}), ...(typeof task.release_id==="string"?{release_id:task.release_id}:{}), ...(typeof task.conversation_id==="string"?{conversation_id:task.conversation_id}:{})};
      state.pending=saved; state.conversationId=saved.conversation_id;
      $("module").value=modules[saved.module]?saved.module:"auto";
      const sameOrigin=candidateSession?.turns.length && matchingSession
        && typeof task.previous_run_id==="string" && candidateSession.turns.some((ref)=>ref.run_id===task.previous_run_id);
      let recoveringHistory = Promise.resolve(), historyStillLoading = false;
      if (sameOrigin) {
        // The task's authoritative identity may retain the original session's
        // references, while missing audits still prevent a new continuation.
        state.session=candidateSession; state.historyIncomplete=true; state.historyReadOnly=true;
        // Restore only editable text now that the task's session identity is
        // confirmed. Query continuation stays locked until its audits load.
        const originalSessionDraft=`session:${candidateSession.id}`;
        if (draftScope!==originalSessionDraft) {
          if ($("question").value || draftRevision!==restoringDraftRevision) {draftScope=originalSessionDraft;saveDraft();}
          else restoreDraft(originalSessionDraft,candidateSession.module);
        }
        const prefix=node("section","pending-history"); questionCard.before(prefix);
        const notice=node("div","task-progress history-recovery-status"); notice.setAttribute("role","status"); prefix.append(notice);
        const rows=candidateSession.turns.filter((ref)=>ref.run_id!==task.response?.run_id).map((ref,index)=>{
          const slot=node("section","history-turn-slot"); slot.dataset.turn=String(index+1); prefix.append(slot);
          return {ref,slot,status:"loading",result:null};
        });
        const current=()=>prefix.isConnected && state.session?.id===candidateSession.id;
        const updateHistory=()=>{
          if (!current()) return;
          const loading=rows.some((row)=>row.status==="loading"), missing=rows.filter((row)=>row.status==="error").length;
          state.historyIncomplete=loading || missing>0;
          state.historyReadOnly=state.historyIncomplete || !rows.every((row)=>isModelRun(row.result));
          notice.hidden=!state.historyReadOnly; notice.replaceChildren();
          if (loading) notice.append(node("strong",null,"正在恢复前轮对话…"),node("p",null,"当前任务继续检查，前轮记录单独读取。"));
          else if (missing) notice.append(node("strong",null,"前轮记录尚未读全"),node("p",null,"已保留当前任务、其他回答和草稿。补齐记录后才能继续追问。"));
          else if (state.historyReadOnly) notice.append(node("strong",null,"前轮记录 · 只读"),node("p",null,"前轮模型来源尚未确认，可保留当前记录查阅。"));
          if (!state.historyReadOnly) {
            const verifiedScope=`session:${candidateSession.id}`;
            if (draftScope!==verifiedScope) {
              if ($("question").value || draftRevision!==restoringDraftRevision) {draftScope=verifiedScope;saveDraft();}
              else restoreDraft(verifiedScope,candidateSession.module);
            }
            if (!state.previous) state.previous=rows.find((row)=>row.ref.run_id===task.previous_run_id)?.result || null;
          }
          updateComposer(); renderHistory();
          if (state.pending?.task_id === saved.task_id) scrollBottom(false);
        };
        const readHistoryTurn=async(row,index)=>{
          if (!current() || row.reading) return;
          row.reading=true;row.status="loading";row.slot.replaceChildren(node("div","pending-message",`正在读取第 ${index+1} 轮已保存记录…`));updateHistory();
          try {
            const audit=await request(apiPath(`/v1/qa/runs/${encodeURIComponent(row.ref.run_id)}`,row.ref.release_id));
            if (!current()) return;
            row.slot.replaceChildren(); row.result=renderStoredTurn(row.ref,audit,row.slot);row.status="ready";
          } catch {
            if (!current()) return;
            row.status="error";
            const error=node("div","message error-message history-turn-error");error.setAttribute("role","alert");
            error.append(node("strong",null,`第 ${index+1} 轮记录暂未恢复`),node("p",null,"读取失败不代表原查询失败。此处只重读原版本记录。"),button("重新读取这一轮",()=>readHistoryTurn(row,index),"text-action"));row.slot.replaceChildren(error);
          } finally {row.reading=false;updateHistory();}
        };
        updateHistory();
        historyStillLoading = true;
        recoveringHistory = Promise.allSettled(rows.map(readHistoryTurn)).finally(() => { historyStillLoading = false; });
      }
      if(task.question) {questionCard.replaceChildren(node("div","user-content",saved.display_label||task.question));questionCard.hidden=false;}
      let needsAlignment = task.terminal;
      const delivery = followTask(saved, container, task).then((answer) => {
        needsAlignment ||= historyStillLoading;
        return answer;
      });
      const [answerStart] = await Promise.all([delivery, recoveringHistory]);
      // Restoring older turns can move the delivered answer after it renders.
      // Re-align only if the reader has stayed with the original task.
      if (sameOrigin && needsAlignment && followRecovered && readingRevision === initialReadingRevision && state.view === "chat" && !$("detail-dialog").open
        && answerStart?.isConnected && !state.pending && state.previous === renderedResults.get(answerStart)) {
        scrollBottom(true, null, answerStart);
      }
    } catch(error) {taskConnectionLost(saved,container,error);}
  }
  function renderAnswer(result, target = $("messages"), readingOnly = false) {
    const article = node("article", "message assistant-message"); article.setAttribute("aria-label", "档案回答");
    article.dataset.module = result.module || "auto";
    renderedResults.set(article, result);
    const heading = node("div", "assistant-heading");
    const identity = node("div", "answer-identity"); identity.append(node("span", "answer-turn-label", "回答"), node("strong", null, "方舟档案"));
    heading.append(node("span", "assistant-emblem", "A"), identity, node("span", "response-status", statusLabels[result.status] || "查询结果")); article.append(heading);
    const hasCandidates = result.status === "needs_clarification" && Array.isArray(result.candidates) && result.candidates.length;
    const displayText = displayAnswerText(result);
    const presentation = garrisonReadingPresentation(result, displayText);
    if (presentation) { article.classList.add("answer-with-context"); article.append(answerContext(presentation)); }
    const bodyText = presentation ? presentation.body : displayText;
    const layout = makeAnswerLayout(result, bodyText, hasCandidates, presentation);
    const markerIds = citationMarkers(result);
    const answer = answerContent(hasCandidates ? bodyText.split("\n")[0] : bodyText, {markerIds});
    if (layout.prose || layout.groups) {
      if (layout.prose) {
        article.append(answerContent(layout.prose, {markerIds}));
        if (answerText(result.follow_up_question)) article.append(answerContent(result.follow_up_question, {markerIds}));
      }
      if (layout.summary) article.append(renderAnswerCard(layout.summary, {kind: "lead"}, result));
      for (const group of layout.groups || []) article.append(renderAnswerGroup(group, result));
      const analysisScope = layout.prose && result.source_authority?.literal_observations_source_verified ? ["分析依据为所引资料，未实战验证。"] : [];
      const limits = [...new Set([...answerArray(result.limitations), ...answerArray(result.warnings), ...analysisScope]
        .filter(answerText).map(value => value.split("\n").map(line => withoutCitationMarkers(line, markerIds)).join("\n").trim()).filter(Boolean))];
      if (limits.length) {
        const list = node("ul", "structured-answer-limits");
        for (const value of limits) list.append(node("li", null, value));
        article.append(answerDisclosure("适用范围与未核实内容", list));
      }
      if (layout.original) article.append(answerDisclosure("查看完整回答", answer));
    } else article.append(answer);
    if (hasCandidates) {
      const original = node("details", "detail-disclosure"); original.append(node("summary", null, "查看原始查询回答"), node("div", "answer-text", result.answer)); article.append(original);
    }
    if (!layout.groups && !layout.prose && !hasCandidates && bodyText.length > 1800) {
      answer.id = `answer-reading-${++answerSequence}`;
      const pauseFollowing = () => {
        if (readingOnly) return;
        scrollGeneration++; automaticScroll = false; followLatest = false;
        updateLatestControl();
      };
      const setExpanded = (expanded) => {
        answer.classList.toggle("long-answer", !expanded);
        expand.textContent = expanded ? "收起回答 ↑" : "展开完整回答 ↓";
        expand.setAttribute("aria-expanded", String(expanded));
      };
      const expand = button("展开完整回答 ↓", () => {
        pauseFollowing();
        const expanding = answer.classList.contains("long-answer");
        setExpanded(expanding);
        if (expanding) {
          // Start reading at the answer, instead of following the button to its
          // new position below a potentially very long block of text.
          answer.setAttribute("tabindex", "-1");
          answer.focus({preventScroll: true});
          const area = $(readingOnly ? "saved-view" : "conversation-scroll");
          if (answer.getBoundingClientRect && area.getBoundingClientRect) {
            const top = answer.getBoundingClientRect().top - area.getBoundingClientRect().top + area.scrollTop;
            area.scrollTop = Math.max(0, top - 14);
          } else answer.scrollIntoView?.({block: "start", behavior: "auto"});
        } else {
          expand.focus({preventScroll: true});
          expand.scrollIntoView?.({block: "nearest", behavior: "auto"});
        }
      }, "answer-expand");
      expand.setAttribute("aria-controls", answer.id);
      setExpanded(false);
      // A clipped link must never receive invisible keyboard focus. Expanding
      // on focus keeps the original text, links and their order intact.
      answer.addEventListener("focusin", (event) => {
        if (!answer.classList.contains("long-answer")) return;
        pauseFollowing(); setExpanded(true);
        event.target.scrollIntoView?.({block: "nearest", behavior: "auto"});
      });
      article.append(expand);
    }
    if (presentation?.note) {
      const boundary = node("section", "answer-boundary"); boundary.setAttribute("aria-label", "核验说明");
      boundary.append(node("strong", "answer-boundary-label", "核验说明"), node("p", null, presentation.note)); article.append(boundary);
    }
    if (presentation) article.append(answerProvenance(presentation));
    const catalog = layout.groups?.find(group => group.pagination)?.pagination || result.catalog;
    if (!readingOnly && catalog?.has_more === true && Number.isInteger(catalog.offset) && Number.isInteger(catalog.count)
      && catalog.offset >= 0 && catalog.count > 0 && Number.isInteger(catalog.limit) && catalog.limit > 0) {
      const section = node("div", "answer-section");
      const next = catalog.offset + catalog.count;
      section.append(button("查看下一页 ↓", () => send(`继续刚才的目录查询，从第${next + 1}条开始，最多显示${catalog.limit}条，保留原来的筛选范围。`, result), "text-action"));
      article.append(section);
    }
    if (layout.choices.length) {
      const choices = node("div", "answer-choice-sections");
      if (!readingOnly) choices.append(node("p", "choice-cost-note", "继续查询会产生 API 费用。"));
      for (const group of layout.choices) choices.append(renderAnswerGroup(readingOnly
        ? {...group, cards: group.cards.map(card => ({...card, action: null}))} : group, result));
      article.append(choices);
    }
    const conditions = result.dialogue?.user_reported_conditions;
    if (Array.isArray(conditions) && conditions.length) {
      const box = node("div", "conditions"); box.append(node("strong", null, "本轮条件（用户自述）"));
      for (const condition of conditions) {
        if (!condition.raw_text && structuredValue(condition.value)) {
          disclosure(box, condition.name || "查看条件详情", condition.value);
        } else {
          box.append(node("div", null, condition.raw_text || `${condition.name || "条件"}：${printable(condition.value)}`));
        }
      }
      article.append(box);
    }
    const actions = node("div", "source-actions");
    if (Array.isArray(result.citations) && result.citations.length) {
      const sources = button(`查看来源 · ${result.citations.length}`, () => openSources(result, {origin: sources}), "source-button"); sources.prepend(icon("source")); actions.append(sources);
    }
    actions.append(button("查看本轮记录", () => openAudit(result), "source-button quiet"));
    const copy = button("复制", async () => { try { await navigator.clipboard.writeText(copyReadingText(presentation ? presentation.copyText : displayText, markerIds)); toast("回答已复制。"); } catch { toast("无法访问剪贴板，请选择回答文字复制。"); } }, "source-button quiet"); copy.prepend(icon("copy")); actions.append(copy);
    const footer = node("footer", "answer-footer"); footer.append(actions);
    if (result.run_id && result.release_id) {
      const save = button("收藏", () => toggleSaved(result), "source-button answer-save");
      save.dataset.runId = result.run_id; save.dataset.releaseId = result.release_id; save.setAttribute("aria-pressed", "false");
      save.prepend(icon("bookmark")); actions.append(save);
    }
    if (!readingOnly) {
      const explore = button("继续探索", () => openExploration(result), "answer-explore");
      explore.hidden = true; footer.append(explore);
      const followup = button("继续追问", () => { if (article.isConnected) focusAnswerFollowup(answerFlowContext(result)); }, "answer-followup");
      followup.title = "定位输入框并保留草稿，不会发送问题；提问仍承接最新一轮。";
      footer.append(followup);
    }
    const verification = answerVerification(result);
    if (verification) article.append(verification);
    article.append(footer); target.append(article);
    syncSavedButtons();
    return article;
  }
  function verifiedStoredResult(ref, audit) {
    if (!ref || typeof ref.run_id !== "string" || typeof ref.release_id !== "string"
      || !audit || !audit.result || typeof audit.result.answer !== "string"
      || [audit.run_id, audit.result.run_id].some((id) => id !== undefined && id !== ref.run_id)
      || [audit.release_id, audit.result.release_id].some((id) => id !== undefined && id !== ref.release_id)) {
      throw new Error("历史记录与原查询编号或资料版本不一致，已保留原引用。");
    }
    return {...audit.result, run_id: ref.run_id, release_id: ref.release_id};
  }
  function renderStoredTurn(ref, audit, target = $("messages")) {
    const result = verifiedStoredResult(ref,audit);
    const turn = node("section", "stored-turn");
    renderedResults.set(turn, result);
    turn.dataset.runId = ref.run_id; turn.dataset.releaseId = ref.release_id;
    userMessage(ref.display_label || audit.question || "原查询问题未记录", turn);
    if (result.agent_runtime?.requested === "deepseek" && result.agent_runtime?.server_committed !== true) {
      const card = node("div");
      renderQueryFailure(card, result, result.agent_runtime.http_task?.task_id, result.agent_runtime.reason, result.agent_runtime.budget?.frozen === true);
      turn.append(card);
    } else renderAnswer(result, turn);
    target.append(turn);
    return result;
  }
  async function restoreSession(session) {
    if (state.pending || state.blocked) { toast("原任务结果尚待确认，请先检查原任务状态。"); return; }
    if (state.busy && !state.historyLoading) return;
    scrollGeneration++; automaticScroll=false;
    saveDraft(); restoreDraft(`session:${session.id}`, modules[session.module] ? session.module : "auto");
    followLatest = true; unseenContent = null;
    state.historyIncomplete = true; state.historyLoading = true;
    busy(true); switchView("chat"); $("welcome").hidden = true; $("messages").replaceChildren(); state.previous = null; state.session = null; state.historyReadOnly = true;
    const root = node("section", "history-recovery");
    const token = {}; $("messages").historyRestoreToken = token;
    $("messages").append(root);
    const current = () => root.isConnected && $("messages").historyRestoreToken === token;
    const notice = node("div", "task-progress history-recovery-status"); notice.setAttribute("role", "status");
    const rows = session.turns.map((ref, index) => {
      const slot = node("section", "history-turn-slot"); slot.dataset.turn = String(index + 1); root.append(slot);
      return {ref, slot, status: "loading", result: null};
    });
    root.append(notice);
    state.session = session; state.conversationId = session.conversation_id || session.id;
    $("backend").value = "deepseek"; $("module").value = modules[session.module] ? session.module : "auto";
    const updateRecovery = () => {
      if (!current()) return;
      const loading = rows.filter((row) => row.status === "loading").length;
      const missing = rows.filter((row) => row.status === "error").length;
      const complete = rows.length > 0 && !loading && !missing;
      state.historyLoading = loading > 0;
      state.historyIncomplete = loading > 0 || missing > 0;
      state.historyReadOnly = !complete || !rows.every((row) => isModelRun(row.result));
      state.previous = state.historyReadOnly ? null : rows.at(-1).result;
      notice.replaceChildren();
      notice.hidden = !state.historyReadOnly;
      if (loading) {
        notice.append(node("span", "pending-indicator"), node("strong", null, "正在恢复完整对话…"), node("p", null, `已读取 ${rows.filter((row) => row.status === "ready").length} / ${rows.length} 轮。此操作只读取已保存记录。`));
      } else if (missing) {
        notice.append(node("strong", null, `有 ${missing} 轮待恢复`), button("恢复完整对话", async () => {
          if (!current() || rows.some(row => row.reading)) return;
          await Promise.allSettled(rows.map((row, index) => row.status === "error" ? readTurn(row, index) : null));
          if (current()) await refreshModelReadiness();
        }, "text-action history-recover-all"));
      } else if (state.historyReadOnly) {
        notice.append(node("strong", null, "历史记录 · 只读"), node("p", null, "这段记录包含本地查询或未确认模型来源的回答，保留原记录供查阅。"), button("开启模型对话", () => newChat(), "text-action"));
      }
      busy(loading > 0);
    };
    const readTurn = async (row, index) => {
      if (!current() || row.reading) return;
      row.reading = true; row.status = "loading"; row.result = null;
      row.slot.replaceChildren();
      const progress = node("div", "task-progress"); progress.setAttribute("role", "status");
      progress.append(node("span", "pending-indicator"), node("span", null, `正在读取第 ${index + 1} 轮已保存记录…`)); row.slot.append(progress);
      updateRecovery();
      try {
        // Re-read the exact original release and run; this never submits a model task.
        const audit = await request(apiPath(`/v1/qa/runs/${encodeURIComponent(row.ref.run_id)}`, row.ref.release_id));
        if (!current()) return;
        row.slot.replaceChildren();
        row.result = renderStoredTurn(row.ref, audit, row.slot); row.status = "ready";
      } catch (error) {
        if (!current()) return;
        row.status = "error";
        const failure = node("div", "message error-message history-turn-error"); failure.setAttribute("role", "alert");
        const detail = error.httpStatus === 404 ? "原资料版本内暂未找到这轮记录。原引用仍保留，可再次核对。"
          : error.httpStatus === 409 ? "这轮记录未通过编号、版本或完整性校验。已保留原引用。"
          : "暂时无法读取这轮已保存记录。此处的读取失败不代表原查询执行失败。";
        failure.append(node("strong", null, `第 ${index + 1} 轮记录暂未恢复`), node("p", null, detail), button("重新读取这一轮", () => readTurn(row, index), "text-action"));
        row.slot.replaceChildren(failure);
      } finally {
        row.reading = false;
        updateRecovery();
      }
    };
    updateRecovery();
    await Promise.allSettled(rows.map((row, index) => readTurn(row, index)));
    if (current() && state.view === "chat") scrollBottom(false, "status", rows.at(-1)?.slot.querySelector(".assistant-message"));
  }
  function openDetail(title) {
    sourceReturnContext = null;
    state.drawerToken++; $("detail-title").textContent = title; $("detail-body").replaceChildren();
    if (!$("detail-dialog").open) $("detail-dialog").showModal();
    $("detail-body").scrollTop = 0; $("close-detail").focus(); return state.drawerToken;
  }
  function meta(container, pairs) {
    const dl = node("dl", "detail-meta");
    for (const [key, value] of pairs) {
      if (value === undefined || value === null || value === "") continue;
      const row = node("div"); row.append(node("dt", null, key), node("dd", null, printable(value))); dl.append(row);
    } container.append(dl);
  }
  function externalLink(container, href, text) {
    try { const url = new URL(href); if (!["http:", "https:"].includes(url.protocol)) return; const link = node("a", "detail-link", text); link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer"; container.append(link); } catch { /* No unsafe or malformed links. */ }
  }
  function disclosure(container, title, data) {
    const details = node("details", "detail-disclosure"); details.append(node("summary", null, title), node("pre", "detail-json", printable(data))); container.append(details);
  }
  function sourceReaderDisclosure(container, title) {
    const details = node("details", "detail-disclosure source-reader-disclosure");
    details.dataset.sourceReaderKey = title;
    const body = node("div", "source-reader-disclosure-body");
    details.append(node("summary", null, title), body); container.append(details);
    return body;
  }
  function sourceReaderQuote(container, value, className = "") {
    if (structuredValue(value)) {
      const highlights = sourceHighlights(value);
      if (highlights.length) appendSourceHighlights(container, highlights);
      sourceReaderData(sourceReaderDisclosure(container, "查看原始字段值"), value);
      return;
    }
    const quote = node("blockquote", `source-quote source-reader-quote${className ? ` ${className}` : ""}`, printable(value));
    container.append(quote);
  }
  function sourceReaderData(container, value) {
    const data = node("pre", "detail-json", printable(value)); data.tabIndex = 0; container.append(data);
  }
  function sourceReaderCapture(container, result, citation, parts) {
    return {owner: result, runId: result.run_id, releaseId: result.release_id, citation,
      scrollTop: container.scrollTop, parts,
      sections: [...container.querySelectorAll(".source-reader-disclosure")].map((section) => ({
        key: section.dataset.sourceReaderKey || section.children[0]?.textContent,
        open: !!section.open, rawScroll: [...section.querySelectorAll(".detail-json")].map((raw) => raw.scrollTop),
      }))};
  }
  function sourceReaderRestore(container, saved, token) {
    if (!saved) return {apply() {}, finish() {}};
    const restored = new WeakSet(), interaction = new AbortController();
    let touched = false;
    for (const event of ["wheel", "touchstart", "pointerdown", "keydown"]) {
      container.addEventListener(event, () => { touched = true; }, {passive: true, signal: interaction.signal});
    }
    const apply = () => {
      if (token !== state.drawerToken || !container.isConnected) return;
      for (const section of container.querySelectorAll(".source-reader-disclosure")) {
        if (restored.has(section)) continue;
        const key = section.dataset.sourceReaderKey || section.children[0]?.textContent;
        const previous = saved.sections.find((row) => row.key === key);
        if (previous) {
          section.open = previous.open;
          section.querySelectorAll(".detail-json").forEach((raw, index) => { raw.scrollTop = previous.rawScroll[index] || 0; });
        }
        restored.add(section);
      }
      if (!touched) container.scrollTop = saved.scrollTop;
    };
    return {apply, finish() { apply(); interaction.abort(); }};
  }
  const sourceAnswerJourneys = new WeakMap();
  function sourceJourneyCurrent(journey) {
    return !!journey && sourceAnswerJourneys.get(journey.owner) === journey
      && journey.runId === journey.owner.run_id && journey.releaseId === journey.owner.release_id
      && answerFlowCurrent(journey.context);
  }
  function sourceJourneyFor(result, reading) {
    if (reading?.sourceJourney) return sourceJourneyCurrent(reading.sourceJourney)
      && reading.sourceJourney.owner === result ? reading.sourceJourney : null;
    const context = answerFlowContext(result, reading?.origin);
    if (!answerFlowCurrent(context)) return null;
    const previous = sourceAnswerJourneys.get(result);
    const retain = sourceJourneyCurrent(previous);
    previous?.tracking?.abort(); previous?.restoreGuard?.abort();
    const journey = {owner: result, runId: result.run_id, releaseId: result.release_id, context,
      reading: retain ? {...previous.reading} : {}, comparisonOpen: retain && previous.comparisonOpen === true,
      positions: retain ? previous.positions : new Map()};
    sourceAnswerJourneys.set(result, journey);
    readingRevision++; scrollGeneration++; automaticScroll = false; followLatest = false;
    markAnswerReading(context); return journey;
  }
  function sourceJourneyActive(journey, token, root) {
    return token === state.drawerToken && root.isConnected && $("detail-dialog").open && sourceJourneyCurrent(journey);
  }
  function sourceJourneyRemember(journey, reading) {
    if (!sourceJourneyCurrent(journey)) return;
    journey.reading = {sourceQuery: typeof reading.sourceQuery === "string" ? reading.sourceQuery : "",
      sourceKind: reading.sourceKind, focusIndex: reading.focusIndex,
      listScrollTop: Number.isFinite(reading.listScrollTop) ? Math.max(0, reading.listScrollTop) : 0};
  }
  function sourceJourneyTrack(journey, root, token, capture) {
    journey.tracking?.abort(); journey.restoreGuard?.abort();
    const tracking = new AbortController(); journey.tracking = tracking;
    $("detail-body").addEventListener("scroll", () => { if (sourceJourneyActive(journey, token, root)) capture(); }, {passive: true, signal: tracking.signal});
    $("detail-dialog").addEventListener("close", () => {
      // The global close listener advances drawerToken first. The still-owned
      // root, rather than the old token, identifies this close's reading state.
      if (root.isConnected && sourceJourneyCurrent(journey)) { capture(); journey.restoreGuard?.abort(); }
      tracking.abort();
    }, {once: true, signal: tracking.signal});
  }
  function sourceAnswerComparison(journey) {
    const comparison = node("details", "source-answer-comparison"); comparison.open = journey.comparisonOpen === true;
    comparison.append(node("summary", null, "对照本轮回答"));
    const content = node("div", "source-answer-comparison-content"); content.tabIndex = 0;
    content.setAttribute("aria-label", "本轮完整回答");
    const text = displayAnswerText(journey.owner), presentation = garrisonReadingPresentation(journey.owner, text);
    if (presentation) content.append(answerContext(presentation));
    content.append(answerContent(presentation ? presentation.body : text, {markerIds: citationMarkers(journey.owner)}));
    if (presentation?.note) {
      const boundary = node("section", "answer-boundary"); boundary.setAttribute("aria-label", "核验说明");
      boundary.append(node("strong", "answer-boundary-label", "核验说明"), node("p", null, presentation.note)); content.append(boundary);
    }
    if (presentation) content.append(answerProvenance(presentation));
    comparison.append(content);
    comparison.addEventListener("toggle", () => {
      if (comparison.isConnected && $("detail-dialog").open && sourceJourneyCurrent(journey)) journey.comparisonOpen = !!comparison.open;
    });
    return comparison;
  }
  function sourceJourneyHeading(journey) {
    const heading = node("section", "source-answer-context");
    heading.append(node("p", "source-answer-turn", journey.context.saved ? "我的档案 · 回答来源" : `第 ${journey.context.rowNumber} 轮 · 回答来源`));
    const question = journey.context.question;
    if (question.length > 120) {
      const complete = node("details", "source-answer-question");
      complete.append(node("summary", null, `${question.slice(0, 96)}…`), node("p", null, question)); heading.append(complete);
    } else heading.append(node("p", "source-answer-question", question));
    heading.append(sourceAnswerComparison(journey));
    return heading;
  }
  function restoreSourceList(journey, token, root, container, reading, entries, search, remember) {
    const guard = new AbortController(), initialTop = container.scrollTop; journey.restoreGuard = guard;
    const current = () => sourceJourneyActive(journey, token, root);
    const cancel = () => { if (current()) guard.abort(); };
    for (const event of ["pointerdown", "keydown", "wheel", "input", "touchstart"]) {
      container.addEventListener(event, cancel, {passive: true, signal: guard.signal});
    }
    container.addEventListener("scroll", () => {
      if (Math.abs(container.scrollTop - initialTop) > 1) cancel();
    }, {passive: true, signal: guard.signal});
    requestAnimationFrame(() => {
      const apply = !guard.signal.aborted && current();
      // Remove interaction listeners before our own scroll/focus. Native scroll
      // events from this restoration must not count as a new reading gesture.
      guard.abort(); if (journey.restoreGuard === guard) journey.restoreGuard = null;
      if (!apply) return;
      container.scrollTop = Number.isFinite(reading.listScrollTop) ? Math.max(0, reading.listScrollTop) : 0;
      const selectedEntry = entries[reading.focusIndex];
      const entryRect = selectedEntry?.getBoundingClientRect?.(), viewport = container.getBoundingClientRect?.();
      if (selectedEntry && !selectedEntry.hidden && (!entryRect || !viewport || entryRect.top >= viewport.top && entryRect.bottom <= viewport.bottom)) {
        selectedEntry.focus({preventScroll: true});
      } else if (!matchMedia("(max-width:760px)").matches) search.focus({preventScroll: true});
      remember();
    });
  }
  function sourceJourneyActions(toolbar, journey, current, remember) {
    const actions = node("div", "source-answer-actions");
    if (journey.context.saved) {
      actions.append(button("返回收藏回答", () => {
        if (!current()) return;
        remember(); sourceReturnContext = null; $("detail-dialog").close(); restoreAnswerReading(journey.context);
      }, "text-action source-answer-return"));
      toolbar.append(actions); return;
    }
    actions.append(button("返回回答", () => {
      if (!current()) return;
      remember(); sourceReturnContext = null; $("detail-dialog").close(); restoreAnswerReading(journey.context);
    }, "text-action source-answer-return"),
      button("回到输入框", () => { if (current()) { remember(); focusAnswerFollowup(journey.context); } }, "text-action source-answer-followup"));
    const note = state.historyReadOnly ? "历史记录仅供回看" : state.historyIncomplete ? "记录尚未读全，草稿保持" : state.blocked || state.uncertain ? "结果待确认，草稿保持" : "";
    if (note) actions.append(node("p", "source-answer-note", note));
    toolbar.append(actions);
  }
  function openSources(result, reading = null) {
    const journey = sourceJourneyFor(result, reading); if (!journey) return;
    reading = {...journey.reading, ...reading, sourceJourney: journey};
    const token = openDetail("回答来源"); const container = $("detail-body");
    sourceReturnContext = journey.context;
    const introduction = node("div", "source-reader-list-heading");
    const current = () => sourceJourneyActive(journey, token, introduction);
    introduction.append(sourceJourneyHeading(journey));
    const allCitations = Array.isArray(result.citations) ? result.citations : [];
    const subset = Array.isArray(reading.citationSubset) ? new Set(reading.citationSubset) : null;
    const citations = subset ? allCitations.filter(citation => subset.has(citation)) : allCitations;
    const version = node("details", "source-list-version");
    version.append(node("summary", null, "本轮资料版本"), node("div", "source-release", `资料版本 / ${result.release_id}`)); introduction.append(version); container.append(introduction);
    if (result.run_id && result.release_id && citations.length) introduction.append(button("查看本轮记录", () => { if (current()) openAudit(result); }, "text-action"));
    const toolbar = node("nav", "source-reader-toolbar source-answer-toolbar"); toolbar.setAttribute("aria-label", "回答阅读导航");
    let remember = () => sourceJourneyRemember(journey, reading);
    sourceJourneyActions(toolbar, journey, current, () => remember()); container.append(toolbar);
    if (!citations.length) {
      const empty = node("div", "empty-source-state"); empty.append(node("h3", null, "本轮未附引用"));
      if (result.run_id && result.release_id) empty.append(button("查看本轮记录", () => { if (current()) openAudit(result); }, "text-action"));
      container.append(empty); return;
    }
    const kinds = citations.map((citation) => Object.prototype.hasOwnProperty.call(citation, "field_value") ? "field" : citation.quote !== undefined ? "quote" : "location");
    const kindLabels = {all: "全部", field: "字段值", quote: "原文", location: "定位"};
    let sourceKind = reading?.sourceKind === "all" || kinds.includes(reading?.sourceKind) ? reading.sourceKind : "all";
    const tools = node("section", "source-list-tools"); tools.setAttribute("aria-label", "查找本轮来源");
    const label = node("label", "source-list-search-label", "查找本轮来源");
    const search = node("input", "source-list-search"); search.type = "search";
    search.value = typeof reading?.sourceQuery === "string" ? reading.sourceQuery : "";
    search.placeholder = "名称、原文或网站"; search.setAttribute("aria-label", "查找本轮来源");
    label.append(search); tools.append(label);
    const filters = node("div", "source-list-filters"); filters.setAttribute("role", "group"); filters.setAttribute("aria-label", "来源类型");
    const filterButtons = [];
    const availableKinds = [...new Set(kinds)];
    if (availableKinds.length > 1) {
      for (const kind of ["all", "field", "quote", "location"].filter((key) => key === "all" || kinds.includes(key))) {
        const filter = button(kindLabels[kind], () => { if (current()) { sourceKind = kind; filterEntries(); } }, "source-filter");
        filter.dataset.kind = kind; filters.append(filter); filterButtons.push(filter);
      }
      tools.append(filters);
    }
    const count = node("p", "source-list-count"); count.setAttribute("role", "status"); count.setAttribute("aria-live", "polite"); tools.append(count);
    container.append(tools);
    const grid = node("div", "source-card-grid");
    const entries = [];
    const searchable = [];
    for (const [index, citation] of citations.entries()) {
      const entry = button("", () => {
        if (!current()) return;
        const navigation = {listScrollTop: container.scrollTop, focusIndex: index, sourceQuery: search.value, sourceKind, sourceJourney: journey, citationSubset: reading.citationSubset};
        sourceJourneyRemember(journey, navigation); return openCitation(citation, result, navigation);
      }, "source-list-entry source-card");
      const exact = citationValue(citation);
      const fact = citationFact(citation, result);
      const title = citationTitle(citation, index, result), site = citationSite(citation);
      const previewText = fact ? fact.text : structuredValue(exact)
        ? sourceHighlights(exact).filter(row => row.text.trim() !== title).map(row => row.text).join(" · ")
        : exact === undefined ? "" : printable(exact);
      const kind = kinds[index] === "field" ? "资料条目" : kinds[index] === "quote" ? "原文" : "来源链接";
      entry.setAttribute("aria-label", `查看第 ${index + 1} 条来源：${title}`);
      const top = node("span", "source-card-top"); top.append(node("strong", "source-card-title", title), icon("arrow"));
      entry.append(top, node("span", "source-card-meta", [kind, site].filter(Boolean).join(" · ")));
      if (previewText) {
        entry.append(node("span", "source-card-preview", previewText.length > 240 ? `${previewText.slice(0, 240)}…` : previewText));
      }
      grid.append(entry); entries.push(entry);
      searchable.push([title, site, citation.id, previewText, printable(exact), citation.source_url, citation.page_url, citation.pointer, citation.source_revision].filter((value) => value !== undefined && value !== null).join("\n").toLocaleLowerCase());
    }
    container.append(grid);
    const empty = node("div", "source-list-empty");
    empty.append(node("strong", null, "没有匹配的本轮来源"), button("清除筛选", () => { if (current()) { sourceKind = "all"; search.value = ""; filterEntries(); search.focus({preventScroll: true}); } }, "text-action"));
    container.append(empty);
    remember = () => sourceJourneyRemember(journey, {sourceQuery: search.value, sourceKind, listScrollTop: container.scrollTop, focusIndex: reading.focusIndex});
    function filterEntries() {
      if (!current()) return;
      const query = search.value.trim().toLocaleLowerCase(); let visible = 0;
      entries.forEach((entry, index) => {
        const matches = (sourceKind === "all" || kinds[index] === sourceKind) && searchable[index].includes(query);
        entry.hidden = !matches;
        if (matches) { entry.removeAttribute("tabindex"); visible++; }
        else entry.setAttribute("tabindex", "-1");
      });
      for (const filter of filterButtons) filter.setAttribute("aria-pressed", String(filter.dataset.kind === sourceKind));
      count.textContent = visible === citations.length ? `全部 ${citations.length} 条来源` : `显示 ${visible} / ${citations.length} 条来源`;
      empty.hidden = visible !== 0;
      remember();
    }
    search.addEventListener("input", filterEntries); filterEntries();
    sourceJourneyTrack(journey, introduction, token, remember);
    if (Number.isFinite(reading.listScrollTop) || reading.sourceQuery) restoreSourceList(journey, token, introduction, container, reading, entries, search, remember);
  }
  async function openCitation(citation, result, reading = null, options = null) {
    const journey = sourceJourneyFor(result, reading); if (!journey) return;
    const proposed = options?.restore;
    const saved = proposed?.owner === result && proposed.runId === result.run_id
      && proposed.releaseId === result.release_id && proposed.citation === citation ? proposed : null;
    const readingPosition = saved || journey.positions.get(citation);
    const retained = saved?.parts;
    const allCitations = Array.isArray(result.citations) ? result.citations : [];
    const subset = Array.isArray(reading?.citationSubset) ? new Set(reading.citationSubset) : null;
    const citations = subset ? allCitations.filter(item => subset.has(item)) : allCitations;
    const index = citations.indexOf(citation);
    const token = openDetail(citationTitle(citation, index, result)); const container = $("detail-body");
    sourceReturnContext = journey.context;
    const navigation = {...journey.reading, ...reading, sourceJourney: journey, focusIndex: index >= 0 ? index : reading?.focusIndex};
    sourceJourneyRemember(journey, navigation);
    container.append(sourceJourneyHeading(journey));
    const toolbar = node("nav", "source-reader-toolbar"); toolbar.setAttribute("aria-label", "来源阅读导航");
    const current = () => sourceJourneyActive(journey, token, toolbar);
    const remember = () => {
      sourceJourneyRemember(journey, navigation);
      // Presentation only: no evidence payload or DOM nodes are cached between
      // sources. A citation belongs to this exact answer/release journey.
      journey.positions.set(citation, sourceReaderCapture(container, result, citation));
    };
    const back = button("← 返回本轮来源", () => { if (current()) { remember(); openSources(result, navigation); } }, "back-to-sources"); toolbar.append(back);
    let previous = null, next = null;
    if (index >= 0 && citations.length > 1) {
      const pager = node("div", "source-reader-pager");
      previous = button("上一条来源", () => { if (current() && index > 0) { remember(); openCitation(citations[index - 1], result, navigation, {focus: "previous"}); } }, "text-action source-reader-step"); previous.disabled = index === 0;
      next = button("下一条来源", () => { if (current() && index < citations.length - 1) { remember(); openCitation(citations[index + 1], result, navigation, {focus: "next"}); } }, "text-action source-reader-step"); next.disabled = index === citations.length - 1;
      const position = node("span", "source-reader-position", `${index + 1} / ${citations.length}`); position.setAttribute("aria-label", `第 ${index + 1} 条来源，共 ${citations.length} 条`);
      pager.append(previous, position, next); toolbar.append(pager);
    }
    sourceJourneyActions(toolbar, journey, current, remember);
    container.append(toolbar);
    sourceJourneyTrack(journey, toolbar, token, remember);
    const primary = node("section", "source-reader-primary");
    const heading = node("div", "source-reader-heading");
    const exact = citationValue(citation);
    const fact = citationFact(citation, result);
    if (exact !== undefined) {
      heading.append(node("h3", "detail-subhead", fact?.label || "引用内容")); primary.append(heading);
      if (fact?.context) primary.append(node("p", "source-fact-context", fact.context));
      if (fact?.text) primary.append(node("p", "source-fact-value", fact.text));
      else if (!fact?.technical) sourceReaderQuote(primary, exact);
      if (fact && (fact.technical || printable(exact) !== fact.text)) sourceReaderData(sourceReaderDisclosure(primary, "查看原始字段值"), exact);
    }
    primary.hidden = exact === undefined;
    container.append(primary);
    let evidenceReady = retained?.evidenceReady === true, evidence = retained?.evidence;
    const evidenceSection = evidenceReady && retained.evidenceNode ? retained.evidenceNode : node("section", "source-reader-evidence");
    evidenceSection.hidden = !evidenceReady; container.append(evidenceSection);
    const actions = node("div", "source-reader-actions"); externalLink(actions, citation.source_url || citation.page_url, "访问来源页面 ↗"); container.append(actions);
    const technical = sourceReaderDisclosure(container, "来源定位与校验信息");
    meta(technical, [["资料版本", result.release_id], ["修订", citation.source_revision], ["JSON 定位", citation.pointer], ["字符起止", citation.start !== undefined ? `${citation.start} – ${citation.end}` : null], ["快照摘要", citation.snapshot_hash], ["字段摘要", citation.field_hash]]);
    const complete = sourceReaderDisclosure(container, "查看完整引用数据"); sourceReaderData(complete, citation);
    const pending = node("div", "source-drawer-state"); pending.dataset.state = "loading"; pending.setAttribute("role", "status"); pending.append(node("span", "pending-indicator"), node("span", null, "正在读取来源记录…")); container.append(pending);
    if (saved) { pending.tabIndex = -1; pending.setAttribute("aria-live", "polite"); }
    let snapshotId = citation.snapshot_id || (evidenceReady ? evidence?.snapshot_id : null);
    let snapshotReady = !!(retained?.snapshotNode && retained.snapshotId === snapshotId), snapshotNode = snapshotReady ? retained.snapshotNode : null;
    if (snapshotNode) container.append(snapshotNode);
    const restore = sourceReaderRestore(container, readingPosition, token);
    const parts = new Map(), errors = new Map();
    const capture = () => sourceReaderCapture(container, result, citation, {evidenceReady, evidence, evidenceNode: evidenceReady ? evidenceSection : null, snapshotId, snapshotNode: snapshotReady ? snapshotNode : null});
    const updatePending = () => {
      if (!current()) return;
      const loading = [...parts.values()].includes("loading");
      if (!loading && !errors.size) {
        if (saved) {
          // Keep the explicit retry's reading anchor connected. Removing a
          // focused status would send keyboard users back to the document.
          pending.className = "source-reader-retry-status"; pending.dataset.state = "ready";
          pending.setAttribute("role", "status"); pending.replaceChildren(node("span", null, "来源已重新读取"));
        } else pending.remove();
        restore.apply(); return;
      }
      if (errors.size) {
        const ownsFocus = document.activeElement === pending || [...pending.querySelectorAll(".text-action")].includes(document.activeElement);
        pending.dataset.state = "error"; pending.setAttribute("role", "alert");
        pending.replaceChildren(node("strong", null, "来源记录暂时无法读取"));
        for (const [part, error] of errors) {
          const message = error.httpStatus === 404 ? "未在本轮资料版本中找到这条来源记录，原引用仍保留。" : error.httpStatus === 409 ? "来源记录与本轮资料版本不一致，原引用仍保留。请核对来源信息。" : "暂时无法读取资料。检查连接后可重新读取，此操作不会重新提交问题。";
          pending.append(node("p", null, `${part === "evidence" ? "来源证据" : "原始快照"}：${message}`));
        }
        if (loading) pending.append(node("p", null, "其余资料仍在读取，已读取的内容保持显示。"));
        const retry = button("重新读取此来源", () => {
          if (!current()) return;
          return openCitation(citation, result, navigation, {restore: capture()});
        }, "text-action"); retry.disabled = loading; pending.append(retry);
        if (saved && ownsFocus && !retry.disabled) retry.focus({preventScroll: true});
      }
      restore.apply();
    };
    const readSnapshot = async (id) => {
      parts.set("snapshot", "loading"); updatePending();
      try {
        const snapshot = node("details", "detail-disclosure source-reader-disclosure source-reader-snapshot"); snapshot.dataset.sourceReaderKey = "固定原始快照与下载";
        const snapshotBody = node("div", "source-reader-disclosure-body"); snapshot.append(node("summary", null, "固定原始快照与下载"), snapshotBody);
        await appendSnapshot(snapshotBody, id, result.release_id, token);
        if (!current()) return;
        snapshotId = id; snapshotNode = snapshot; snapshotReady = true; container.append(snapshot); parts.set("snapshot", "ready");
      } catch (error) {
        if (!current()) return;
        parts.set("snapshot", "error"); errors.set("snapshot", error);
      } finally { updatePending(); }
    };
    const readEvidence = async () => {
      try {
        const data = await request(apiPath(`/v1/evidence/${encodeURIComponent(citation.evidence_id)}`, result.release_id));
        if (!current()) return;
        const received = data?.evidence;
        if (received === null || typeof received !== "object" || Array.isArray(received)) {
          const error = new Error("来源证据响应不完整，原引用仍保留。");
          error.code = "invalid_evidence_response"; throw error;
        }
        evidence = received;
        evidenceReady = true;
        evidenceSection.hidden = false;
        evidenceSection.append(node("h3", "detail-subhead", "来源证据"));
        if (evidence && Object.prototype.hasOwnProperty.call(evidence, "excerpt") && evidence.excerpt !== undefined
          && printable(evidence.excerpt) !== printable(exact)) {
          const excerpt = evidence.excerpt;
          if (structuredValue(excerpt)) {
            sourceReaderData(sourceReaderDisclosure(evidenceSection, "查看来源摘录（结构数据）"), excerpt);
          } else sourceReaderQuote(evidenceSection, excerpt, "source-reader-excerpt");
        }
        const fullEvidence = sourceReaderDisclosure(evidenceSection, "查看来源证据（可能包含完整文档）"); sourceReaderData(fullEvidence, evidence);
        parts.set("evidence", "ready");
        if (!citation.snapshot_id && evidence?.snapshot_id && !snapshotReady) { snapshotId = evidence.snapshot_id; await readSnapshot(snapshotId); }
      } catch (error) {
        if (!current()) return;
        parts.set("evidence", "error"); errors.set("evidence", error);
      } finally { updatePending(); }
    };
    if (citation.evidence_id && !evidenceReady) parts.set("evidence", "loading");
    if (snapshotId && !snapshotReady) parts.set("snapshot", "loading");
    restore.apply();
    if (saved) pending.focus({preventScroll: true});
    else if (options?.focus) {
      const preferred = options.focus === "next" ? next : previous, other = options.focus === "next" ? previous : next;
      (preferred && !preferred.disabled ? preferred : other && !other.disabled ? other : back).focus({preventScroll: true});
    }
    const jobs = [];
    if (citation.evidence_id && !evidenceReady) jobs.push(readEvidence());
    if (citation.snapshot_id && !snapshotReady) jobs.push(readSnapshot(citation.snapshot_id));
    else if (snapshotId && !snapshotReady && evidenceReady) jobs.push(readSnapshot(snapshotId));
    if (!jobs.length) updatePending();
    try { await Promise.allSettled(jobs); } finally { restore.finish(); }
  }
  async function appendSnapshot(container, id, releaseId, token) {
    const data = await request(apiPath(`/v1/snapshots/${encodeURIComponent(id)}`, releaseId)); if (token !== state.drawerToken) return;
    const snapshot = data.snapshot; container.append(node("h3", "detail-subhead", "固定原始快照"));
    meta(container, [["原始 URL", snapshot.url], ["修订", snapshot.revision], ["文件类型", snapshot.media_type], ["字节数", snapshot.byte_size], ["SHA-256", snapshot.content_hash]]);
    const download = node("a", "detail-link", "下载原始快照 ↓"); download.href = apiPath(`/v1/snapshots/${encodeURIComponent(id)}/raw`, releaseId); download.download = "snapshot.bin"; container.append(download);
  }
  function detailReadFailure(container, pending, error, retry) {
    pending.className="source-drawer-state"; pending.dataset.state="error"; pending.setAttribute("role","alert");
    const text=error.httpStatus===404 ? "原版本内暂未找到这条记录，原引用仍保留。" : error.httpStatus===409 ? "这条记录与原资料版本不一致，请核对原引用。" : error.code==="client_timeout" ? "读取记录超时。原查询状态不会因此改变，可以重新读取。" : "暂时无法读取记录。检查连接后可重新读取，不会重新提交问题。";
    pending.replaceChildren(node("strong",null,"记录暂时无法读取"),node("p",null,text),button("重新读取记录",retry,"text-action"));
    if (!pending.isConnected) container.append(pending);
  }
  async function openAudit(result) {
    const token = openDetail("本轮查询记录"); const container = $("detail-body"); const pending = node("p", "detail-description", "正在读取服务端保存的查询记录…"); container.append(pending);
    try {
      const audit = await request(apiPath(`/v1/qa/runs/${encodeURIComponent(result.run_id)}`, result.release_id)); if (token !== state.drawerToken) return; pending.remove();
      container.append(node("h3", "detail-subhead", "原始问题"), node("blockquote", "source-quote", audit.question));
      meta(container, [["查询 ID", result.run_id], ["任务 ID", audit.result?.agent_runtime?.http_task?.task_id], ["状态原因", audit.result?.agent_runtime?.reason], ["资料版本", result.release_id], ["保存时间", audit.created_at], ["响应摘要", audit.response_hash], ["查询方式", audit.result?.agent_runtime?.used || "local"], ["模型请求次数",audit.result?.agent_runtime?.model_calls]]);
      if (typeof audit.result?.answer === "string") disclosure(container, "查看原始完整回答", audit.result.answer);
      disclosure(container, "查看完整服务端查询记录", audit);
    } catch (error) { if (token === state.drawerToken) detailReadFailure(container,pending,error,()=>openAudit(result)); }
  }
  async function openTaskAudit(taskId, back) {
    const token=openDetail("本轮查询记录"); const container=$("detail-body");
    if (back) container.append(button("返回待确认记录", back, "text-action archived-task-back"));
    const pending=node("p","detail-description","正在读取任务记录…"); container.append(pending);
    try {
      const task=await request(`/v1/agent/tasks/${encodeURIComponent(taskId)}`);
      if (token!==state.drawerToken) return; pending.remove();
      if (task.question) container.append(node("h3","detail-subhead","原始问题"),node("blockquote","source-quote",task.question));
      meta(container,[["任务 ID",task.task_id],["任务状态",task.state],["状态原因",task.error_code || task.reason],["资料版本",task.release_id]]);
      if (task.response?.run_id) container.append(button("查看服务端查询记录",()=>openAudit(task.response),"text-action"));
      disclosure(container,"查看完整任务记录",task);
    } catch(error) {if(token===state.drawerToken)detailReadFailure(container,pending,error,()=>openTaskAudit(taskId, back));}
  }
  function openVersion() {
    openDetail("当前资料版本"); const container = $("detail-body");
    if (!state.release) { container.append(node("p", "detail-description", "尚未连接资料服务。请检查本地服务后刷新页面。")); return; }
    container.append(node("p", "detail-description", "查询基于固定社区资料与来源快照。此版本标识用于回溯，不代表当前线上游戏版本；追问沿用原回答版本。"));
    meta(container, [["资料名称", state.release.label], ["发布 ID", state.release.id], ["清单摘要", state.release.manifest_hash], ["发布时间", state.release.created_at], ["真实模型",state.modelReady?"服务端执行策略已就绪":"当前不可用"]]);
    const datasets = state.release.manifest?.datasets || {};
    for (const [name, data] of Object.entries(datasets)) { container.append(node("h3", "detail-subhead", modules[name] || name)); meta(container, [["数据集", data.dataset_id], ["版本", data.version], ["记录数", data.record_count]]); }
    disclosure(container, "查看原始发布清单", state.release.manifest);
  }
  function renderRecordSummary(container, record) {
    const value = record.data?.source_value ?? record.data;
    if (!value || typeof value !== "object") { sourceReaderQuote(container, value); return; }
    const rows = sourceHighlights(value).filter(row => row.label !== "名称" && !/[{}]/.test(row.text));
    if (rows.length) appendSourceHighlights(container, rows);
    if (record.kind === "operator") {
      const professions = {PIONEER: "先锋", WARRIOR: "近卫", TANK: "重装", SNIPER: "狙击", CASTER: "术师", MEDIC: "医疗", SUPPORT: "辅助", SPECIAL: "特种"};
      const rarity = /^TIER_([1-6])$/.exec(value.rarity || "");
      meta(container, [["职业", professions[value.profession]], ["星级", rarity ? `${rarity[1]} 星` : null], ["位置", {MELEE: "近战位", RANGED: "远程位", ALL: "全部位置"}[value.position]], ["标签", answerArray(value.tagList).join("、")]]);
      answerArray(value.phases).forEach((phase, index) => {
        const frame = answerArray(phase.attributesKeyFrames).at(-1);
        if (!frame?.data) return;
        const block = node("details", "detail-disclosure record-stat-block");
        block.append(node("summary", null, `${index ? `精英${index}` : "初始"} · 等级 ${frame.level}`));
        meta(block, [["生命", frame.data.maxHp], ["攻击", frame.data.atk], ["防御", frame.data.def], ["法术抗性", frame.data.magicResistance], ["部署费用", frame.data.cost], ["阻挡数", frame.data.blockCnt]]);
        container.append(block);
      });
    }
  }
  async function openRecord(record, releaseId) {
    const token = openDetail(record.name || "资料记录"); const container = $("detail-body");
    const pending = node("p", "detail-description", "正在读取版本内的记录…"); container.append(pending);
    try {
      const data = await request(apiPath(`/v1/records/${encodeURIComponent(record.id)}`, releaseId)); if (token !== state.drawerToken) return; pending.remove();
      record = data.record;
      const parentLabel = Object.prototype.hasOwnProperty.call(themeNames, record.parent_native_id) ? "主题" : "所属对象";
      meta(container, [["模块", modules[record.module] || "资料"], ["类型", recordKinds[record.kind] || "资料条目"], ["主题", themeNames[record.parent_native_id]]]);
      renderRecordSummary(container, record);
      const metadata = sourceReaderDisclosure(container, "资料标识");
      meta(metadata, [[parentLabel, record.parent_native_id], ["原始 ID", record.native_id], ["资料版本", releaseId]]);
      container.append(node("h3", "detail-subhead", `来源证据 · ${(record.evidence_ids || []).length}`));
      for (const [index, id] of (record.evidence_ids || []).entries()) {
        container.append(button(`查看证据 ${index + 1} ↗`, () => openEvidence(id, record, releaseId), "detail-link"));
      }
      disclosure(container, "查看原始记录内容", record.data);
    } catch (error) { if (token === state.drawerToken) detailReadFailure(container,pending,error,()=>openRecord(record,releaseId)); }
  }
  async function openEvidence(id, record, releaseId) {
    const token = openDetail("原始来源证据"); const container = $("detail-body"); container.append(button("← 返回资料记录", () => openRecord(record, releaseId), "back-to-sources"));
    const pending = node("p", "detail-description", "正在读取证据…"); container.append(pending);
    try {
      const {evidence} = await request(apiPath(`/v1/evidence/${encodeURIComponent(id)}`, releaseId)); if (token !== state.drawerToken) return; pending.remove();
      if (evidence.excerpt !== undefined) sourceReaderQuote(container, evidence.excerpt);
      const technical = sourceReaderDisclosure(container, "来源定位与校验信息");
      meta(technical, [["资料版本", releaseId], ["原文位置", evidence.pointer], ["修订", evidence.source_revision], ["摘录摘要", evidence.excerpt_hash]]);
      externalLink(container, evidence.source_url, "访问来源页面 ↗");
      if (evidence.snapshot_id) await appendSnapshot(container, evidence.snapshot_id, releaseId, token);
    } catch (error) { if (token === state.drawerToken) detailReadFailure(container,pending,error,()=>openEvidence(id,record,releaseId)); }
  }
  function libraryQueryLabel(query, module) {
    return `${modules[module] || "全部资料"} · ${query ? `“${query}”` : "全部记录"}`;
  }
  function updateLibrarySummary(container) {
    container.querySelectorAll(".library-query-summary").forEach((summary) => summary.remove());
    const current = state.library;
    if (!current?.releaseId) return;
    const summary = node("div", "library-query-summary result-count");
    summary.append(node("strong", null, libraryQueryLabel(current.query, current.module)), node("span", null, `已显示 ${current.offset} 条`));
    container.prepend(summary);
  }
  async function searchLibrary(more = false, retry = null) {
    if (!state.ready) { toast("资料服务未连接，请先刷新连接状态。"); return; }
    if (state.library?.busy) { toast("正在检索资料，请等待完成后再提交；输入内容已保留。"); return; }
    const query = retry ? retry.query : more ? state.library?.query : $("library-query").value.trim();
    const module = retry ? retry.module : more ? state.library?.module : $("library-module").value;
    const offset = retry ? retry.offset : more ? state.library?.offset : 0;
    const releaseId = retry ? retry.releaseId : more ? state.library?.releaseId : state.release.id;
    more = retry ? retry.more : more;
    if (more && (!state.library?.releaseId || state.library.query !== query || state.library.module !== module || state.library.releaseId !== releaseId || state.library.offset !== offset)) {
      toast("检索范围已改变，请从当前结果继续加载。"); return;
    }
    const requested = {query, module, offset, releaseId, more};
    const container = $("library-results");
    container.querySelectorAll(".library-feedback").forEach((feedback) => feedback.remove());
    state.library ||= {busy: false, hasMore: false};
    state.library.busy = true; $("load-more").hidden = true;
    const pending = node("div", "library-feedback"); pending.dataset.state = "loading"; pending.setAttribute("role", "status");
    pending.append(node("span", "pending-indicator"), node("span", null, `${more ? "正在继续加载" : "正在检索"}：${libraryQueryLabel(query, module)}…`)); container.append(pending);
    try {
      const data = await request(apiPath("/v1/records", releaseId, {...(module ? {module} : {}), ...(query ? {name: query} : {}), limit: 20, offset}));
      if (!Array.isArray(data.items)) throw new Error("资料列表格式无法读取。");
      pending.remove();
      if (!more) container.replaceChildren();
      if (!data.items.length && !more) container.append(node("p", "detail-description", "这个版本没有找到同名记录。可以缩短名称，或切换模块再查。"));
      data.items.forEach((record, index) => {
        const row = button("", () => openRecord(record, data.release_id || releaseId), "record-row record-card");
        row.dataset.module = record.module || module;
        const scope = [modules[record.module], themeNames[record.parent_native_id]].filter(Boolean).join(" · ");
        const top = node("div", "record-card-top");
        top.append(node("span", "record-card-kind", recordKinds[record.kind] || "资料条目"), node("span", "record-card-index", String(offset + index + 1).padStart(2, "0")));
        const body = node("div", "record-card-body");
        body.append(node("strong", "record-card-name", record.name || "未命名记录"), node("span", "record-card-scope", scope));
        const action = node("div", "record-card-action"); action.append(node("span", null, "查看资料"), icon("arrow"));
        row.append(top, body, action); container.append(row);
      });
      state.library = {...requested, offset: offset + data.items.length, hasMore: data.items.length === 20, busy: true};
      updateLibrarySummary(container);
    } catch (error) {
      pending.dataset.state = "error"; pending.setAttribute("role", "alert");
      const message = error.httpStatus === 409 ? "资料版本不一致，可按原条件重新读取。" : error.httpStatus === 404 ? "未找到这次检索的资料记录，可按原条件重新读取。" : "暂时无法读取资料，可按原条件重新读取。";
      pending.replaceChildren(node("strong", null, more ? "继续加载未完成" : "本次检索未完成"), node("p", null, `${libraryQueryLabel(query, module)}。${message}${state.library.releaseId ? " 已保留上次成功检索的结果。" : ""}`), button(more ? "重新加载这一页" : "按原条件重试", () => searchLibrary(more, requested), "text-action"));
    } finally {
      state.library.busy = false; $("load-more").hidden = !state.library.hasMore;
    }
  }

  viewControls().forEach((b) => b.addEventListener("click", () => switchView(b.dataset.view)));
  const tour = $("workbench-tour");
  const tourSteps = [
    {target: () => $("question"), title: "直接问，接着问", description: "试着问「温蒂有哪些公招标签？」看完回答，可以查看来源或继续追问。"},
    {target: () => document.querySelector(".module-picker"), title: "选定查询范围", description: "平时用「自动识别」就好。只想查某个玩法时，可以在这里选「集成战略」或「卫戍协议」。"},
    {target: () => $("context-meter"), title: "查看上下文用量", description: "点发送键旁的圆环，查看最近一次模型调用用了多少上下文，以及可用上限。"},
    {target: () => $("mobile-menu").getClientRects().length ? $("mobile-menu") : document.querySelector('button[data-view="tools"]'),
      title: "打开战术工具", description: "想凑盟约或算公招标签，可以从导航打开配队沙盘和公招助手。"},
    {target: () => $("open-account"), title: "查看账户用量", description: "这里能看本机的调用次数和预估费用。服务商支持时，还会显示余额。"},
    {target: () => $("composer-model"), title: "选择模型", description: "点模型名切换服务。在部署电脑上，还可以从这里添加自己的 API 密钥。"}
  ];
  let tourStep = 0, tourFromHelp = false, tourObserver = null;
  function positionTour() {
    if (!tour.open) return;
    const card = $("tour-card"), spot = $("tour-spot"), anchor = tourSteps[tourStep].target();
    const bounds = tour.getBoundingClientRect(), target = anchor.getBoundingClientRect();
    const w = bounds.width, h = bounds.height, gap = 12;
    const clamp = (v, min, max) => Math.max(min, Math.min(max, v));
    const x = clamp(target.left - bounds.left - 4, 1, w - 1);
    const y = clamp(target.top - bounds.top - 4, 1, h - 1);
    const right = clamp(target.right - bounds.left + 4, x, w - 1);
    const bottom = clamp(target.bottom - bounds.top + 4, y, h - 1);
    Object.assign(spot.style, {left: x + "px", top: y + "px", width: right - x + "px", height: bottom - y + "px"});
    const below = h - bottom - 2 * gap, above = y - 2 * gap;
    const cardWidth = card.offsetWidth;
    let left = clamp(x, gap, w - cardWidth - gap), top;
    // Use the side of the anchor in a short landscape viewport.
    if (Math.max(above, below) < card.scrollHeight && Math.max(x, w - right) >= cardWidth + 2 * gap) {
      card.style.maxHeight = (h - 2 * gap) + "px";
      left = w - right >= cardWidth + 2 * gap ? right + gap : x - gap - cardWidth;
      top = clamp(y, gap, h - card.offsetHeight - gap);
    } else {
      const useBelow = below >= card.scrollHeight || below >= above;
      card.style.maxHeight = Math.max(0, useBelow ? below : above) + "px";
      top = useBelow ? bottom + gap : y - gap - card.offsetHeight;
    }
    card.style.left = left + "px"; card.style.top = top + "px";
  }
  function renderTour() {
    const step = tourSteps[tourStep];
    $("tour-count").textContent = (tourStep + 1) + " / " + tourSteps.length;
    $("tour-title").textContent = step.title;
    $("tour-description").textContent = tourStep === tourSteps.length - 1 && !credentialInfo?.can_manage
      ? "点模型名切换部署电脑已连接的服务。" : step.description;
    $("tour-back").hidden = tourStep === 0;
    $("tour-next").textContent = tourStep < tourSteps.length - 1 ? "下一步"
      : credentialInfo?.can_manage && credentialInfo?.setup_required ? "配置模型" : "完成";
    $("tour-card").scrollTop = 0; positionTour();
  }
  function startTour(fromHelp = false) {
    if (document.querySelector("dialog[open]")) return;
    tourFromHelp = fromHelp; tourStep = 0;
    if (fromHelp) switchView("chat");
    tour.showModal(); renderTour();
    window.addEventListener("resize", positionTour);
    if (typeof ResizeObserver === "function") {
      tourObserver = new ResizeObserver(positionTour);
      for (const step of tourSteps) tourObserver.observe(step.target());
      tourObserver.observe(tour);
    }
    $("tour-next").focus({preventScroll:true});
  }
  function finishTour(configure = false) {
    if (!tour.open) return;
    tourObserver?.disconnect(); tourObserver = null;
    window.removeEventListener("resize", positionTour); tour.close();
    if (tourFromHelp && !configure) { switchView("help"); $("replay-tour").focus({preventScroll:true}); }
    if (credentialInfo?.tour_pending) {
      const info = credentialInfo;
      request("/v1/workbench/tour/dismiss", {method:"POST", mode:"same-origin", redirect:"error",
        headers:{"X-Tour-Nonce":info.tour_nonce}, keepalive:true}).then(() => { info.tour_pending = false; })
        .catch(() => toast("引导已关闭，但未能保存，下次打开可能再次显示。"));
    }
    if (configure) openCredential();
  }
  $("replay-tour").addEventListener("click", () => startTour(true));
  $("tour-skip").addEventListener("click", () => finishTour());
  tour.addEventListener("cancel", event => { event.preventDefault(); finishTour(); });
  $("tour-back").addEventListener("click", () => { if (tourStep > 0) { tourStep--; renderTour(); if (!tourStep) $("tour-next").focus(); } });
  $("tour-next").addEventListener("click", () => {
    if (tourStep < tourSteps.length - 1) { tourStep++; renderTour(); }
    else finishTour(!!(credentialInfo?.can_manage && credentialInfo?.setup_required));
  });
  document.querySelector(".skip-link").addEventListener("click", (event) => { event.preventDefault(); switchView("chat"); $("question").focus(); });
  $("new-chat").addEventListener("click", startNewChat);
  $("pending-new-chat").addEventListener("click", startNewChat);
  $("detach-context").addEventListener("click", () => newChat());
  $("mobile-menu").addEventListener("click", () => {
    $("sidebar").classList.add("nav-open"); $("sidebar").inert = false; $("workspace").inert = true;
    $("nav-backdrop").hidden = false; $("mobile-menu").setAttribute("aria-expanded", "true");
    const first = $("new-chat").disabled ? viewControls().find(button => button.dataset.view === "chat") : $("new-chat");
    first?.focus();
  });
  $("nav-backdrop").addEventListener("click", closeNav);
  document.addEventListener("keydown", (event) => {
    if (!$("sidebar").classList.contains("nav-open")) return;
    if (event.key === "Escape") { closeNav(); $("mobile-menu").focus(); }
    if (event.key === "Tab") {
      const controls = [...$("sidebar").querySelectorAll("a[href],button:not(:disabled),input:not(:disabled)")];
      const edge = event.shiftKey ? controls[0] : controls.at(-1);
      if (document.activeElement === edge) { event.preventDefault(); (event.shiftKey ? controls.at(-1) : controls[0]).focus(); }
    }
  });
  matchMedia("(max-width:760px)").addEventListener("change", (event) => { if (!event.matches) closeNav(); $("sidebar").inert = event.matches && !$("sidebar").classList.contains("nav-open"); updateComposer(); });
  if (matchMedia("(max-width:760px)").matches) $("sidebar").inert = true;
  $("question").addEventListener("input", () => { draftRevision++; saveDraft(); updateComposer(); });
  $("latest-draft").addEventListener("click", () => {
    if (state.busy || state.pending || state.blocked || state.historyIncomplete || state.historyReadOnly || !state.previous) return;
    draftParent = null; saveDraft(); updateComposer(); $("question").focus({preventScroll: true});
  });
  $("question").addEventListener("compositionstart", () => { composing = true; });
  $("question").addEventListener("compositionend", () => { composing = false; });
  $("conversation-scroll").addEventListener("scroll", () => {
    if (automaticScroll) return;
    const area = $("conversation-scroll"); followLatest = area.scrollHeight - area.scrollTop - area.clientHeight <= 80;
    if (followLatest) { unseenContent = null; unseenAnswer = null; }
    updateLatestControl();
  });
  const stopFollowingScroll = () => {
    readingRevision++;
    if (!automaticScroll && !followLatest) return;
    scrollGeneration++; automaticScroll = false; followLatest = false; updateLatestControl();
  };
  $("conversation-scroll").addEventListener("wheel", (event) => { if (event.deltaY < 0) stopFollowingScroll(); }, {passive:true});
  $("conversation-scroll").addEventListener("touchstart", () => { automaticScroll=false; }, {passive:true});
  $("conversation-scroll").addEventListener("touchmove", stopFollowingScroll, {passive:true});
  $("conversation-scroll").addEventListener("pointerdown", () => {
    // A queued history-alignment frame must not move a control between press
    // and click. Resume automatic following only through the latest action.
    readingRevision++; scrollGeneration++; automaticScroll = false; followLatest = false;
  });
  $("conversation-scroll").addEventListener("keydown", (event) => { if (["ArrowUp","PageUp","Home"].includes(event.key)) stopFollowingScroll(); });
  $("jump-latest").addEventListener("click", () => { readingRevision++; readingTarget = null; updateSurface(); scrollBottom(true, null, unseenAnswer); });
  $("open-turn-index").addEventListener("click", openTurnIndex);
  $("reading-reference").addEventListener("click", openReadingReference);
  $("turn-index-search").addEventListener("input", (event) => { turnIndexQuery = event.target.value.slice(0, 160); syncConversationIndex(); });
  $("refresh-model").addEventListener("click", async () => { await refreshModelReadiness(); if (state.ready && !state.pending) await resumePending(); });
  $("recheck-task").addEventListener("click", () => { if (taskRecovery) return checkOriginalTask(taskRecovery.saved, taskRecovery.container); });
  $("model-status-details").addEventListener("click", openModelStatus);
  if ($("history-search")) $("history-search").addEventListener("input", (event) => { state.historyQuery = event.target.value; renderHistory(); });
  $("module").addEventListener("change", () => { selectModule($("module").value, false); saveDraft(); });
  document.querySelectorAll("[data-example-module]").forEach((control) => control.addEventListener("click", () => selectModule(control.dataset.exampleModule, false)));
  $("question").addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.shiftKey || event.altKey || composing || event.isComposing || event.keyCode === 229) return;
    if (matchMedia("(max-width:760px)").matches && !event.ctrlKey && !event.metaKey) return;
    event.preventDefault(); if (!$("send-button").disabled) $("chat-form").requestSubmit();
  });
  $("chat-form").addEventListener("submit", (event) => { event.preventDefault(); if (!composing) send($("question").value, state.previous, null, {consumeDraft: true}); });
  document.querySelectorAll(".suggestion").forEach((control) => control.addEventListener("click", () => draftQuestion(control)));
  $("connection").addEventListener("click", async () => { if (state.ready) openVersion(); else { await refreshModelReadiness(); if (state.ready && !state.pending) await resumePending(); } }); $("help-version").addEventListener("click", openVersion);
  $("context-meter").addEventListener("click", () => {
    const opening = $("context-bubble").hidden;
    $("context-bubble").hidden = !opening;
    $("context-meter").setAttribute("aria-expanded", String(opening));
    if (opening) { renderContextMeter(); syncContextUsage(true); }
  });
  document.addEventListener("pointerdown", event => {
    if (!$("context-bubble").hidden && !event.composedPath().includes($("context-meter-wrap"))) closeContextBubble();
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && !$("context-bubble").hidden) { closeContextBubble(); $("context-meter").focus({preventScroll: true}); }
  });
  document.addEventListener("focusin", event => {
    if (!$("context-bubble").hidden && !event.composedPath().includes($("context-meter-wrap"))) closeContextBubble();
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) syncContextUsage(true); });
  $("close-detail").addEventListener("click", () => $("detail-dialog").close());
  $("detail-dialog").addEventListener("close", () => {
    if ($("detail-dialog").open) return;
    state.drawerToken++;
    const context = sourceReturnContext; sourceReturnContext = null;
    if (context) restoreAnswerReading(context);
  });
  $("detail-dialog").addEventListener("click", (event) => { if (event.target === $("detail-dialog")) { const rect = $("detail-dialog").getBoundingClientRect(); if (event.clientX < rect.left) $("detail-dialog").close(); } });
  $("library-form").addEventListener("submit", (event) => { event.preventDefault(); searchLibrary(); });
  $("load-more").addEventListener("click", () => searchLibrary(true));
  $("saved-search").addEventListener("input", renderSavedLibrary);
  $("saved-module").addEventListener("change", renderSavedLibrary);
  globalThis.addEventListener?.("storage", event => {
    if (event.key === savedKey || event.key === null) { syncSavedButtons(); if (state.view === "saved") renderSavedLibrary(); }
  });
  let providerInfo = null, providerBusy = false, providerDraft = null, providerModels = [], providerSafetyAcknowledged = false;
  const providerNames = {deepseek:"DeepSeek", openai:"OpenAI", anthropic:"Anthropic", compatible:"通用兼容"};
  const providerFields = {context_tokens:"provider-context", max_output_tokens:"provider-output", input_cny_per_million:"provider-input-price", output_cny_per_million:"provider-output-price"};
  function modelSwitchLocked() {
    return modelSwitching || providerBusy || state.busy || !!state.pending || state.blocked || state.uncertain || state.modelChecking;
  }
  function renderComposerModel() {
    const trigger = $("composer-model"), current = composerModel;
    $("composer-model-name").textContent = current?.model || "选择模型";
    trigger.dataset.state = modelSwitching ? "busy" : $("backend-status").dataset.state;
    const status = modelSwitching ? "正在切换" : $("backend-status").textContent;
    trigger.title = `${current ? `${providerNames[current.protocol] || current.protocol || "模型"} · ${current.model} · ` : ""}${status}`;
    trigger.setAttribute("aria-label", `${current ? `当前模型 ${current.model}` : "选择模型"}，${status}，点击切换模型`);
    trigger.disabled = modelSwitching || providerBusy;
    if (!$("model-menu").hidden) {
      for (const item of $("model-menu-options").children) item.disabled = modelMenuLoading || modelSwitchLocked() || !providerInfo?.can_switch;
      $("model-menu-manage").disabled = modelSwitchLocked();
      $("model-menu").setAttribute("aria-busy", String(modelSwitching || modelMenuLoading));
    }
  }
  function closeModelMenu(restoreFocus = false) {
    $("model-menu").hidden = true;
    $("composer-model").setAttribute("aria-expanded", "false");
    modelMenuSerial++;
    if (restoreFocus) $("composer-model").focus({preventScroll:true});
  }
  function renderModelMenu() {
    const options = $("model-menu-options"); options.replaceChildren();
    const entries = Object.entries(providerInfo?.connections || {}).filter(([protocol, c]) => providerNames[protocol] && typeof c?.model === "string");
    for (const [protocol, connection] of entries) {
      const active = providerInfo.active === protocol;
      const item = button("", () => switchComposerModel(protocol), "model-option");
      item.dataset.protocol = protocol; item.setAttribute("aria-pressed", String(active));
      item.setAttribute("aria-label", `${connection.model} · ${providerNames[protocol]}${active ? "，当前模型" : ""}`);
      const text = node("span", "model-option-text");
      text.append(node("strong", "", connection.model), node("small", "", providerNames[protocol]));
      const check = node("span", "model-option-check", active ? "✓" : ""); check.setAttribute("aria-hidden", "true");
      item.append(text, check); options.append(item);
    }
    const message = modelMenuError || (modelMenuLoading ? "正在读取…" : modelSwitching ? "正在切换…" : modelSwitchLocked() ? "本轮结束后可切换" : providerInfo && !providerInfo.can_switch ? "当前连接无法切换模型" : !entries.length ? "还没有配置模型" : "");
    $("model-menu-feedback").textContent = message; $("model-menu-feedback").hidden = !message;
    renderComposerModel();
  }
  async function openModelMenu() {
    if (modelSwitching || providerBusy) return;
    if (!$("model-menu").hidden) { closeModelMenu(); return; }
    closeContextBubble();
    $("model-menu").hidden = false; $("composer-model").setAttribute("aria-expanded", "true");
    modelMenuLoading = true; modelMenuError = ""; renderModelMenu();
    $("close-model-menu").focus({preventScroll:true});
    const serial = ++modelMenuSerial;
    try {
      const info = await request("/v1/account/providers");
      if (serial !== modelMenuSerial) return;
      if (info.error) throw new Error("unreadable configuration");
      providerInfo = info;
      const active = info.connections?.[info.active];
      composerModel = active ? {protocol:info.active, model:active.model} : null;
    } catch {
      if (serial !== modelMenuSerial) return;
      providerInfo = null; modelMenuError = "暂时无法读取，关闭后重试";
    } finally {
      if (serial === modelMenuSerial) { modelMenuLoading = false; renderModelMenu(); }
    }
  }
  async function switchComposerModel(protocol) {
    if (modelSwitchLocked() || modelMenuLoading || !providerInfo?.can_switch || !providerInfo?.switch_nonce || !providerInfo.connections?.[protocol]) return;
    if (providerInfo.active === protocol) { closeModelMenu(true); return; }
    // Activate only an existing encrypted profile; no key or chat request is sent.
    modelSwitching = true; modelMenuError = ""; updateComposer(); renderModelMenu();
    try {
      const selectedProfile = JSON.stringify(providerInfo.connections[protocol]);
      // With mode=same-origin, inherited no-referrer makes WebKit send Origin:null.
      // Keep redirects/cross-origin requests forbidden and send provenance only to this server.
      const activate = () => request("/v1/account/providers/activate", {method:"POST", redirect:"error", credentials:"same-origin", mode:"same-origin", referrerPolicy:"same-origin",
        headers:{"Content-Type":"application/json", "X-Model-Switch-Nonce":providerInfo.switch_nonce},
        body:JSON.stringify({protocol})});
      let result;
      try { result = await activate(); }
      catch (error) {
        // Only an explicit pre-mutation nonce rejection is safe to recover once.
        // Lost responses, timeouts, denied origins and changed profiles never retry.
        if (error.httpStatus !== 403 || error.code !== "provider_switch_session_expired") throw error;
        const fresh = await request("/v1/account/providers"); providerInfo = fresh;
        if (fresh.error || !fresh.can_switch || !fresh.switch_nonce) throw error;
        if (!fresh.connections?.[protocol]) { error.code = "provider_not_configured"; throw error; }
        if (JSON.stringify(fresh.connections[protocol]) !== selectedProfile) { error.code = "provider_selection_changed"; throw error; }
        result = fresh.active === protocol ? fresh : await activate();
      }
      if (result.active !== protocol || !result.connections?.[protocol]?.model) throw new Error("switch not confirmed");
      providerInfo = result;
      composerModel = {protocol, model:result.connections[protocol].model};
      accountRevision++; accountOverview = null; accountRefreshQueued = accountLoading;
      await refreshModelReadiness(); refreshAccount(true);
      closeModelMenu();
      toast(`已切换至 ${composerModel?.model || result.connections[protocol].model}`);
    } catch (error) {
      // Read back after a lost response. Never automatically repeat activation.
      try {
        const result = await request("/v1/account/providers"); providerInfo = result;
        const active = result.connections?.[result.active];
        composerModel = active ? {protocol:result.active, model:active.model} : null;
        await refreshModelReadiness();
        accountRevision++; accountOverview = null; accountRefreshQueued = accountLoading; refreshAccount(true);
        modelMenuError = result.active === protocol ? "已确认切换完成" : "未切换，当前模型保持不变";
        if (result.active !== protocol) {
          const messages = {
            provider_switch_session_expired:"连接状态仍在更新，请再试一次",
            provider_switch_origin_rejected:"浏览器未通过来源校验，请刷新页面后重试",
            provider_switch_client_forbidden:"请通过电脑本机或同一局域网地址切换",
            provider_switch_forbidden:"本次切换被拒绝，请刷新页面后重试",
            provider_not_configured:"此配置已移除，请选择其他模型",
            provider_selection_changed:"模型配置已更新，请确认后再选择",
          };
          modelMenuError = messages[error.code] || modelMenuError;
        }
      } catch { state.modelReady = false; modelMenuError = "切换结果待确认，请刷新状态后再发送"; }
    } finally {
      modelSwitching = false; updateComposer();
      if (!$("model-menu").hidden) renderModelMenu();
      else $("composer-model").focus({preventScroll:true});
    }
  }
  $("composer-model").addEventListener("click", openModelMenu);
  $("close-model-menu").addEventListener("click", () => closeModelMenu(true));
  $("model-menu-manage").addEventListener("click", () => {
    if (modelSwitchLocked()) return;
    closeModelMenu(); providerReturnToComposer = true; openProvider();
  });
  document.addEventListener("pointerdown", event => {
    if (!$("model-menu").hidden && !event.composedPath().includes($("model-switcher"))) closeModelMenu();
  });
  document.addEventListener("keydown", event => {
    if ($("model-menu").hidden) return;
    if (event.key === "Escape") { event.preventDefault(); closeModelMenu(true); }
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      const items = [...$("model-menu").querySelectorAll(".model-option,.model-menu-manage")].filter(item => !item.disabled);
      if (!items.length) return;
      event.preventDefault(); const index = items.indexOf(document.activeElement);
      const next = event.key === "Home" ? 0 : event.key === "End" ? items.length-1 : (index + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      items[next].focus({preventScroll:true});
    }
  });
  document.addEventListener("focusin", event => {
    if (!$("model-menu").hidden && !event.composedPath().includes($("model-switcher"))) closeModelMenu();
  });
  function providerControls() {
    const denied = !providerInfo?.can_manage;
    $("provider-fields").disabled = providerBusy || denied;
    for (const id of ["provider-save","provider-check","provider-remove","provider-protocol"]) $(id).disabled = providerBusy || denied;
    $("close-provider").disabled = providerBusy;
    for (const item of $("provider-list").children) item.disabled = providerBusy;
    $("provider-check").textContent = providerBusy ? "正在连接…" : providerDraft || providerInfo?.connections?.[$("provider-protocol").value] ? "刷新模型" : "连接并读取模型";
  }
  function renderProviderModels(selected = "") {
    const choice = $("provider-model-choice"); choice.replaceChildren();
    const available = providerModels.filter(m => m.supported !== false);
    if (!available.length) { const option = node("option", "", "连接服务后选择"); option.value = ""; choice.append(option); }
    for (const model of available) { const option = node("option", "", model.name && model.name !== model.id ? `${model.name} · ${model.id}` : model.id); option.value = model.id; choice.append(option); }
    const custom = node("option", "", "手动填写模型 ID"); custom.value = "__custom__"; choice.append(custom);
    choice.value = available.some(m => m.id === selected) ? selected : available[0]?.id || "";
    selectProviderModel();
  }
  function selectProviderModel() {
    const selected = $("provider-model-choice").value, custom = selected === "__custom__";
    $("provider-model-label").hidden = !custom;
    if (!custom) $("provider-model").value = selected;
    const model = providerModels.find(m => m.id === selected), c = providerInfo?.connections?.[$("provider-protocol").value];
    const defaults = model?.defaults || (c?.model === selected ? c : null);
    for (const [field, id] of Object.entries(providerFields)) $(id).value = defaults?.[field] ?? "";
    $("provider-model-note").textContent = !selected ? "" : defaults ? "参数已填入，可在高级设置调整。" : "此模型参数未收录，请在高级设置补充。";
    $("provider-advanced").open = !!selected && !defaults;
  }
  function renderProviderFields() {
    const protocol = $("provider-protocol").value, c = providerInfo?.connections?.[protocol];
    providerDraft = null; providerModels = c ? [{id:c.model, name:c.model, defaults:c}] : [];
    $("provider-list").replaceChildren();
    for (const [id, name] of Object.entries(providerNames)) {
      const connected = providerInfo?.connections?.[id], active = connected && providerInfo.active === id;
      const item = button("", () => { $("provider-protocol").value = id; renderProviderFields(); $("provider-feedback").textContent = ""; }, "provider-card");
      item.setAttribute("aria-pressed", String(protocol === id)); item.dataset.provider = id;
      item.append(node("strong", "", name), node("span", "", active ? "使用中" : connected ? "已连接" : "未连接"));
      $("provider-list").append(item);
    }
    const official = providerInfo?.providers?.[protocol]?.base_url || {deepseek:"https://api.deepseek.com", openai:"https://api.openai.com/v1", anthropic:"https://api.anthropic.com/v1"}[protocol];
    $("provider-url").value = c?.base_url || official || "";
    $("provider-url").readOnly = !!official; $("provider-url-label").hidden = !!official;
    $("provider-token").value = ""; $("provider-token").placeholder = c ? "已加密保存 · 留空保留，输入可替换" : "输入密钥以连接";
    $("provider-key-state").textContent = c ? "密钥已加密保存" : "密钥只保存在部署电脑";
    $("provider-remove").hidden = !c; $("provider-deepseek-key").hidden = true;
    $("provider-remove").textContent = "移除此服务配置"; $("provider-remove").dataset.confirm = "false";
    $("provider-active").textContent = providerInfo?.connections?.[providerInfo.active] ? `正在使用 ${providerNames[providerInfo.active]} · ${providerInfo.connections[providerInfo.active].model}` : "连接一个模型服务";
    $("provider-owner-note").textContent = providerInfo?.can_manage ? "" : "手机可在输入栏切换已配置模型；密钥请在部署电脑管理。";
    $("provider-local-link").hidden = !!providerInfo?.can_manage;
    $("provider-local-link").href = `http://127.0.0.1:${globalThis.location?.port || "8000"}/`;
    renderProviderModels(c?.model || ""); providerControls();
  }
  async function openProvider() {
    if (providerBusy || modelSwitching) return;
    if (!providerSafetyAcknowledged && credentialInfo?.can_manage) { openCredential(); return; }
    if ($("account-dialog").open) $("account-dialog").close();
    $("provider-feedback").textContent = "正在读取配置…";
    if (!$("provider-dialog").open) $("provider-dialog").showModal();
    $("close-provider").focus();
    try {
      const [info] = await Promise.all([request("/v1/account/providers"), refreshCredentials()]);
      providerInfo = info; $("provider-protocol").value = info.active || "deepseek";
      $("provider-feedback").textContent = info.error ? "配置暂不可读，请检查加密存储。" : "";
    } catch { providerInfo = null; $("provider-feedback").textContent = "配置读取失败，请关闭后重试。"; }
    renderProviderFields();
  }
  async function changeProvider(action) {
    if (providerBusy || !providerInfo?.can_manage || !credentialInfo?.nonce) return;
    const protocol = $("provider-protocol").value;
    let connection = null;
    if (["save","discover"].includes(action)) {
      const token = $("provider-token").value.trim(), base = $("provider-url").value.trim();
      if (!token && !providerDraft && !providerInfo.connections?.[protocol]) { $("provider-feedback").textContent = "请先输入 API Key。"; $("provider-token").focus(); return; }
      if (protocol === "compatible" && !base) { $("provider-feedback").textContent = "请填写兼容服务的 API 地址。"; $("provider-url").focus(); return; }
      connection = {protocol, base_url:base, token, ...(providerDraft ? {draft_id:providerDraft} : {})};
      if (action === "save") {
        const model = $("provider-model").value.trim();
        if (!model) { $("provider-feedback").textContent = "先连接服务并选择模型。"; return; }
        connection.model = model;
        for (const [field, id] of Object.entries(providerFields)) {
          const value = $(id).value;
          if (value !== "") connection[field] = field.endsWith("tokens") ? Number(value) : value;
        }
        const saved = providerInfo.connections?.[protocol];
        if (saved && !token && !providerDraft && saved.model === model && saved.base_url === base
            && Object.keys(providerFields).every(field => Number(saved[field]) === Number(connection[field]))) {
          action = "activate"; connection = null;
        }
      }
    }
    providerBusy = true; providerControls();
    $("provider-feedback").textContent = action === "discover" ? "正在读取可用模型…" : "正在保存…";
    try {
      const pending = request("/v1/account/providers", {method:"POST", redirect:"error", credentials:"same-origin", mode:"same-origin", referrerPolicy:"same-origin",
        headers:{"Content-Type":"application/json","X-Credential-Nonce":credentialInfo.nonce},
        body:JSON.stringify({action,protocol,connection,acknowledged:true})});
      connection = null;
      const result = await pending;
      if (action === "discover") {
        if (!result.ok) {
          const reasons = {authentication_failed:"密钥无效或没有访问权限，请核对后重试。", models_endpoint_unavailable:"此地址未提供模型列表，可手动填写模型 ID。", connection_failed:"连接失败，请检查网络与 API 地址。"};
          $("provider-feedback").textContent = reasons[result.reason] || "连接未通过。";
        } else {
          $("provider-token").value = ""; providerDraft = result.draft_id;
          providerModels = result.models || []; renderProviderModels(providerInfo.connections?.[protocol]?.model);
          $("provider-key-state").textContent = "密钥已暂存，保存后加密落盘";
          $("provider-feedback").textContent = providerModels.length ? `已读取 ${providerModels.length} 个模型${result.complete === false ? "（部分列表）" : ""}，选择后保存。连接检查不生成回答。` : "连接成功，列表为空；可手动填写模型 ID。";
        }
      } else {
        $("provider-token").value = ""; providerInfo = result; renderProviderFields();
        $("provider-feedback").textContent = action === "remove" ? "配置已移除，历史记录保留。" : "已保存，新提问使用当前模型。";
        accountRevision++; accountOverview = null; accountRefreshQueued = accountLoading;
        refreshCredentials(); refreshModelReadiness(); refreshAccount(true);
      }
    } catch (error) {
      const tips = {credential_owner_required:"请在部署电脑打开本机管理页面，刷新后重试。", provider_new_host_requires_key:"地址已更换，请重新填入该服务的密钥。", provider_endpoint_mismatch:"官方服务请使用官方地址；中转服务请选择通用兼容。", provider_context_invalid:"输出上限必须小于上下文上限。", provider_draft_expired:"临时密钥已过期，请重新连接。", provider_metadata_required:"未收录此模型的参数，请在高级设置补充上下文和单价。", provider_model_unsupported:"此模型不支持当前文本工具链，请选择支持工具调用的文本模型。"};
      $("provider-feedback").textContent = error.uncertain ? "保存结果待确认，请关闭后重新查看配置，勿重复提交。" : tips[error.code] || "操作未完成，请核对配置并重试。";
      if (error.code === "provider_metadata_required") $("provider-advanced").open = true;
    } finally { connection = null; providerBusy = false; providerControls(); }
  }
  $("credential-other-provider").addEventListener("click", () => { $("credential-dialog").close(); openProvider(); });
  $("manage-provider").addEventListener("click", () => { providerReturnToComposer = false; openProvider(); });
  $("close-provider").addEventListener("click", () => { if (!providerBusy) $("provider-dialog").close(); });
  $("provider-dialog").addEventListener("cancel", event => { if (providerBusy) event.preventDefault(); });
  $("provider-dialog").addEventListener("close", () => { $("provider-token").value = ""; providerDraft = null; $(providerReturnToComposer ? "composer-model" : "open-account").focus({preventScroll:true}); providerReturnToComposer = false; });
  $("provider-protocol").addEventListener("change", () => { renderProviderFields(); $("provider-feedback").textContent = ""; });
  $("provider-model-choice").addEventListener("change", selectProviderModel);
  for (const id of ["provider-url","provider-token"]) $(id).addEventListener("input", () => { providerDraft = null; });
  $("provider-form").addEventListener("submit", event => { event.preventDefault(); changeProvider("save"); });
  $("provider-check").addEventListener("click", () => changeProvider("discover"));
  $("provider-remove").addEventListener("click", () => {
    if ($("provider-remove").dataset.confirm !== "true") { $("provider-remove").dataset.confirm = "true"; $("provider-remove").textContent = "确认移除此服务"; return; }
    changeProvider("remove");
  });
  $("provider-deepseek-key").addEventListener("click", openProvider);
  function openAccount() { renderAccount(); $("account-dialog").showModal(); $("close-account").focus(); refreshAccount(); refreshCredentials(); }
  $("open-account").addEventListener("click", openAccount);
  $("balance-alert-open").addEventListener("click", openAccount);
  $("balance-alert-snooze").addEventListener("click", () => {
    balanceAlert.snoozedUntil = Date.now() + 3600000; saveBalanceAlert(); renderAccount(); $("open-account").focus({preventScroll: true});
  });
  $("balance-alert-resume").addEventListener("click", () => {
    balanceAlert.snoozedUntil = 0; saveBalanceAlert(); renderAccount(); $("balance-alert-enabled").focus({preventScroll: true});
  });
  $("balance-alert-enabled").addEventListener("change", () => {
    balanceAlert.enabled = $("balance-alert-enabled").checked; balanceAlert.snoozedUntil = 0; saveBalanceAlert(); renderAccount();
  });
  $("balance-alert-form").addEventListener("submit", event => {
    event.preventDefault();
    if (!balanceAlert.enabled) return;
    const raw = $("balance-alert-threshold").value.trim(), amount = Number(raw);
    if (!/^\d+(\.\d{1,2})?$/.test(raw) || !Number.isFinite(amount) || amount < .01 || amount > 10000) {
      $("balance-alert-error").textContent = "请输入 0.01–10000 元，最多两位小数。";
      $("balance-alert-threshold").setAttribute("aria-invalid", "true"); $("balance-alert-threshold").focus(); return;
    }
    balanceAlert.thresholdCents = Math.round(amount * 100); balanceAlert.snoozedUntil = 0;
    $("balance-alert-threshold").value = (balanceAlert.thresholdCents / 100).toFixed(2);
    $("balance-alert-threshold").removeAttribute("aria-invalid"); $("balance-alert-error").textContent = "";
    saveBalanceAlert(); renderAccount(); toast(balanceAlertVolatile ? "提醒设置仅在当前页面生效" : "提醒设置已保存");
  });
  $("balance-alert-threshold").value = (balanceAlert.thresholdCents / 100).toFixed(2);
  $("close-account").addEventListener("click", () => $("account-dialog").close());
  $("account-dialog").addEventListener("close", () => $("open-account").focus({preventScroll: true}));
  $("account-refresh").addEventListener("click", () => refreshAccount(true));
  $("account-privacy").addEventListener("click", () => { hideAmounts = !hideAmounts; try { localStorage.setItem("ark-archive.amounts-hidden.v1", String(hideAmounts)); } catch { /* Keep preference in this page. */ } renderAccount(); });
  $("manage-credential").addEventListener("click", async () => { await refreshCredentials(); openCredential(); });
  $("close-credential").addEventListener("click", () => $("credential-dialog").close());
  $("credential-later").addEventListener("click", () => $("credential-dialog").close());
  $("credential-dialog").addEventListener("close", () => {
    $("credential-input").value = ""; credentialConsent = false; $("credential-remove-confirm").checked = false;
    $(providerReturnToComposer ? "composer-model" : "open-account").focus({preventScroll: true});
  });
  $("credential-understand").addEventListener("click", () => {
    if (!credentialInfo?.can_manage || credentialBusy) return;
    providerSafetyAcknowledged = true; $("credential-dialog").close(); openProvider();
  });
  $("credential-form").addEventListener("submit", event => { event.preventDefault(); return changeCredential("save"); });
  $("credential-remove-confirm").addEventListener("change", renderCredential);
  $("credential-remove").addEventListener("click", () => changeCredential("remove"));
  $("credential-recheck").addEventListener("click", async () => {
    const ok = await refreshCredentials(); credentialFeedback(ok ? "已读取当前配置状态。" : "暂时无法读取配置，请稍后重试。", !ok);
  });
  document.addEventListener("visibilitychange", () => { clearTimeout(accountTimer); if (!document.hidden) refreshAccount(); });
  restoreDraft("new"); renderHistory(); syncSavedButtons(); updateComposer(); refreshAccount(); refreshCredentials(true);
  (async () => {
    await Promise.all([refreshModelReadiness(), resumePending()]);
    updateComposer();
  })();
})();
