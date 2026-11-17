"""Public, bounded descriptions of actual LangGraph workbench activity."""

from urllib.parse import urlsplit

from arknights_agent.roguelike_sources import THEME_NAMES
from arknights_agent.runtime.security import contains_secret, redact

MODULES = {
    "operator": "干员资料",
    "recruitment": "公开招募",
    "garrison": "卫戍协议",
    "roguelike": "集成战略",
}

TOOL_LABELS = {
    "query_records": "查找资料条目",
    "analysis_search": "查找相关资料",
    "analysis_read": "读取资料原文",
    "read_evidence": "核对来源摘录",
    "rag_search": "检索已收录资料",
    "roguelike_query": "查询集成战略资料",
    "read_source_query": "读取固定资料",
    "source_query": "查询固定资料",
    "web_search": "搜索公开网页",
    "public_wiki_read": "读取公开网页",
}
QUERY_LABELS = {
    "catalog": "查找资料目录",
    "operator_overview": "查询干员档案",
    "operator_skill": "查询干员技能",
    "operator_talent": "查询干员天赋",
    "operator_module": "查询干员模组",
    "operator_attributes": "查询干员属性",
    "recruitment_calculation": "核对招募标签与候选",
    "recruitment_tag_overview": "查询标签保底星级",
    "garrison_metadata": "查询卫戍赛季资料",
    "garrison_regulation_record": "读取卫戍条目",
    "garrison_bond_catalog": "读取盟约目录",
}


def _short_text(value):
    if not isinstance(value, str) or contains_secret(value) or redact(value) != value:
        return ""
    text = " ".join(value.split())
    return text[:72] if not text.startswith(("http://", "https://")) else ""


def _items(value):
    return value if isinstance(value, list) else []


def _source_host(item):
    url = item.get("source_url") or item.get("page_url")
    if not isinstance(url, str):
        return ""
    try:
        parsed = urlsplit(url)
        return _short_text(parsed.hostname) if parsed.scheme in {"http", "https"} else ""
    except ValueError:
        return ""


def _source_name(item):
    value = item.get("field_value")
    if isinstance(value, dict):
        for key in ("name", "title", "effectName"):
            if name := _short_text(value.get(key)):
                return name
    for key in ("name", "title"):
        if name := _short_text(item.get(key)):
            return name
    return ""


def _found_sources(tool, data):
    # Only explicit, successful source lookups feed the live list. Prior model
    # drafts, review prose, arbitrary arguments and raw source values stay out.
    if tool in {"source_query", "read_source_query", "roguelike_query"}:
        if data.get("status") not in {"answered", "partial", "computed"}:
            return
        rows = _items(data.get("citations")) + _items(data.get("records"))
    elif tool in {"analysis_read", "read_evidence", "public_wiki_read"}:
        rows = [data]
    else:
        rows = _items(data.get("hits")) + _items(data.get("items"))
    for item in rows:
        if not isinstance(item, dict):
            continue
        citation = item.get("citation")
        evidence = item.get("evidence")
        host = _source_host(citation if isinstance(citation, dict) else
                            evidence if isinstance(evidence, dict) else item)
        name = _source_name(item)
        if name or host:
            yield {"title": name or host, "site": host if name else ""}


class TaskProgress:
    """One publisher per execution. Pollers receive only its latest snapshot."""

    def __init__(self, publish, question):
        self.publish = publish
        self.question = question
        self.sources = []
        self.steps = []
        self.revision = 0

    def show(self, phase, *, activity="", module=None, targets=(), outcome="", count=None):
        if self.publish is None:
            return
        step = {
            "phase": phase,
            "activity": activity,
            "module": MODULES.get(module, "") if isinstance(module, str) else "",
            "targets": list(targets),
            "outcome": outcome,
        }
        if count is not None:
            step["count"] = count
        if self.steps and self.steps[-1] == step:
            return
        if outcome and self.steps and self.steps[-1]["phase"] == "searching" and self.steps[-1]["outcome"] == "":
            self.steps[-1] = step
        else:
            self.steps = [*self.steps[-11:], step]
        self.revision += 1
        self.publish({
            **step,
            "revision": self.revision,
            "sources": self.sources.copy(),
            "steps": self.steps.copy(),
        })

    def read(self, tool, module, arguments, result=None):
        if tool not in TOOL_LABELS or self.publish is None:
            return
        intent = arguments.get("intent")
        activity = QUERY_LABELS.get(intent, TOOL_LABELS[tool]) if isinstance(intent, str) else TOOL_LABELS[tool]
        # Use known theme labels and short names already present in the question.
        names = _items(arguments.get("entity_names"))[:2] + [arguments.get("name"), arguments.get("entity_name")]
        theme = arguments.get("theme_id")
        targets = []
        if isinstance(theme, str) and theme in THEME_NAMES:
            targets.append(THEME_NAMES[theme])
        for name in names:
            if isinstance(name, str) and name in self.question and (label := _short_text(name)) and label not in targets:
                targets.append(label)
        if result is None:
            self.show("searching", activity=activity, module=module, targets=targets)
            return
        if not result.get("ok"):
            self.show("searching", activity=activity, module=module, targets=targets, outcome="lookup_failed")
            return
        data = result.get("data")
        if not isinstance(data, dict):
            return
        for source in _found_sources(tool, data):
            if len(self.sources) == 8:
                break
            if source not in self.sources:
                self.sources.append(source)
        outcomes = {
            "not_found": "no_match",
            "unknown": "no_match",
            "needs_clarification": "needs_clarification",
            "partial": "partial",
            "answered": "found",
            "computed": "found",
        }
        outcome = outcomes.get(data.get("status"), "lookup_done")
        count = None
        if tool in {"source_query", "read_source_query", "roguelike_query"}:
            if outcome in {"found", "partial"}:
                count = len(_items(data.get("citations"))) or None
        elif tool not in {"analysis_read", "read_evidence", "public_wiki_read"}:
            for key in ("hits", "items"):
                if isinstance(data.get(key), list):
                    count = len(data[key])
                    break
        if count is not None:
            if not count:
                outcome = "no_match"
            elif outcome != "partial":
                outcome = "found"
        self.show("searching", activity=activity, module=module, targets=targets, outcome=outcome, count=count)
