"""Source-owned Integrated Strategies documents scoped to one immutable release.

No probabilities, combat calculations, ending routes or live-game state are
inferred. Full objects and each quoted top-level field retain exact raw evidence.
"""

from __future__ import annotations

import re

from sqlalchemy import select

from arknights_agent import services
from arknights_agent.errors import Conflict, DomainError, InvalidData
from arknights_agent.models import Dataset, KnowledgeRecord
from arknights_agent.roguelike_sources import (
    ACTIVITY_FILE,
    LEGACY_FILE,
    LEGACY_THEME,
    NAME_FIELDS,
    THEME_NAMES,
    TOPIC_FILE,
)

ALIASES = {
    "刻俄柏": LEGACY_THEME, "灰蕈": LEGACY_THEME,
    "傀影": "rogue_1", "孤钻": "rogue_1",
    "水月": "rogue_2", "深蓝之树": "rogue_2",
    "萨米": "rogue_3", "银凇": "rogue_3",
    "萨卡兹": "rogue_4", "无终奇语": "rogue_4",
    "界园": "rogue_5", "岁肉鸽": "rogue_5",
    "黑流": "rogue_6", "树海": "rogue_6",
}
KINDS = {
    "theme": ("主题",), "relic": ("收藏品", "藏品"), "squad": ("分队",),
    "ending": ("结局",), "node": ("节点",), "difficulty": ("难度",),
    "stage": ("关卡",), "zone": ("区域",), "event": ("事件", "奇遇", "场景"),
    "choice": ("选项",), "initial": ("初始配置", "初始", "开局配置"),
    "rule": ("规则", "机制"), "item": ("道具",), "buff": ("buff", "黑板"),
    "month_squad": ("月度小队",), "challenge": ("深入探索", "调查条目"),
    "recruit_group": ("招募组合",), "ticket": ("招募券", "进阶券"),
}
_CONTEXT = re.compile(r"集成战略|肉鸽|roguelike|integrated strategies", re.I)
_LIST = re.compile(r"目录|列表|一览|有哪些|全部|列出|所有|清单")
_BOUNDARY = re.compile(
    r"概率|掉率|几率|期望|随机|叠加|运算|计算|总伤害|实际|实战|生效|结算|"
    r"推荐|配队|最强|强度|攻略|路线|怎么打|如何打|保证|必出|必定|"
    r"当前|现在|最新|今天|今日|正在|线上|"
    r"(?:如何|怎么|怎样)(?:获得|解锁|通关)|解锁结局|结局解锁|开启结局"
)
_FILLER = (
    "请分别", "请问", "请", "帮我", "查询", "查一下", "查看", "查", "给我", "告诉我",
    "集成战略", "肉鸽", "roguelike", "integrated strategies", "原始", "原文", "原件",
    "完整", "文档", "文案", "描述", "配置", "字段", "对象", "数据", "详情", "基本信息",
    "目录", "列表", "一览", "有哪些", "全部", "列出", "所有", "清单", "是什么", "什么",
    "包括", "包含", "内容", "名称", "使用效果", "效果", "解锁条件", "中", "的", "和", "与", "及",
    "是", "呢", "吗", "为", "仅", "只", "固定", "来源", "CN", "国服", "名称ID", "ID", "参数",
)
_DESTINATION_WORDS = ("nextSceneId", "目的场景", "结果场景", "关联场景", "后续场景")
_RELATED_WORDS = ("与其关联", "及其关联", "与其对应", "及其对应", "关联", "对应", "其")
_OBJECT_SECTIONS = {
    "relic": "items", "squad": "items", "item": "items", "buff": "relics",
    "event": "choiceScenes", "choice": "choices", "ending": "endings",
}


def _item_kind(raw, theme):
    if raw.get("type") == "BAND" or (theme == LEGACY_THEME and raw.get("type") == "RELIC"
                                     and raw.get("rarity") == "BORN"):
        return "squad"
    return "relic" if raw.get("type") == "RELIC" else "item"


def _table_pointer(theme, section):
    if theme != LEGACY_THEME:
        return f"/details/{theme}/{section}"
    return f"/itemTable/{section}" if section in {"items", "relics"} else "/" + section


def _object_pointer(theme, section, ident):
    return _table_pointer(theme, section) + "/" + ident.replace("~", "~0").replace("/", "~1")


def _sections(kind, theme):
    if kind == "ending" and theme != LEGACY_THEME:
        return ("endings", "failEndings")
    return (_OBJECT_SECTIONS[kind],)


def _ending_family(row):
    pointer = row["native_id"].partition(":")[2]
    theme = row["parent_native_id"]
    for family in ("endings", "failEndings"):
        if pointer.startswith(_table_pointer(theme, family) + "/"):
            return family
    raise Conflict("roguelike ending source family is invalid")


def _quoted_literals(text):
    # Pair delimiters so a source name containing literal curly quotes remains
    # one complete object name when wrapped in Chinese corner brackets.
    return re.finditer(r'「([^」]*)」|『([^』]*)』|【([^】]*)】|“([^”]*)”|"([^"]*)"', text)


def is_roguelike_question(question: str) -> bool:
    """Public-text routing; operator nicknames require explicit mode context."""
    if not isinstance(question, str):
        return False
    return (bool(_CONTEXT.search(question))
            or bool(re.search(r"(?<![A-Za-z0-9_])rogue_[1-6](?![A-Za-z0-9])"
                              r"|(?<![A-Za-z0-9_])act12d6(?![A-Za-z0-9_])", question, re.I))
            or any(name in question for name in THEME_NAMES.values()))


def _mask(text, spans):
    chars = list(text)
    for start, end in spans:
        chars[start:end] = " " * (end - start)
    return "".join(chars)


def _spans(text, values):
    matches = []
    for value in set(values):
        if not value:
            continue
        start = text.find(value)
        while start >= 0:
            matches.append((start, start + len(value), value))
            start = text.find(value, start + len(value))
    return [match for match in matches if not any(
        other[0] <= match[0] and other[1] >= match[1] and len(other[2]) > len(match[2])
        for other in matches
    )]


class _Answer:
    def __init__(self, session, release, question, store):
        self.session, self.release, self.store = session, release, store
        self.entry = release.manifest.get("datasets", {}).get("roguelike", {})
        self.documents = {}
        self.evidence_cache = {}
        self.snapshot_cache = {}
        self.catalog_rows = {}
        self.result = {
            "question": question, "module": "roguelike", "status": "answered", "answer": "",
            "release_id": release.id, "manifest_hash": release.manifest_hash,
            "version": {
                "release_id": release.id, "manifest_hash": release.manifest_hash,
                "source_revision": self.entry.get("source_revision"), "theme_id": None,
                "effective_date_verified": False, "runtime_mechanics_verified": False,
                "latest_availability_verified": False,
            },
            "scope": self.entry.get("scope", {}), "coverage": self.entry.get("coverage", {}),
            "conditions": {}, "claims": [], "citations": [], "records": [], "relations": [],
            "tool_trace": [],
            "limitations": [
                "回答对应固定国服客户端配置原件，未核验当前线上可用状态。",
                "描述和参数按原文展示，未推断战斗执行、随机概率或结局解锁路线。",
            ],
        }

    def refuse(self, message, status="needs_clarification"):
        self.result.update(status=status, answer=message, claims=[], citations=[], records=[],
                           relations=[], conditions={})
        return self.result

    def source_document(self, snapshot):
        if (snapshot["id"] not in self.entry["source_snapshot_ids"]
                or snapshot["revision"] != self.entry["source_revision"]):
            raise Conflict("roguelike original snapshot is outside the pinned release")
        digest = snapshot["content_hash"]
        if digest not in self.documents:
            raw = self.store.read_verified(digest)
            if len(raw) != snapshot["byte_size"]:
                raise Conflict("roguelike source byte size differs")
            self.documents[digest] = services.snapshot_document(raw, snapshot["media_type"])
        return self.documents[digest]

    def source_table(self, theme, section, *, optional=False):
        filename = LEGACY_FILE if theme == LEGACY_THEME else TOPIC_FILE
        snapshots = []
        for ident in self.entry["source_snapshot_ids"]:
            if ident not in self.snapshot_cache:
                self.snapshot_cache[ident] = services.get_snapshot(self.session, self.release, ident)
            snapshot = self.snapshot_cache[ident]
            if snapshot["source_key"].rsplit("/", 1)[-1] == filename:
                snapshots.append(snapshot)
        if len(snapshots) != 1:
            raise Conflict("roguelike original table snapshot is ambiguous or absent")
        document = self.source_document(snapshots[0])
        path = _table_pointer(theme, section)
        if optional:
            parent, _, key = path.rpartition("/")
            container = services.json_pointer(document, parent)
            if isinstance(container, dict) and key not in container:
                return {}
        table = services.json_pointer(document, path)
        if not isinstance(table, dict):
            raise Conflict("roguelike original object section is not a complete table")
        return table

    def verify_catalog(self, theme, kind, catalog):
        """Certify membership from raw tables, rather than a truncated DB count."""
        if kind not in _OBJECT_SECTIONS:
            return
        expected = set()
        for section in _sections(kind, theme):
            table = self.source_table(theme, section, optional=section == "failEndings")
            for ident, raw in table.items():
                if not isinstance(raw, dict) or raw.get("id", ident) != ident:
                    raise Conflict("roguelike original table key/object identity differs")
                if section == "items" and _item_kind(raw, theme) != kind:
                    continue
                expected.add(theme + ":" + _object_pointer(theme, section, ident))
        actual = [row["native_id"] for row in catalog if row["parent_native_id"] == theme
                  and row["kind"] == "roguelike_" + kind]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise Conflict("roguelike normalized catalog differs from complete original membership")

    def catalog(self):
        dataset = self.session.get(Dataset, self.entry["dataset_id"])
        if (
            services.content_digest(self.release.manifest) != self.release.manifest_hash
            or dataset is None or dataset.module != "roguelike" or dataset.status != "sealed"
            or dataset.source_revision != self.entry.get("source_revision")
            or dataset.fingerprint != self.entry.get("fingerprint")
        ):
            raise Conflict("roguelike release/dataset identity differs")
        rows = list(self.session.execute(select(
            KnowledgeRecord.id, KnowledgeRecord.kind, KnowledgeRecord.native_id,
            KnowledgeRecord.name, KnowledgeRecord.parent_native_id,
            KnowledgeRecord.payload["id"].as_string().label("source_id"),
        ).where(KnowledgeRecord.dataset_id == dataset.id,
                KnowledgeRecord.module == "roguelike").order_by(
                    KnowledgeRecord.kind, KnowledgeRecord.native_id,
                )).mappings())
        self.catalog_rows = {row["id"]: row for row in rows}
        return rows

    def record(self, ident):
        record = services.get_record(self.session, self.release, ident)
        expected = self.catalog_rows.get(ident)
        if expected is not None and any(record[field] != expected[field] for field in (
            "id", "kind", "native_id", "name", "parent_native_id",
        )):
            raise Conflict("roguelike selected document metadata differs from its catalog")
        return record

    def cite(self, record, selector, expected):
        if (
            record["module"] != "roguelike" or record["dataset_id"] != self.entry["dataset_id"]
            or not isinstance(record["data"], dict)
        ):
            raise Conflict("roguelike record is outside the pinned dataset")
        if not services.same_json_value(services.json_pointer(record["data"], selector), expected):
            raise Conflict("roguelike requested field differs from complete record")
        if len(record["evidence_ids"]) != 1:
            raise Conflict("roguelike record requires one exact original object evidence")
        evidence_id = record["evidence_ids"][0]
        if evidence_id not in self.evidence_cache:
            self.evidence_cache[evidence_id] = services.get_evidence(
                self.session, self.release, evidence_id,
            )
        evidence = self.evidence_cache[evidence_id]
        if evidence["snapshot_id"] not in self.snapshot_cache:
            self.snapshot_cache[evidence["snapshot_id"]] = services.get_snapshot(
                self.session, self.release, evidence["snapshot_id"],
            )
        snapshot = self.snapshot_cache[evidence["snapshot_id"]]
        if (
            evidence["dataset_id"] != record["dataset_id"]
            or evidence["snapshot_id"] not in self.entry["source_snapshot_ids"]
            or evidence["source_revision"] != self.entry["source_revision"]
            or snapshot["revision"] != self.entry["source_revision"]
            or evidence["source_url"] != snapshot["url"]
            or not services.same_json_value(evidence["excerpt"], record["data"])
            or services.content_digest(evidence["excerpt"]) != evidence["excerpt_hash"]
        ):
            raise Conflict("roguelike complete object evidence identity differs")
        pointer = evidence["pointer"]
        filename = snapshot["source_key"].rsplit("/", 1)[-1]
        if record["kind"] == "roguelike_theme":
            theme = record["native_id"]
            wanted = f"/basicInfo/{theme}" if theme == LEGACY_THEME else f"/topics/{theme}"
            wanted_file = ACTIVITY_FILE if theme == LEGACY_THEME else TOPIC_FILE
            if (pointer != wanted or filename != wanted_file or record["parent_native_id"] is not None
                    or record["data"].get("id") != theme or record["data"].get("name") != record["name"]):
                raise Conflict("roguelike theme original identity differs")
        else:
            theme = record["parent_native_id"]
            if theme not in THEME_NAMES or record["native_id"] != theme + ":" + pointer:
                raise Conflict("roguelike source family/pointer identity differs")
            if theme == LEGACY_THEME:
                if filename != LEGACY_FILE:
                    raise Conflict("legacy roguelike object belongs to another table")
            elif filename != TOPIC_FILE or not any(pointer.startswith(f"/{section}/{theme}/")
                                                     for section in ("details", "modules", "customizeData")):
                raise Conflict("roguelike object belongs to another theme namespace")
            if record["kind"] != "roguelike_buff":
                source_name = next((record["data"][field] for field in NAME_FIELDS
                                    if isinstance(record["data"].get(field), str)
                                    and record["data"][field].strip()),
                                   pointer.rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~"))
                if record["name"] != source_name:
                    raise Conflict("roguelike object name differs from its raw name/key")
            if record["kind"] in {"roguelike_relic", "roguelike_squad"}:
                squad = record["data"].get("type") == "BAND" or (
                    theme == LEGACY_THEME and record["data"].get("type") == "RELIC"
                    and record["data"].get("rarity") == "BORN"
                )
                expected_kind = "roguelike_squad" if squad else "roguelike_relic"
                if record["kind"] != expected_kind or record["data"].get("type") not in {"RELIC", "BAND"}:
                    raise Conflict("roguelike relic/squad original classification differs")
            kind = record["kind"].removeprefix("roguelike_")
            source_sections = [(key, value) for key, value in _OBJECT_SECTIONS.items()]
            source_sections.append(("ending", "failEndings"))
            for source_kind, section in source_sections:
                prefix = _table_pointer(theme, section) + "/"
                if pointer.startswith(prefix) and "/" not in pointer[len(prefix):]:
                    wanted_kind = (_item_kind(record["data"], theme)
                                   if section == "items" else source_kind)
                    if kind != wanted_kind:
                        raise Conflict("roguelike source section has been assigned another object kind")
            if kind in _OBJECT_SECTIONS:
                ident = record["data"].get("id")
                if kind == "ending" and ident is None:
                    # Preserve dictionary-key identity for source rows without an id field.
                    ident = pointer.rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~")
                if (not isinstance(ident, str) or not ident or pointer not in {
                    _object_pointer(theme, section, ident) for section in _sections(kind, theme)
                }):
                    raise Conflict("roguelike object kind differs from its raw source section")
                if kind == "item" and _item_kind(record["data"], theme) != kind:
                    raise Conflict("roguelike item original classification differs")
                if kind == "buff" and not isinstance(record["data"].get("buffs"), list):
                    raise Conflict("roguelike buff original list is absent")
        digest = snapshot["content_hash"]
        document = self.source_document(snapshot)
        original = services.json_pointer(document, pointer)
        full_pointer = pointer + selector
        if not services.same_json_value(original, evidence["excerpt"]) or not services.same_json_value(
            services.json_pointer(document, full_pointer), expected,
        ):
            raise Conflict("roguelike source original object/field differs")
        for citation in self.result["citations"]:
            if citation["snapshot_id"] == snapshot["id"] and citation["pointer"] == full_pointer:
                return citation["id"]
        citation = {
            "id": f"c{len(self.result['citations']) + 1}", "record_id": record["id"],
            "dataset_id": record["dataset_id"], "evidence_id": evidence_id,
            "snapshot_id": snapshot["id"], "snapshot_hash": digest,
            "source_url": snapshot["url"], "source_revision": snapshot["revision"],
            "source_media_type": snapshot["media_type"], "pointer": full_pointer,
            "evidence_pointer": pointer, "excerpt_hash": evidence["excerpt_hash"],
            "field_value": expected, "field_hash": services.content_digest(expected),
            "selector_semantics": "RFC6901_on_original_JSON",
        }
        self.result["citations"].append(citation)
        return citation["id"]

    def emit(self, record):
        if any(item["id"] == record["id"] for item in self.result["records"]):
            return
        citation = self.cite(record, "", record["data"])
        self.result["records"].append(record)
        self.result["claims"].append({
            "id": f"f{len(self.result['claims']) + 1}", "record_id": record["id"],
            "label": "原始完整对象", "value": record["data"], "citation_ids": [citation],
            "verification_status": "fixed_client_source_document",
        })
        for field, value in record["data"].items():
            selector = "/" + field.replace("~", "~0").replace("/", "~1")
            citation = self.cite(record, selector, value)
            self.result["claims"].append({
                "id": f"f{len(self.result['claims']) + 1}", "record_id": record["id"],
                "label": field, "value": value, "citation_ids": [citation],
                "verification_status": "fixed_client_source_document",
            })

    def link(self, source, target, *, kind, selector, reference):
        """Two raw fields prove an edge; neither object becomes a joined quote."""
        if any(row["kind"] == kind and row["source_record_id"] == source["id"]
               and row["target_record_id"] == target["id"] for row in self.result["relations"]):
            return
        self.result["relations"].append({
            "kind": kind, "source_record_id": source["id"], "target_record_id": target["id"],
            "theme_id": source["parent_native_id"], "reference_field": selector,
            "reference_value": reference,
            "citation_ids": [self.cite(source, selector, reference),
                             self.cite(target, "/id", reference)],
            "verification_status": "fixed_client_source_reference",
        })


def _types(text):
    spans = _spans(text, [word for words in KINDS.values() for word in words])
    return {kind for kind, words in KINDS.items() if any(value in words for _, _, value in spans)}


def _strip_query(text, more=()):
    for token in sorted({*_FILLER, *more, *(word for words in KINDS.values() for word in words)},
                        key=lambda value: (-len(value), value)):
        text = re.sub(re.escape(token), "", text, flags=re.I)
    return re.sub(r"[\s，,。.!！?？、；;：:\"'“”‘’「」『』【】\[\]（）()<>\\/\-]+", "", text)


def answer_roguelike(session, release, question: str, *, store=None) -> dict:
    """Answer a complete, unambiguous source document request without outbound IO."""
    if not isinstance(question, str) or not question.strip() or len(question) > 2000:
        raise InvalidData("question must contain 1 to 2000 characters")
    answer = _Answer(session, release, question, store)
    if not answer.entry:
        return answer.refuse("该发布版本未包含集成战略资料，不能借用其他发布或线上来源。", "unsupported")
    if store is None:
        return answer.refuse("集成战略文档需要回放固定来源原件；本次未提供快照存储。", "needs_verification")
    try:
        catalog = answer.catalog()
        themes = [row for row in catalog if row["kind"] == "roguelike_theme"]
        declared = answer.result["scope"].get("theme_ids")
        if (not isinstance(declared, list) or set(declared) != {row["native_id"] for row in themes}
                or len(themes) != len(set(declared))):
            raise Conflict("roguelike declared theme catalog is incomplete")
        from arknights_agent.roguelike_guides import try_guide

        guided = try_guide(answer, catalog, question)
        if guided is not None:
            return guided
        objects = [row for row in catalog if row["kind"] not in {"roguelike_theme", "roguelike_wiki"}]
        title_spans = _spans(question, [row["name"] for row in objects] + [row["native_id"] for row in objects]
                             + [row["source_id"] for row in objects if isinstance(row["source_id"], str)])
        # Client items include operator names and very short event labels. Those
        # substrings cannot eat a longer explicit theme or a type selector.
        reserved = _spans(question, [row["name"] for row in themes] + [row["native_id"] for row in themes]
                          + [word for words in KINDS.values() for word in words])
        reserved.extend((match.start(), match.end(), match[0])
                        for match in re.finditer(r"难度(?:等级)?\s*[0-9]+", question))
        contextual_aliases = []
        for alias in ALIASES:
            for match in re.finditer(re.escape(alias), question):
                before, after = question[:match.start()], question[match.end():]
                if re.match(r"\s*(?:的)?(?:肉鸽|集成战略)", after) or re.search(
                    r"(?:肉鸽|集成战略|主题)\s*$", before,
                ):
                    contextual_aliases.append((match.start(), match.end(), alias))
        reserved.extend(contextual_aliases)
        title_spans = [span for span in title_spans if not any(
            other[0] <= span[0] and other[1] >= span[1] and len(other[2]) >= len(span[2])
            for other in reserved
        )]
        semantic = _mask(question, [(start, end) for start, end, _ in title_spans])
        theme_spans = _spans(semantic, [row["name"] for row in themes] + [row["native_id"] for row in themes])
        selected_themes = {row["native_id"] for row in themes if any(
            value in (row["name"], row["native_id"]) for _, _, value in theme_spans
        )}
        alias_spans = []
        if _CONTEXT.search(semantic):
            alias_spans = [span for span in contextual_aliases
                           if semantic[span[0]:span[1]] == span[2]]
            selected_themes.update(ALIASES[value] for _, _, value in alias_spans)
        semantic = _mask(semantic, [(start, end) for start, end, _ in [*theme_spans, *alias_spans]])
        if _BOUNDARY.search(semantic):
            return answer.refuse("固定原文不能证明随机概率、实战叠加、结局解锁攻略或当前线上状态。", "unsupported")
        if re.search(r"endingDetailList|endingRelicDetailList|局末摘要|局末统计", semantic):
            return answer.refuse("该发布尚未规范化局末摘要模板，不能把它们补接为某个结局的关联文档或解锁路线。", "unsupported")
        if len(selected_themes) > 1:
            return answer.refuse("问题包含多个集成战略主题；请一次指定一个主题与一种文档类型。")
        if selected_themes and not selected_themes.issubset(set(declared)):
            return answer.refuse("指定主题不在该发布版本的覆盖范围内。", "unsupported")
        theme = next(iter(selected_themes)) if selected_themes else None
        ending_families = set()
        family_spans = []
        for match in re.finditer(r"(失败|正常|普通|常规|成功)\s*(?=结局)", semantic):
            ending_families.add("failEndings" if match[1] == "失败" else "endings")
            family_spans.append((match.start(), match.end()))
        if len(ending_families) > 1:
            return answer.refuse("问题同时限定正常和失败结局；请分别查询，或请求不限定类别的完整结局目录。")
        ending_family = next(iter(ending_families)) if ending_families else None
        semantic = _mask(semantic, family_spans)
        kinds = _types(semantic)
        related_request = None
        if kinds in ({"relic", "buff"}, {"squad", "buff"}, {"item", "buff"}):
            related_request = "item_buff"
            kinds = kinds - {"buff"}
        elif kinds == {"choice", "event"} and any(word in semantic for word in _DESTINATION_WORDS):
            related_request = "choice_next_scene"
            kinds = {"choice"}
        elif kinds == {"choice", "event"} and re.search(r"(?:所有|全部|完整)\s*选项", semantic):
            return answer.refuse("固定场景原件没有起始菜单的完整选项成员关系；nextSceneId只能证明选项指向的场景。",
                                 "unsupported")
        if len(kinds) > 1:
            return answer.refuse("问题同时请求多类资料；请分别查询，以免遗漏对象或条件。")
        kind = next(iter(kinds)) if kinds else None
        title_values = {value for _, _, value in title_spans}
        matches = [row for row in objects if any(
            value in title_values for value in (row["name"], row["native_id"], row["source_id"])
        )]
        if not theme:
            id_themes = {row["parent_native_id"] for row in matches if any(
                isinstance(value, str) and value in title_values
                for value in (row["native_id"], row["source_id"])
            )}
            if len(id_themes) == 1:
                theme = next(iter(id_themes))
        if theme:
            matches = [row for row in matches if row["parent_native_id"] == theme]
        if kind:
            matches = [row for row in matches if row["kind"] == f"roguelike_{kind}"]
        id_values = {value for value in title_values if any(
            value in (row["native_id"], row["source_id"]) for row in objects
        )}
        if id_values:
            # A supplied exact ID disambiguates a repeated name. Every remaining
            # literal still has to describe this same row below; no union fallback.
            matches = [row for row in matches if any(
                value in (row["native_id"], row["source_id"]) for value in id_values
            )]
        if ending_family:
            matches = [row for row in matches if row["kind"] == "roguelike_ending"
                       and _ending_family(row) == ending_family]
        matched_titles = {value for value in title_values if any(
            value in (row["name"], row["native_id"], row["source_id"]) for row in matches
        )}
        if title_values - matched_titles:
            return answer.refuse("问题同时指定了其他主题或文档类型的对象；请分别查询完整名称。")
        for match in _quoted_literals(question):
            literal = next(value for value in match.groups() if value is not None)
            if not any(start <= match.start() and end >= match.end() for start, end, _ in title_spans):
                if literal not in {row["name"] for row in objects} | {row["native_id"] for row in objects} | {
                    row["source_id"] for row in objects if isinstance(row["source_id"], str)
                }:
                    return answer.refuse("明确指定的对象名称或ID未匹配固定主题目录；请核对完整名称。")
        if not theme:
            if kind == "theme" or (_CONTEXT.search(semantic) and _LIST.search(semantic) and not kinds and not title_values):
                if _strip_query(semantic):
                    return answer.refuse("目录请求含未支持的附加条件，请明确查询固定主题目录。")
                selected = [answer.record(row["id"]) for row in themes]
                answer.result["conditions"] = {"selection": "complete_theme_catalog", "theme_ids": declared,
                                                  "record_kind": "roguelike_theme", "total_count": len(selected)}
            else:
                return answer.refuse("请指定集成战略主题完整名称，或在肉鸽语境中使用主题简称。")
        elif kind == "theme" or (not kinds and not title_values and not _strip_query(semantic)):
            if _strip_query(semantic):
                return answer.refuse("主题文档请求含未支持的条件或额外问题；请分别查询。")
            selected = [answer.record(row["id"]) for row in themes if row["native_id"] == theme]
            answer.result["conditions"] = {"selection": "theme_document", "theme_ids": [theme],
                                              "record_kind": "roguelike_theme", "total_count": 1}
        else:
            if not kind and matches:
                item_matches = [row for row in matches if row["kind"] != "roguelike_buff"]
                matches = item_matches or matches
                implied = {row["kind"] for row in matches}
                if len(implied) != 1:
                    return answer.refuse("同名对象属于多种资料类型，请补充文档类型或完整原始ID。")
                kind = next(iter(implied)).removeprefix("roguelike_")
            grade = re.search(r"难度(?:等级)?\s*([0-9]+)", semantic) if kind == "difficulty" else None
            if grade:
                semantic = semantic[:grade.start()] + " " * len(grade[0]) + semantic[grade.end():]
            if not kind:
                return answer.refuse("请指定藏品、分队、结局、节点等文档类型或唯一对象名称。")
            if title_values and len(matches) != 1:
                return answer.refuse("对象名称包含多个匹配或未匹配项，请使用完整名称或唯一原始ID分别查询。")
            if title_values:
                selected = [answer.record(matches[0]["id"])]
                selection = "exact_name_or_native_id"
            else:
                if not _LIST.search(semantic) and not grade:
                    return answer.refuse("请提供唯一对象名称，或明确请求该类型的完整目录。")
                selected = [answer.record(row["id"]) for row in objects
                            if row["parent_native_id"] == theme and row["kind"] == f"roguelike_{kind}"]
                selection = "complete_catalog"
            if ending_family:
                selected = [row for row in selected if _ending_family(row) == ending_family]
            if grade:
                selected = [row for row in selected if type(row["data"].get("grade")) is int
                            and row["data"]["grade"] == int(grade[1])]
                if len(selected) != 1:
                    return answer.refuse("该难度等级存在多个模式或无匹配项，请补充唯一原始ID。")
            fields = {key for row in selected for key in row["data"]}
            request_words = [*fields]
            if related_request == "item_buff":
                request_words.extend(_RELATED_WORDS)
            if kind == "choice":
                request_words.extend([*_DESTINATION_WORDS, *_RELATED_WORDS, "指向"])
            if _strip_query(semantic, request_words):
                return answer.refuse("问题含未匹配的对象、附加条件或第二项请求；请分别查询。")
            if not selected:
                return answer.refuse("该发布版本的指定主题没有这类已规范化文档。", "unsupported")
            answer.result["conditions"] = {"selection": selection, "theme_ids": [theme],
                                              "record_kind": f"roguelike_{kind}", "total_count": len(selected)}
            if grade:
                answer.result["conditions"]["difficulty_grade"] = int(grade[1])
            if kind == "ending":
                answer.result["conditions"]["ending_source_families"] = {
                    family: sum(_ending_family(row) == family for row in selected)
                    for family in ("endings", "failEndings")
                    if any(_ending_family(row) == family for row in selected)
                }
                if ending_family:
                    answer.result["conditions"]["ending_source_family_condition"] = ending_family
            answer.verify_catalog(theme, kind, objects)
        if related_request:
            answer.result["conditions"]["related_document_request"] = related_request
        verified_associations = set()
        for record in selected:
            answer.emit(record)
            if record["kind"] in {"roguelike_relic", "roguelike_squad", "roguelike_item", "roguelike_buff"}:
                ident = record["data"].get("id")
                target_kinds = {"roguelike_buff"} if record["kind"] != "roguelike_buff" else {
                    "roguelike_relic", "roguelike_squad", "roguelike_item",
                }
                if not isinstance(ident, str) or not ident:
                    raise Conflict("roguelike item/buff original ID is absent")
                target_section = "relics" if record["kind"] != "roguelike_buff" else "items"
                related_table = answer.source_table(record["parent_native_id"], target_section)
                target_native = record["parent_native_id"] + ":" + _object_pointer(
                    record["parent_native_id"], target_section, ident,
                )
                linked = [answer.record(row["id"]) for row in objects
                          if row["parent_native_id"] == record["parent_native_id"]
                          and row["kind"] in target_kinds
                          and row["native_id"] == target_native]
                linked = [row for row in linked if row["data"].get("id") == ident]
                expected_count = int(ident in related_table)
                if (len(linked) != expected_count
                        or (record["kind"] == "roguelike_buff" and expected_count != 1)):
                    raise Conflict("roguelike item/buff original ID association is ambiguous")
                if related_request == "item_buff" and not expected_count:
                    return answer.refuse("该对象的固定原件没有对应的buff配置，不能补写参数。", "unsupported")
                for related in linked:
                    if related["name"] != record["name"]:
                        raise Conflict("roguelike item/buff name association differs")
                    answer.emit(related)
                    item, buff = ((related, record) if record["kind"] == "roguelike_buff"
                                  else (record, related))
                    answer.link(item, buff, kind="item_buff", selector="/id", reference=ident)
                    verified_associations.add("item_buff_same_theme_exact_id")
            if record["kind"] == "roguelike_choice" and "nextSceneId" in record["data"]:
                destination = record["data"]["nextSceneId"]
                if destination is not None:
                    if not isinstance(destination, str) or not destination:
                        raise Conflict("roguelike choice destination ID is invalid")
                    destination_native = record["parent_native_id"] + ":" + _object_pointer(
                        record["parent_native_id"], "choiceScenes", destination,
                    )
                    linked = [row for row in objects if row["parent_native_id"] == record["parent_native_id"]
                              and row["kind"] == "roguelike_event" and row["native_id"] == destination_native]
                    if len(linked) != 1:
                        raise Conflict("roguelike choice destination original document is absent or ambiguous")
                    related = answer.record(linked[0]["id"])
                    answer.emit(related)
                    answer.link(record, related, kind="choice_next_scene", selector="/nextSceneId",
                                reference=destination)
                    verified_associations.add("choice_nextSceneId_to_destination_scene_only")
        if any(row["kind"] in {"roguelike_event", "roguelike_choice"} for row in selected):
            answer.result["conditions"]["originating_event_menu_verified"] = False
            answer.result["limitations"].append(
                "nextSceneId仅证明选项指向的场景；原件未提供场景起始菜单的完整选项成员关系。",
            )
        answer.result["conditions"]["verified_associations"] = sorted(verified_associations)
        answer.result["version"]["theme_id"] = theme
        answer.result["theme_id"] = theme
        answer.result["tool_trace"].append({
            "tool": "roguelike_source_document", "release_id": release.id,
            "source_revision": answer.entry["source_revision"], "theme_id": theme,
            "conditions": answer.result["conditions"], "record_count": len(answer.result["records"]),
            "snapshot_verification": "sha256_complete_object_and_exact_field_pointer",
            "runtime_calculation": False, "catalog_truncated": False,
        })
        name = THEME_NAMES.get(theme, "集成战略固定主题目录")
        names = "、".join(record["name"] for record in selected)
        lines = [f"{name}：共{len(selected)}条指定文档，完整目录/原始对象：{names}。"]
        if len(selected) == 1:
            for field in ("usage", "description", "desc", "functionDesc", "ruleDesc", "unlockCondDesc"):
                if field in selected[0]["data"]:
                    value = selected[0]["data"][field]
                    lines.append(f"{field}：{value if value is not None else 'null（原件未提供）'}")
        lines.append(f"固定来源修订：{answer.entry['source_revision']}；知识发布：{release.id}。")
        lines.append("全部字段（含null）及关联配置保留在records和claims，逐字段证据见citations。")
        answer.result["answer"] = "\n".join(lines)
        return answer.result
    except DomainError:
        return answer.refuse("固定来源原件、完整对象或主题关联校验失败，本次暂不回答。", "needs_verification")
