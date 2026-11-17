/* Explicit planning tools. Never submits chat, changes its draft, or calls a model. */
(() => {
  "use strict";
  const uniquePicks = (catalog, picks) => {
    const seen = new Set(), units = new Map(catalog.units.map(u => [u.id, u]));
    return (Array.isArray(picks) ? picks : []).slice(0, 40).filter(p => {
      const u = p && units.get(p.id);
      if (!u || seen.has(u.operator_id)) return false;
      seen.add(u.operator_id); return true;
    }).map(p => ({id: p.id}));
  };
  function analyze(catalog, picks) {
    const units = new Map(catalog.units.map(u => [u.id, u]));
    const safe = uniquePicks(catalog, picks);
    return catalog.bonds.map(b => {
      const ordinary = ["count_threshold_upward", "count_threshold_downward"].includes(b.template) && ["BOARD", "BOARD_AND_DECK"].includes(b.scope);
      const members = safe.filter(p => units.get(p.id).bonds.includes(b.id));
      const count = members.length, upper = Number(b.params?.[1]);
      const supported = Number.isInteger(b.threshold) && b.threshold > 0 && ordinary && (b.template !== "count_threshold_downward" || (Number.isInteger(upper) && upper > b.threshold));
      const blocked = supported && b.template === "count_threshold_downward" && count >= upper;
      const tiers = supported && !blocked ? (Array.isArray(b.tiers) && b.tiers.length ? b.tiers : [{count:b.threshold}]).filter(t=>Number.isInteger(t.count)&&t.count>=b.threshold).sort((a,b)=>a.count-b.count) : [];
      const achieved = tiers.filter(t=>count>=t.count), nextTier = tiers.find(t=>count<t.count);
      return {...b, count, members, supported, blocked, tiers, achieved, nextTier,
        tier: achieved.at(-1)?.count || null, met: supported && !blocked && count >= b.threshold,
        gap: supported ? Math.max(0, (nextTier?.count || b.threshold) - count) : null};
    });
  }
  function suggest(catalog, picks, targetId, replaceId = "") {
    const safe = uniquePicks(catalog, picks), before = analyze(catalog, safe), target = before.find(b => b.id === targetId);
    if (!target?.supported || target.special_effect || target.template === "count_threshold_upward_golden") return [];
    const replace = safe.find(p => p.id === replaceId), remaining = safe.filter(p => p.id !== replace?.id);
    const occupied = new Set(remaining.map(p => catalog.units.find(u => u.id === p.id).operator_id));
    return catalog.units.filter(u => u.id !== replace?.id && !occupied.has(u.operator_id) && u.bonds.includes(targetId)).map(u => {
      const after = analyze(catalog, [...remaining, {id:u.id}]);
      const current = after.find(b => b.id === targetId);
      const lost = before.filter(b => b.met && (b.tier || b.threshold) > (after.find(a => a.id === b.id).tier || 0))
        .map(b => after.find(a => a.id === b.id).met ? `${b.name}${b.tier}人档` : b.name);
      const gained = after.filter(b => b.met && (b.tier || b.threshold) > (before.find(a => a.id === b.id).tier || 0)).map(b => b.name);
      return {unit:u, lost, gained, count:current.count, gap:current.gap, blocked:current.blocked, met:current.met};
    }).filter(r => !r.blocked && (r.met || r.count > target.count)).sort((a,b) => Number(b.met)-Number(a.met) || a.lost.length-b.lost.length || b.gained.length-a.gained.length || a.unit.level-b.unit.level || a.unit.name.localeCompare(b.unit.name, "zh-CN"));
  }
  function groupCombinations(combinations) {
    const groups = new Map();
    for (const combo of combinations) {
      // Keep all conditions and tag variants; grouping never claims equal probabilities.
      const key = JSON.stringify([combo.candidates.map(c=>[c.native_id || c.name,c.rarity_stars]).sort(), combo.minimum_rarity_if_tags_retained,
        combo.status, combo.ordinary_tags_may_drop, combo.special_tags_protected_at_this_duration, combo.condition]);
      if (!groups.has(key)) groups.set(key,{...combo, variants:[]});
      groups.get(key).variants.push(combo);
    }
    return [...groups.values()];
  }
  const logic = {uniquePicks, analyze, suggest, groupCombinations};
  if (typeof module !== "undefined" && module.exports) module.exports = logic;
  if (typeof document === "undefined") return;
  const $ = id => document.getElementById(id);
  const el = (tag, cls, text) => { const n = document.createElement(tag); if(cls) n.className = cls; if(text !== undefined) n.textContent = text; return n; };
  const btn = (text, action, cls = "tool-button") => { const b = el("button", cls, text); b.type = "button"; b.addEventListener("click", action); return b; };
  const option = (value, text) => { const n = el("option", "", text); n.value = value; return n; };
  const labelled = (text, input) => { const l = el("label", "tool-label", text); l.append(input); return l; };
  const read = key => { try { return JSON.parse(localStorage.getItem(key)); } catch { return null; } };
  const save = (key, value, status) => { try { localStorage.setItem(key, JSON.stringify(value)); } catch { status.textContent = "浏览器无法保存，本页仍可使用。"; } };
  async function api(path, options = {}, timeout = 25000) {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(path, {...options, signal:controller.signal, cache:"no-store"});
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "这次操作未完成，请重试。");
      return data;
    } catch (e) { throw new Error(e.name === "AbortError" ? "等待超时。内容已保留，可稍后重试。" : e.message === "Failed to fetch" ? "连接中断，请检查电脑服务和 Wi-Fi 后重试。" : e.message); }
    finally { clearTimeout(timer); }
  }
  function sourceDialog(title, description, source) {
    const dialog = el("dialog", "tool-source-dialog");
    dialog.setAttribute("aria-label", title);
    const close = btn("返回工具", () => dialog.close());
    dialog.append(close, el("h2", "", title), el("p", "tool-source-text", description));
    for (const item of (Array.isArray(source) ? source : [source])) {
      const url=item?.page_url || item?.url;
      if (!url || !/^https:\/\//.test(url)) continue;
      const link = el("a", "tool-source-link", item.page_url ? "查看规则参考" : "打开原始来源"); link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer"; dialog.append(link);
    }
    dialog.append(el("p", "tool-muted", "规则来自当前工具绑定的资料版本。"));
    document.body.append(dialog); dialog.addEventListener("close", () => dialog.remove(), {once:true}); dialog.showModal();
  }
  let current = "squad", squadLoaded = false, recruitLoaded = false, squadLoading = false, recruitLoading = false;
  function tab(which) {
    current = which;
    for(const type of ["squad", "recruit"]) { $(`${type}-tab`).setAttribute("aria-pressed", String(type === which)); $(`${type}-tool`).hidden = type !== which; }
    load(which);
  }
  $("squad-tab").addEventListener("click", () => tab("squad"));
  $("recruit-tab").addEventListener("click", () => tab("recruit"));
  async function load(which) {
    if (which === "squad" ? squadLoaded || squadLoading : recruitLoaded || recruitLoading) return;
    if (which === "squad") squadLoading = true; else recruitLoading = true;
    const root = $(`${which}-tool`); root.replaceChildren(el("p", "tool-loading", "正在读取已校验的资料…")); root.setAttribute("aria-busy", "true");
    try {
      const data = await api(`/v1/game-tools/${which === "squad" ? "garrison" : "recruitment"}`);
      if (which === "squad") { buildSquad(root, data); squadLoaded = true; } else { buildRecruit(root, data); recruitLoaded = true; }
    } catch(e) { root.replaceChildren(el("p", "tool-error", e.message), btn("重新读取", () => load(which))); }
    finally { root.setAttribute("aria-busy", "false"); if (which === "squad") squadLoading = false; else recruitLoading = false; }
  }
  window.ArkGameTools = {open: () => load(current)};
  function buildSquad(root, data) {
    const key = `ark-tools.squad.v2.${data.release_id}.${data.phase_id}`;
    const saved = read(key), legacy = read(`ark-tools.squad.v1.${data.release_id}.${data.phase_id}`);
    // Migrate both old zones without losing members; retain the old record for rollback.
    let picks = uniquePicks(data, saved ?? legacy), pointerDrag = null, suppressClick = false, dragGhost = null;
    const names = new Map(data.bonds.map(b => [b.id,b.name])), unitMap = new Map(data.units.map(u => [u.id,u]));
    root.replaceChildren();
    const header = el("div", "tool-section-heading"), status = el("p", "tool-status squad-status"); status.setAttribute("role", "status");
    const rules = btn("规则", () => sourceDialog(data.season,
      `按已选干员计算人数档位，不区分位置，不代表实战激活。精锐、层数与调和加成不在此模拟。\n\n${data.omitted_unit_count || 0} 个自选或资料不完整条目未纳入；特殊获取不代表商店可购买。`, data.bonds[0]?.source), "tool-text-button");
    header.append(el("h2", "", data.season), rules); root.append(header,status);
    if (saved === null && Array.isArray(legacy)) save(key,picks,status);
    const layout = el("div", "squad-layout"), workspace = el("div", "squad-workspace"), catalog = el("section", "tool-card squad-catalog");
    const selected = el("section", "tool-card squad-zone"), heading = el("h3"), slots = el("div", "squad-slots");
    selected.append(heading,slots);
    const search = el("input"); search.type="search"; search.placeholder="搜索干员"; search.setAttribute("aria-label","搜索沙盘干员");
    const filter = el("select"); filter.setAttribute("aria-label","筛选干员盟约"); filter.append(option("","全部盟约"));
    data.bonds.filter(b=>b.scope!=="BOARD_ALL_CHESS").forEach(b=>filter.append(option(b.id,b.name)));
    const filters=el("div","squad-filters"),roster=el("div","squad-roster"); filters.append(search,filter); catalog.append(filters,roster);
    const bondsRoot=el("div","bond-chips"),otherBonds=el("details","tool-disclosure"),otherSummary=el("summary"),otherList=el("div","bond-chips");
    otherBonds.append(otherSummary,otherList);
    const bondSection=el("section","squad-bonds");bondSection.append(bondsRoot,otherBonds);
    const recommendation=el("details","tool-disclosure squad-recommendation"),suggestions=el("div","squad-suggestions");
    recommendation.append(el("summary","","补位与替换"));
    const target=el("select");target.setAttribute("aria-label","希望补齐的盟约");target.append(option("","选择盟约"));
    data.bonds.filter(b=>!b.special_effect&&b.template!=="count_threshold_upward_golden").forEach(b=>target.append(option(b.id,b.name)));
    const replace=el("select");replace.setAttribute("aria-label","替换干员");
    const suggestionFilters=el("div","squad-filters");suggestionFilters.append(target,replace);recommendation.append(suggestionFilters,suggestions);
    workspace.append(selected,bondSection,recommendation);layout.append(catalog,workspace);root.append(layout);
    function commit(){status.textContent="";save(key,picks,status);render();}
    function add(id){
      const unit=unitMap.get(id);if(!unit)return;
      if(picks.some(p=>unitMap.get(p.id)?.operator_id===unit.operator_id))return;
      if(picks.length>=40){status.textContent="最多保存40名干员";return;}
      picks.push({id});commit();
    }
    function renderRoster(){
      const scrollTop=roster.scrollTop;roster.replaceChildren();const query=search.value.trim();
      const rows=data.units.filter(u=>(!query||u.name.includes(query))&&(!filter.value||u.bonds.includes(filter.value)));
      if(!rows.length)roster.append(el("p","tool-muted","没有匹配的干员"));
      rows.forEach(u=>{
        const occupied=picks.some(p=>unitMap.get(p.id)?.operator_id===u.operator_id);
        const button=btn("",()=>{if(suppressClick){suppressClick=false;return;}add(u.id);},"roster-unit");button.draggable=false;
        button.setAttribute("aria-label",`${u.name}，${occupied?"已加入":"加入阵容"}`);
        button.append(el("strong","",u.name),el("span","",`${u.level}阶 · ${u.bonds.map(id=>names.get(id)).join(" / ")}`));
        if(u.special_obtain)button.append(el("small","","特殊获取"));
        if(occupied)button.classList.add("selected");
        button.addEventListener("pointerdown",e=>{if(e.pointerType==="mouse"&&e.button===0){pointerDrag={id:u.id,x:e.clientX,y:e.clientY,moved:false};suppressClick=false;button.setPointerCapture(e.pointerId);}});
        roster.append(button);
      });
      roster.scrollTop=scrollTop;
    }
    function bondButton(b){
      const label=!b.supported?"条件":b.blocked?"超限":`${b.count}/${b.nextTier?.count || b.tier || b.threshold}`;
      const button=btn("",()=>sourceDialog(b.name,`${b.description}${b.special_effect?"\n\n调和加成未计入。":""}`,b.source),`bond-chip ${b.met?"met":""} ${b.blocked?"blocked":""}`);
      button.append(el("span","",b.name),el("strong","",label));
      if (b.tier) button.append(el("small","bond-tier",`${b.tier}人档${b.nextTier?` · 差${b.gap}人进阶`:""}`));
      button.setAttribute("aria-label",`${b.name}，${!b.supported?"特殊条件":b.blocked?"人数超限":b.nextTier?`还缺${b.gap}名达到${b.nextTier.count}人档`:b.met?`${b.tier || b.threshold}人档已满足`:`还缺${b.gap}名`}，查看规则`);return button;
    }
    function render(){
      heading.textContent=`已选 · ${picks.length}`;slots.replaceChildren();
      if(!picks.length)slots.append(el("p","squad-empty","尚未选择"));
      picks.forEach(p=>{const u=unitMap.get(p.id),chip=el("div","squad-pick"),remove=btn("×",()=>{picks=picks.filter(x=>x.id!==p.id);commit();},"pick-remove");remove.setAttribute("aria-label",`移除${u.name}`);chip.append(el("span","",u.name),remove);slots.append(chip);});
      bondsRoot.replaceChildren();otherList.replaceChildren();let hidden=0;
      analyze(data,picks).sort((a,b)=>Number(b.met)-Number(a.met)||b.count-a.count).forEach(b=>{
        const visible=b.count>0||b.id===filter.value;
        (visible?bondsRoot:otherList).append(bondButton(b));if(!visible)hidden++;
      });otherSummary.textContent=`其他盟约 · ${hidden}`;otherBonds.hidden=!hidden;
      const old=replace.value;replace.replaceChildren(option("","直接加入"));picks.forEach(p=>replace.append(option(p.id,`替换 ${unitMap.get(p.id).name}`)));replace.value=picks.some(p=>p.id===old)?old:"";
      renderRoster();renderSuggestions();
    }
    function renderSuggestions(){
      suggestions.replaceChildren();if(!target.value)return;
      const rows=suggest(data,picks,target.value,replace.value).slice(0,3);
      if(!rows.length)suggestions.append(el("p","tool-muted","暂无合适补位"));
      rows.forEach(r=>{
        const card=el("article","suggestion-card");card.append(el("strong","",r.unit.name),el("span","",r.gap?`还缺 ${r.gap} 名` : "人数档位满足"));
        if(r.lost.length)card.append(el("small","tool-warning",`会失去：${r.lost.join("、")}`));
        if(r.unit.special_obtain)card.append(el("small","tool-muted","特殊获取"));
        card.append(btn(replace.value?"替换":"加入",()=>{if(replace.value){picks=picks.filter(p=>p.id!==replace.value);picks.push({id:r.unit.id});replace.value="";commit();}else add(r.unit.id);}));suggestions.append(card);
      });
    }
    function endDrag(){pointerDrag=null;dragGhost?.remove();dragGhost=null;selected.classList.remove("drag-over");}
    document.addEventListener("pointermove",e=>{
      if(!pointerDrag)return;if(Math.hypot(e.clientX-pointerDrag.x,e.clientY-pointerDrag.y)>8)pointerDrag.moved=true;if(!pointerDrag.moved)return;
      if(!dragGhost){dragGhost=el("div","squad-drag-ghost",unitMap.get(pointerDrag.id).name);dragGhost.setAttribute("aria-hidden","true");document.body.append(dragGhost);}
      dragGhost.style.left=`${e.clientX+12}px`;dragGhost.style.top=`${e.clientY+12}px`;
      selected.classList.toggle("drag-over",document.elementFromPoint(e.clientX,e.clientY)?.closest(".squad-zone")===selected);
    });
    document.addEventListener("pointerup",e=>{const pending=pointerDrag;endDrag();if(!pending?.moved)return;suppressClick=true;if(!root.hidden&&document.elementFromPoint(e.clientX,e.clientY)?.closest(".squad-zone")===selected)add(pending.id);setTimeout(()=>{suppressClick=false;},0);});
    document.addEventListener("pointercancel",endDrag);window.addEventListener("blur",endDrag);
    search.addEventListener("input",renderRoster);filter.addEventListener("change",render);target.addEventListener("change",renderSuggestions);replace.addEventListener("change",renderSuggestions);render();
  }
  function buildRecruit(root,data) {
    const key=`ark-tools.recruit.v1.${data.release_id}`, saved=read(key);
    let tags=Array.isArray(saved?.tags)?[...new Set(saved.tags)].filter(t=>data.tags.includes(t)).slice(0,5):[], confirmed=false, busy=false, previewURL="", generation=0;
    root.replaceChildren();
    const grid=el("div","recruit-layout"), input=el("section","tool-card recruit-input"), output=el("section","tool-card recruit-output");
    const photo=el("details","tool-disclosure recruit-photo");photo.append(el("summary","","截图识别"));
    const file=el("input");file.type="file";file.accept="image/png,image/jpeg,image/webp,image/heic,image/heif";file.setAttribute("aria-label","选择公招截图");file.disabled=!data.ocr_available;
    const preview=el("img","recruit-preview");preview.hidden=true;preview.alt="待识别的公招截图";
    const status=el("p","tool-status",data.ocr_available?"":"本机不支持识图，请手动选标签");status.setAttribute("role","status");
    const recognize=btn("识别截图",recognizePhoto,"tool-button tool-primary");recognize.disabled=true;
    const clearImage=btn("移除图片",()=>{generation++;if(previewURL)URL.revokeObjectURL(previewURL);previewURL="";preview.removeAttribute("src");preview.hidden=true;file.value="";recognize.disabled=true;clearImage.hidden=true;status.textContent="";});clearImage.hidden=true;
    photo.append(file,preview,recognize,clearImage);input.append(photo,status);
    const count=el("span","tag-count"), picker=el("div","tag-picker"),selectedTags=el("div","selected-tags");
    const tagEditor=el("details","tool-disclosure tag-editor"),tagSummary=el("summary");tagSummary.append(el("span","","选择标签"),count);tagEditor.append(tagSummary,picker);
    const duration=el("select");duration.setAttribute("aria-label","招募时长");
    for(let m=60;m<=540;m+=10)duration.append(option(String(m),`${Math.floor(m/60)}小时${m%60?m%60+"分":""}`));duration.value=String(Number.isInteger(saved?.duration)&&saved.duration>=60&&saved.duration<=540&&saved.duration%10===0?saved.duration:540);
    const check=el("input");check.type="checkbox"; const confirm=labelled("已核对标签与时长",check);confirm.classList.add("tag-confirm");
    const calculate=btn("计算组合",calculateTags,"tool-button tool-primary");calculate.disabled=true;
    const controls=el("div","recruit-controls");controls.append(duration,calculate);
    input.append(selectedTags,tagEditor,confirm,controls);output.hidden=true;
    grid.append(input,output);root.append(grid);
    function persist(){save(key,{tags,duration:Number(duration.value)},status);}
    function invalidate(){generation++;confirmed=false;check.checked=false;calculate.disabled=true;persist();output.replaceChildren();output.hidden=true;}
    function renderTags(){
      count.textContent=`${tags.length}/5`;picker.replaceChildren();selectedTags.replaceChildren();selectedTags.hidden=!tags.length;
      tags.forEach(tag=>{const remove=btn(`${tag} ×`,()=>{tags=tags.filter(t=>t!==tag);invalidate();renderTags();},"selected-tag");remove.setAttribute("aria-label",`移除标签${tag}`);remove.disabled=busy;selectedTags.append(remove);});
      data.tags.forEach(tag=>{const b=btn(tag,()=>{if(tags.includes(tag))tags=tags.filter(t=>t!==tag);else if(tags.length<5)tags.push(tag);else{status.textContent="最多选择屏幕上的5个标签，请先移除一个。";return;}invalidate();renderTags();},"tag-choice");b.setAttribute("aria-pressed",String(tags.includes(tag)));b.disabled=busy;picker.append(b);});
    }
    function lock(value){busy=value;file.disabled=value||!data.ocr_available;recognize.disabled=value||!file.files?.length||file.files[0].size>12*1024*1024;clearImage.disabled=value;duration.disabled=value;check.disabled=value;calculate.disabled=value||!confirmed;renderTags();}
    check.addEventListener("change",()=>{confirmed=check.checked;calculate.disabled=busy||!confirmed;});duration.addEventListener("change",invalidate);
    file.addEventListener("change",()=>{
      generation++;if(previewURL)URL.revokeObjectURL(previewURL);previewURL="";preview.removeAttribute("src");preview.hidden=true;
      // New screenshot must not inherit the previous screenshot's confirmed tags.
      tags=[];invalidate();renderTags();const f=file.files?.[0];clearImage.hidden=!f;recognize.disabled=!f;
      if(!f)return;if(f.size>12*1024*1024){status.textContent="图片超过12MB，请裁剪到标签区域。";recognize.disabled=true;return;}
      previewURL=URL.createObjectURL(f);preview.src=previewURL;preview.hidden=false;status.textContent="";
    });
    preview.addEventListener("error",()=>{preview.hidden=true;status.textContent="浏览器无法预览这种格式，仍可尝试识别。";});
    async function recognizePhoto(){
      const f=file.files?.[0];if(!f||busy||f.size>12*1024*1024)return;invalidate();const token=generation;lock(true);status.textContent="正在识别…";
      const extension=f.name.split(".").pop().toLowerCase(), mime=f.type||({jpg:"image/jpeg",jpeg:"image/jpeg",png:"image/png",webp:"image/webp",heic:"image/heic",heif:"image/heif"}[extension]||"application/octet-stream");
      try{const result=await api(`/v1/game-tools/recruitment/recognize?release_id=${encodeURIComponent(data.release_id)}`,{method:"POST",headers:{"Content-Type":mime},body:f},125000);
        if(token!==generation)return;
        const detected=result.tags.filter(t=>data.tags.includes(t.name));
        tags=detected.length<=5?detected.map(t=>t.name):[];persist();if(tags.length)photo.open=false;
        status.textContent=detected.length>5?"识别出超过5个标签，请裁剪到本次招募区域，或手动选择。":tags.length?`识别到 ${tags.length} 个标签，待核对`:"未识别到完整标签，可换清晰截图或手动选择。";
      }catch(e){if(token===generation)status.textContent=e.message;}finally{lock(false);}
    }
    async function calculateTags(){
      if(busy||!confirmed)return;const token=++generation;lock(true);status.textContent="正在计算组合…";
      try{const result=await api("/v1/game-tools/recruitment/calculate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({release_id:data.release_id,tags,duration_minutes:Number(duration.value),confirmed:true})});if(token!==generation)return;
        output.hidden=false;tagEditor.open=false;photo.open=false;
        const resultHeading=el("div","tool-section-heading");resultHeading.append(el("h2","",`组合 · ${result.combinations.length}`));
        output.replaceChildren(resultHeading,el("p","combo-caveat","星级以标签保留为前提"));
        if(result.sources?.length) resultHeading.append(btn("依据",()=>sourceDialog("公招计算依据",["最低星级以标签保留为前提，候选数量不是招募概率；资料池不实时跟随游戏更新。",...result.warnings].join("\n\n"),result.sources),"tool-text-button"));
        const grouped=groupCombinations(result.combinations);
        const primary=el("div","recruit-primary-results"),more=el("details","tool-disclosure recruit-more"),remaining=el("div","recruit-remaining");more.append(el("summary","",`其余 ${Math.max(0,grouped.length-3)} 组结果`),remaining);output.append(primary);if(grouped.length>3)output.append(more);
        result.warnings.filter(w=>w.includes("支援机械")||w.includes("未满9小时")).forEach(w=>output.append(el("p","tool-warning",w)));
        grouped.forEach((combo,index)=>{
          const card=el("article","recruit-combo");card.append(el("h3","",combo.selected_tags.join(" + ")||"不选标签"));
          card.append(el("strong","combo-floor",combo.candidate_count?`最低 ${combo.minimum_rarity_if_tags_retained} 星 · ${combo.candidate_count} 位候选`:"保留标签时无候选"));
          const detail=el("details"),summary=el("summary","",`候选 · ${combo.candidate_count}`);detail.append(summary);
          combo.candidates.forEach(c=>detail.append(el("span","candidate-name",`${c.name} · ${c.rarity_stars}星`)));if(combo.candidate_count)card.append(detail);
          if(combo.variants.length>1){const variants=el("details","combo-variants");variants.append(el("summary","",`其他标签组合 · ${combo.variants.length-1}`));combo.variants.slice(1).forEach(v=>variants.append(el("p","",v.selected_tags.join(" + ")||"不选标签")));card.append(variants);}
          (index<3?primary:remaining).append(card);
        });status.textContent="";
      }catch(e){if(token===generation)status.textContent=e.message;}finally{lock(false);}
    }
    renderTags();
  }
})();
