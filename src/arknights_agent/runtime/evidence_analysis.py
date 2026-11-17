"""Model-led analysis over replayable evidence from one published release.

The model chooses searches and explains implications. This boundary authorizes
only literal source observations: generated analysis is never a verified fact.
No user-language classifier, network client, or model call lives in this module.
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from math import log
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from arknights_agent import rag, services
from arknights_agent.errors import Conflict, InvalidData
from arknights_agent.models import KnowledgeRecord
from arknights_agent.roguelike import _Answer
from arknights_agent.runtime.dialogue_context import attach_state, previous_questions
from arknights_agent.wiki_text import plain

Theme = Literal["act12d6", "rogue_1", "rogue_2", "rogue_3", "rogue_4", "rogue_5", "rogue_6"]
ShortText = Annotated[str, Field(min_length=1, max_length=1000)]
EvidenceRef = Annotated[str, Field(min_length=1, max_length=80)]
MAX_EVIDENCE = 64
MAX_STRUCTURE_CHARS = 600
REGISTERED_SOURCES_FORMAT = "analysis-registered-sources-v1"
# Field families observed in the pinned client schema. These classify source
# fields, never user language: enum/identity/blackboard slots are not prose.
_EXPLANATORY_FIELDS = frozenset({"usage", "description", "desc", "tip", "info", "text",
                                "obtainApproach", "teamDes", "challengeDes", "taskDes"})
_EXPLANATORY_SUFFIXES = ("Desc", "Description", "Usage", "Tips", "Text")
_CLIENT_TEXT_ROLE = "player_explanatory_text"
_WIKI_TEXT_ROLE = "published_wiki_passage"


class AnalysisBoundaryError(InvalidData):
    """Only fixed, non-sensitive model correction guidance crosses this edge."""

    def __init__(self, code, detail, *, fields=None):
        super().__init__(detail)
        self.repair = {"code": code, "detail": detail, "fields": fields or []}

    def feedback(self, context):
        return _repair_feedback(context, self.repair)


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("*", mode="after", check_fields=False)
    @classmethod
    def no_blank_strings(cls, value):
        if isinstance(value, str) and not value.strip():
            raise ValueError("blank argument")
        return value


class AnalysisSearchArguments(_Arguments):
    query: ShortText = Field(description="由模型选择的来源检索词；不要求用户提供原始ID。")
    subqueries: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=2,
        description="模型可自主补充至多两条分解检索词，与query合计最多三条实际查询。"
        "可分别检索不同对象、机制或范围假设；系统不自动改写用户说法、补同义词或确认假设。"
        "所有查询共享一次源扫描、总limit和来源校验；按query_coverage和词面命中诊断自行消歧。")
    theme_id: Theme | None = None
    limit: int = Field(default=5, ge=1, le=8)
    offset: int = Field(default=0, ge=0, le=128)

    @field_validator("subqueries")
    @classmethod
    def nonblank_subqueries(cls, values):
        if any(not value.strip() for value in values):
            raise ValueError("blank subquery")
        return values


class AnalysisReadArguments(_Arguments):
    evidence_ref: EvidenceRef = Field(description="本运行analysis_search或analysis_read实际返回的引用。")
    offset: int | None = Field(default=None, ge=0,
        description="同一原始字段的Unicode字符位置；null或省略从已注册片段起点读取。")
    length: int = Field(default=1200, ge=1, le=2000,
        description="读取字符数，默认1200，最多2000；不改变原文或跨来源字段。")


class AnalysisRequestQuote(_Arguments):
    question_index: int = Field(ge=0, le=127)
    quote: Annotated[str, Field(min_length=1, max_length=2000)]


class AnalysisObservation(_Arguments):
    evidence_ref: EvidenceRef
    quote: Annotated[str, Field(min_length=1, max_length=2000)] | None = Field(default=None,
        description="通常省略：服务器从真实引用组装原文。旧协议可传逐字子串，不可改写。")
    start: int | None = Field(default=None, ge=0,
        description="可选：相对于该引用quote的Unicode起点；与end一起选取连续原文。")
    end: int | None = Field(default=None, ge=1,
        description="可选：相对于该引用quote的结束位置（不含）；不填则引用完整已读片段。")


class EvidenceGroup(_Arguments):
    question: ShortText
    observations: list[AnalysisObservation] = Field(min_length=1, max_length=8)
    interpretation: ShortText = Field(description="模型解释对象、字段角色、主题、适用条件及与任务的关系；不是服务器确认的事实。")
    missing_evidence: list[ShortText] = Field(default_factory=list, max_length=8)


class OrganizeEvidenceArguments(_Arguments):
    groups: list[EvidenceGroup] = Field(min_length=1, max_length=8)


class TaskPlanArguments(_Arguments):
    complexity: Literal['simple', 'complex'] = Field(default='complex',
        description='由模型判断：单条事实、固定字段或有确定条件的计算为simple；多机制推理、方案取舍、证据冲突为complex。缺省按complex。')
    evidence_scope: ShortText | None = Field(default=None,
        description='能回答当前问题的最小取证范围；只列必要对象、字段、条件，避免附带无关目录或所有组合。')
    task: ShortText = Field(description="结合全部用户问题说明当前任务；补充主题和条件是前轮任务的延续。")
    question_indices: list[Annotated[int, Field(ge=0, le=127)]] = Field(min_length=1, max_length=128,
        description="真实完整原问链的0基索引，必须包含当前问题的索引；第2轮当前索引为1，不能只填原始第0轮。")
    context_resolution: ShortText = Field(description="结合原问链解释指代、简称、对象、用户纠正和条件变更；未经来源确认的映射明确标成假设。")
    conditions: list[ShortText] = Field(default_factory=list, max_length=16)
    evidence_questions: list[ShortText] = Field(min_length=1, max_length=12,
        description="需要查证什么，先定义机制链和判定依据，再选搜索词；模型记忆只能生成待验证假设。")
    search_queries: list[ShortText] = Field(min_length=1, max_length=12)
    uncertainties: list[ShortText] = Field(default_factory=list, max_length=12)
    requested_object_type: ShortText | None = Field(default=None,
        description="本轮所问对象类别，由模型结合完整上下文判断；用户最新纠正优先，不能沿用已否定类别。")
    rejected_object_types: list[ShortText] = Field(default_factory=list, max_length=8,
        description="用户明确否定的对象类别；仅记原问证据，不根据模型猜测新增。")


def plan_task(context, args):
    args = _validated(TaskPlanArguments, args)
    questions = [*previous_questions(context.previous_answer), context.original_question]
    if max(args.question_indices) >= len(questions) or len(questions) - 1 not in args.question_indices:
        raise AnalysisBoundaryError("analysis_plan_question_indices_invalid",
            "plan must reference the real question chain including the current question",
            fields=[{"loc": ["question_indices"], "question_index": len(questions) - 1}])
    _registry(context)
    plan = {**args.model_dump(), "original_question_count": len(questions),
            "original_questions_sha256": services.content_digest(questions),
            "origin": "model_task_plan", "hypotheses_are_verified_facts": False}
    context.analysis_evidence["task_plan"] = copy.deepcopy(plan)
    return {"ok": True, "data": plan}


def organize(context, args):
    args = _validated(OrganizeEvidenceArguments, args)
    # Reuse the exact final source boundary: every selected original is replayed
    # against the published snapshot, even though this is only a reading step.
    groups = []
    for group in args.groups:
        selected = answer(context, AnalysisAnswerArguments(
            request_summary=group.question,
            request_quotes=[AnalysisRequestQuote(question_index=len(previous_questions(context.previous_answer)),
                                                quote=context.original_question)],
            observations=group.observations))['data']
        groups.append({"question": group.question, "model_interpretation": group.interpretation,
                       "missing_evidence": group.missing_evidence,
                       "source_observations": selected["source_observations"],
                       "registered_sources": selected["registered_sources"],
                       "interpretation_verified_by_server": False})
    result = {"groups": groups, "originals_assembled_by_server": True,
              "next_step": "模型核对对象/字段/条件与原任务；缺证据继续检索读取，否则据此生成回答。"}
    context.analysis_evidence["organized_evidence"] = copy.deepcopy(result)
    return {"ok": True, "data": result}


class AnalysisInference(_Arguments):
    text: Annotated[str, Field(min_length=1, max_length=600)]
    evidence_refs: list[EvidenceRef] = Field(min_length=1, max_length=8)


class AnalysisAnswerArguments(_Arguments):
    theme_id: Theme | None = None
    request_summary: ShortText = Field(description="完整当前任务，包括真实前轮尚未完成的分析请求。")
    request_quotes: list[AnalysisRequestQuote] = Field(min_length=1, max_length=12)
    observations: list[AnalysisObservation] = Field(default_factory=list, max_length=12)
    analysis: list[AnalysisInference] = Field(default_factory=list, max_length=8)
    limitations: list[Annotated[str, Field(min_length=1, max_length=600)]] = Field(
        default_factory=list, max_length=8,
        description="无法由本次真实证据确认的机制、阈值、适用范围或实战结论；查不到须明确说明。")
    follow_up_question: Annotated[str, Field(min_length=1, max_length=600)] | None = Field(
        default=None, description="确有必要时向用户提出一个自然、具体的补充问题；不要索要内部ID或重复已提供的信息。")
    review_response: ShortText | None = Field(default=None,
        description="修订时简述哪些复核问题已根据真实证据纠正、哪些意见与原文不符；此解释不是新证据。")


class ModelFirstAnalysisAnswerArguments(AnalysisAnswerArguments):
    observations: list[AnalysisObservation] = Field(default_factory=list, max_length=MAX_EVIDENCE,
        description="可省略；服务器按analysis.evidence_refs组装本运行已登记原文。"
        "仅需选择更短连续片段时提供start/end，不必重新抄写Wiki。")


def _validated(cls, args):
    return args if isinstance(args, cls) else cls.model_validate(args)


def _registry(context):
    registry = context.analysis_evidence
    if not isinstance(registry, dict):
        raise AnalysisBoundaryError("analysis_registry_unavailable", "analysis evidence registry is unavailable")
    identity = {"release_id": context.release.id, "manifest_hash": context.release.manifest_hash,
                "question": context.original_question, "previous_run_id": context.previous_run_id}
    if not registry:
        registry.update(identity=identity, items={})
    if registry.get("identity") != identity or not isinstance(registry.get("items"), dict):
        raise Conflict("analysis evidence belongs to another request or release")
    return registry["items"]


_REPAIR_INSTRUCTIONS = {
    "analysis_plan_question_indices_invalid": "question_indices是完整真实用户问链的0基索引，必须包含fields标明的当前question_index；保留相关前轮索引，所有索引须在真实问链范围内。只纠正索引，不丢弃已有任务、对象或条件。",
    "analysis_registry_unavailable": "本运行证据登记不可用，不能编造引用或沿用其它运行引用。",
    "analysis_registry_limit": "本运行引用数量已达上限；使用已有真实引用，不重置登记或计数。",
    "analysis_non_explanatory_source": "来源不是已核验的解释文案；请检索效果、条件或难度说明，不用结构标识代替事实。",
    "analysis_unknown_evidence_reference": "只用本运行实际返回的analysis引用；先检索或读取真实引用，不能编造来源ID。",
    "analysis_read_offset_out_of_range": "offset必须位于该真实引用的原始字段内；按返回的field_length和分页位置读取。",
    "analysis_nonliteral_request_quote": "按fields指定的原问索引逐字引用真实用户原话，不使用来源文案或改写后的问题。",
    "analysis_missing_current_question_quote": "request_quotes必须包含本轮真实用户原话；保留相关前轮要求和全部条件。",
    "analysis_missing_observation": "fields所列推论引用没有对应原文observation；读取该真实引用后增加逐字依据，或撤去缺少依据的推论，不编造引文。",
    "analysis_evidence_theme_mismatch": "该observation不属于所选主题；核对真实来源与任务范围，不改引用身份来通过校验。",
    "analysis_nonliteral_observation": "fields所列quote不是对应原文的连续逐字子串。color标签、Wiki链接、HTML注释和模板须原样保留；按read提示用analysis_read读取，再选择完整原文。不能清洗后冒充原文或截掉必要条件。",
}


def _feedback_location(loc):
    if not isinstance(loc, list) or any(type(part) not in {str, int} for part in loc):
        return False
    if len(loc) == 1:
        return loc[0] in {"request_quotes", "question_indices", "evidence_ref", "offset", "theme_id"}
    if len(loc) == 3 and type(loc[1]) is int:
        return ((loc[0] == "request_quotes" and 0 <= loc[1] < 12
                 and loc[2] in {"quote", "question_index"})
                or (loc[0] == "observations" and 0 <= loc[1] < MAX_EVIDENCE
                    and loc[2] in {"quote", "evidence_ref"}))
    return (len(loc) == 4 and loc[0] == "analysis" and type(loc[1]) is int
            and 0 <= loc[1] < 8 and loc[2] == "evidence_refs"
            and type(loc[3]) is int and 0 <= loc[3] < 8)


def _repair_feedback(context, repair):
    """Publish schema locations and run-owned read paths, never exception text."""
    code = repair.get("code")
    if not isinstance(code, str) or code not in _REPAIR_INSTRUCTIONS:
        return {"reason": "analysis_boundary_rejected", "fields": [],
                "instruction": "证据边界校验未通过；保留原问、真实来源和逐字引用，不编造或自动修补。"}
    registry = context.analysis_evidence
    identity = {"release_id": context.release.id, "manifest_hash": context.release.manifest_hash,
                "question": context.original_question, "previous_run_id": context.previous_run_id}
    items = (registry.get("items", {}) if isinstance(registry, dict)
             and registry.get("identity") == identity else {})
    if not isinstance(items, dict):
        items = {}
    fields = []
    raw_fields = repair.get("fields", [])
    for field in raw_fields[:12] if isinstance(raw_fields, list) else []:
        if (not isinstance(field, dict)
                or set(field) - {"loc", "evidence_ref", "question_index", "read"}
                or not _feedback_location(field.get("loc"))):
            continue
        data = {"loc": copy.deepcopy(field["loc"])}
        index = field.get("question_index")
        if type(index) is int and 0 <= index <= 127 and field["loc"][0] in {"request_quotes", "question_indices"}:
            data["question_index"] = index
        ref = field.get("evidence_ref")
        if (isinstance(ref, str) and re.fullmatch(r"analysis_evidence_[1-9][0-9]?", ref)
                and int(ref.rsplit("_", 1)[1]) <= MAX_EVIDENCE
                and isinstance(items.get(ref), dict)):
            data["evidence_ref"] = ref
            item = items[ref]
            start, end = item.get("start"), item.get("end")
            if (field.get("read") is True and type(start) is int and type(end) is int
                    and 0 <= start < end):
                data["read"] = {"tool": "analysis_read", "arguments": {
                    "evidence_ref": ref, "offset": start, "length": min(2000, end - start)}}
        fields.append(data)
    instruction = _REPAIR_INSTRUCTIONS[code]
    if (code == "analysis_nonliteral_observation"
            and getattr(getattr(context, "settings", None), "agent_model_first", False)):
        instruction = ("所列quote与真实原文不符。删除quote、保留真实evidence_ref，由服务器组装原文；"
                       "需要连续短片段时读取来源后选择start/end。不要重新抄写或清洗Wiki，不能改写原文。")
    return {"reason": code, "fields": fields, "instruction": instruction}


def _leaves(value, pointer=""):
    if isinstance(value, str):
        if value.strip():
            yield pointer, value
    elif isinstance(value, dict):
        for key, child in value.items():
            token = str(key).replace("~", "~0").replace("/", "~1")
            yield from _leaves(child, pointer + "/" + token)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _leaves(child, pointer + "/" + str(index))


def _explanatory_field(selector, text):
    """Authorize real display prose, never arbitrary strings from raw objects."""
    if not isinstance(selector, str) or not selector.startswith("/") or not isinstance(text, str):
        return False
    parts = [part.replace("~1", "/").replace("~0", "~") for part in selector[1:].split("/")]
    # A list of explanatory strings keeps its owning field's source role.
    while parts and parts[-1].isdecimal():
        parts.pop()
    field = parts[-1] if parts else ""
    if field not in _EXPLANATORY_FIELDS and not field.endswith(_EXPLANATORY_SUFFIXES):
        return False
    visible = plain(text).strip()
    # Numeric configuration rendered as text is still not an explanation.
    # Original text such as “攻击力+20%” remains intact and source-replayable.
    return bool(visible) and any(character.isalpha() for character in visible)


def _slice(citation, start, end):
    value = citation["field_value"]
    if not isinstance(value, str) or not 0 <= start < end <= len(value):
        raise InvalidData("invalid literal analysis source span")
    base = citation.get("start", 0)
    return {**copy.deepcopy(citation), "start": base + start, "end": base + end,
            "field_value": value[start:end], "field_hash": services.content_digest(value[start:end]),
            "selector_semantics": "RFC6901_then_Unicode_codepoint_slice"}


def _context_span(text, start, end, *, full_end=None, **extra):
    """A reading aid uses the same canonical-string hash as source citations."""
    raw = text[start:end]
    return {"start": start, "end": end, "text": raw,
            "field_hash": services.content_digest(raw),
            "truncated": end < (end if full_end is None else full_end), **extra}


def _wiki_structure(text, offset):
    """Read literal Wiki delimiters without expanding templates, HTML or code.

    Positions always refer to the untouched source field. A preceding closed
    table cannot be mistaken for the table containing the requested position.
    The innermost containing table wins when source tables are nested.
    """
    protected = [m.span() for m in re.finditer(
        r"<!--[\s\S]*?(?:-->|$)"
        r"|<(?:nowiki|pre|source|syntaxhighlight|script|style)\b[^>]*?/\s*>"
        r"|<(nowiki|pre|source|syntaxhighlight|script|style)\b[^>]*>"
        r"[\s\S]*?(?:</\1\s*>|$)", text, re.I)]

    def ignored(position):
        return any(start <= position < end for start, end in protected)

    headings = []
    for match in re.finditer(r"(?m)^(={2,6})[^\r\n]*?\1[ \t]*\r?$", text):
        if match.start() > offset:
            break
        if ignored(match.start()):
            continue
        level = len(match[1])
        headings = [h for h in headings if h[0] < level]
        headings.append((level, match.start(), match.end()))

    stack, tables = [], []
    for match in re.finditer(r"(?m)^[ \t:]*(\{\||\|\})", text):
        start, end = match.span(1)
        if ignored(start):
            continue
        if match[1] == "{|":
            stack.append(start)
        elif stack:
            tables.append((stack.pop(), end))
    tables.extend((start, None) for start in stack)
    containing = [(start, end) for start, end in tables
                  if start <= offset and (end is None or offset < end)]
    table = max(containing, default=None, key=lambda span: span[0])
    # Keep room for the actual table opening/header before spending characters
    # on long section titles. These are syntax bounds, never game semantics.
    title_budget = 120 if table else MAX_STRUCTURE_CHARS
    per_title = title_budget // max(1, len(headings))
    title_spans = [_context_span(text, start, min(end, start + per_title),
                               full_end=end, level=level)
                   for level, start, end in headings]
    remaining = MAX_STRUCTURE_CHARS - sum(len(h["text"]) for h in title_spans)
    table_span = None
    if table:
        start, closed_end = table
        full_end = len(text) if closed_end is None else closed_end
        end = min(full_end, start + remaining)
        table_span = _context_span(text, start, end, full_end=full_end,
                                   closed_in_source=closed_end is not None)
        if closed_end is None:
            table_span["truncated"] = True
    return {"headings": title_spans, "table": table_span,
            "max_text_chars": MAX_STRUCTURE_CHARS,
            "truncated": any(h["truncated"] for h in title_spans)
                         or bool(table_span and table_span["truncated"])}


def _source_structure(context, record, citation, source_kind, *, offset=None, wiki_text=None):
    """Derive structure from verified records/raw bytes, never registry caches.

    Context is reading/review data. Citing any adjacent text still requires an
    explicit analysis_read and the unchanged final literal-source boundary.
    """
    wiki = None
    if source_kind == "wiki":
        if record["kind"] != rag.KIND:
            raise Conflict("analysis wiki source classification differs")
        if wiki_text is None:
            raw = context.store.read_verified(citation["snapshot_hash"])
            document = services.snapshot_document(raw, citation["source_media_type"])
            wiki_text = services.json_pointer(document, citation["pointer"])
        if (not isinstance(wiki_text, str)
                or wiki_text[citation["start"]:citation["end"]] != citation["field_value"]
                or services.content_digest(citation["field_value"]) != citation["field_hash"]):
            raise Conflict("analysis reading context differs from its raw field")
        wiki = _wiki_structure(wiki_text, citation["start"] if offset is None else offset)
    structure = {"record_kind": record["kind"], "selector": citation["pointer"],
                 "wiki": wiki, "reading_only": True}
    if source_kind == "client" and getattr(getattr(context, "settings", None), "agent_model_first", False):
        # Read surrounding fields from the same verified raw snapshot, not
        # model text or a local interpretation of the user's wording.
        document = services.snapshot_document(context.store.read_verified(citation["snapshot_hash"]),
                                               citation["source_media_type"])
        pointer = citation["pointer"].rsplit("/", 1)[0]
        parent = services.json_pointer(document, pointer)
        if isinstance(parent, dict):
            siblings = {key: value for key, value in parent.items()
                        if value is None or type(value) in {str, int, float, bool}}
            structure["original_object_fields"] = {
                key: value[:2000] if isinstance(value, str) else value
                for key, value in list(siblings.items())[:40]}
            structure["truncated_fields"] = [key for key, value in siblings.items()
                                             if isinstance(value, str) and len(value) > 2000]
            structure["omitted_field_count"] = max(0, len(siblings) - 40)
            structure["original_object_pointer"] = pointer
            structure["field_role_interpretation_owner"] = "model"
    return structure


def resolve_registered_source(answer, observation):
    """Read one complete registered context, including legacy saved answers.

    This is a lossless presentation resolver, never a source authorization
    check. New answers store one exact quote per run-owned ref; citation
    ``field_value`` is that same quote rather than a second serialized copy.
    Final source authorization still requires the original _replay boundary.
    A malformed shared table must not fall back to the chosen short quote or
    a legacy inline value, which could conceal a missing full context.
    """
    error = "analysis registered source context is missing or inconsistent"
    if not isinstance(answer, dict) or not isinstance(observation, dict):
        raise InvalidData(error)
    if "registered_sources" in answer or "registered_sources_format" in answer:
        sources = answer.get("registered_sources")
        if (answer.get("registered_sources_format") != REGISTERED_SOURCES_FORMAT
                or not isinstance(sources, dict)):
            raise InvalidData(error)
        ref = observation.get("evidence_ref")
        item = sources.get(ref) if isinstance(ref, str) else None
        if not isinstance(item, dict):
            raise InvalidData(error)
        quote, citation = item.get("quote"), item.get("citation")
    else:
        quote = observation.get("registered_source_quote")
        citation = observation.get("registered_source_citation")
    if (not isinstance(quote, str) or not quote or not isinstance(citation, dict)
            or "field_value" in citation and citation["field_value"] != quote
            or citation.get("field_hash") != services.content_digest(quote)
            or not isinstance(observation.get("quote"), str)
            or observation["quote"] not in quote):
        raise InvalidData(error)
    return {"quote": quote, "citation": {**copy.deepcopy(citation), "field_value": quote}}


def _register(registry, item):
    signature = copy.deepcopy({key: value for key, value in item.items() if key != "score"})
    signature["citation"].pop("id", None)
    old = next((ref for ref, value in registry.items()
                if services.same_json_value(value, signature)), None)
    if old is None:
        if len(registry) >= MAX_EVIDENCE:
            raise AnalysisBoundaryError("analysis_registry_limit", "analysis evidence registry reached its request limit")
        old = f"analysis_evidence_{len(registry) + 1}"
        registry[old] = signature
    return old


def _client_index(context, proof, args):
    """Scan authorized source fields once for all model-authored queries."""
    catalog = proof.catalog()
    allowed = {row["id"]: row for row in catalog
               if row["kind"] != "roguelike_wiki"
               and (args.theme_id is None or row["parent_native_id"] == args.theme_id
                    or row["kind"] == "roguelike_theme" and row["native_id"] == args.theme_id)}
    query = select(KnowledgeRecord.id, KnowledgeRecord.payload).where(
        KnowledgeRecord.dataset_id == proof.entry["dataset_id"],
        KnowledgeRecord.module == "roguelike", KnowledgeRecord.kind != "roguelike_wiki")
    if args.theme_id is not None:
        query = query.where((KnowledgeRecord.parent_native_id == args.theme_id)
                            | ((KnowledgeRecord.kind == "roguelike_theme")
                               & (KnowledgeRecord.native_id == args.theme_id)))
    rows = context.session.execute(query).all()
    docs = []
    for ident, data in rows:
        row = allowed.get(ident)
        if row is None:
            continue
        for selector, text in _leaves(data):
            if not _explanatory_field(selector, text):
                continue
            spans = [(0, len(text))] if len(text) <= 1200 else rag.passages(text, size=1200)
            for start, end in spans:
                terms = Counter(rag.tokens(plain(row["name"] + " " + text[start:end])))
                docs.append(({"source_kind": "client", "row": row, "selector": selector,
                              "start": start, "end": end}, terms, sum(terms.values())))
    return docs


def _wiki_index(context, proof, args):
    # Source membership, hashes and untouched content are checked by pages().
    docs = []
    if proof.entry.get("scope", {}).get("wiki_revisions"):
        for page in rag.pages(context.session, context.release, context.store, args.theme_id):
            for start, end in rag.passages(page["text"]):
                terms = Counter(rag.tokens(plain(page["text"][start:end])))
                docs.append(({"source_kind": "wiki", "page": page, "start": start,
                              "end": end}, terms, sum(terms.values())))
    return docs


def _candidate_key(item):
    if item["source_kind"] == "client":
        return ("client", item["row"]["id"], item["selector"], item["start"], item["end"])
    return ("wiki", item["page"]["record"]["id"], "", item["start"], item["end"])


def _rank_index(docs, query, limit):
    """BM25 ranks literal token matches; it does not decide semantic support."""
    frequencies = Counter(term for _, terms, _ in docs for term in terms)
    query_terms = set(rag.tokens(query))
    average = sum(doc[2] for doc in docs) / max(1, len(docs))
    scored = []
    for item, terms, length in docs:
        score = sum(log(1 + (len(docs) - frequencies[t] + .5) / (frequencies[t] + .5))
                    * terms[t] * 2.2 / (terms[t] + 1.2 * (.25 + .75 * length / max(1, average)))
                    for t in query_terms if terms[t])
        # These are source field families, never words that classify user prose.
        if (item["source_kind"] == "client" and item["selector"].rsplit("/", 1)[-1]
                in {"usage", "ruleDesc", "description", "functionDesc"}):
            score *= 1.3
        if score > 0:
            scored.append((score, item))
    def order(value):
        score, item = value
        return ((-score, item["row"]["native_id"], item["selector"], item["start"])
                if item["source_kind"] == "client"
                else (-score, item["page"]["pin"]["revision"], "", item["start"]))
    scored.sort(key=order)
    return [item for _, item in scored[:limit]], len(scored)


def _source_ranks(client, wiki, limit):
    # Independent indexes have different IDF scales. Retain the established
    # source-family rank fusion; no numeric/word match is filtered locally.
    ranks = [(1 / (30 + rank), item) for items in (client, wiki)
             for rank, item in enumerate(items, 1)]
    ranks.sort(key=lambda value: (-value[0],
        value[1]["row"]["name"] if value[1]["source_kind"] == "client"
        else value[1]["page"]["pin"]["title"], value[1]["start"]))
    return [item for _, item in ranks[:limit]]


def _query_ranks(ranked, limit):
    """Give each explicit query a candidate before filling repeated coverage.

    This is mechanical rank diversification, not an intent classifier. A
    shared source span is returned/registered once and can cover many queries.
    """
    selected, keys = [], set()
    memberships = {}
    for index, items in enumerate(ranked):
        for item in items:
            memberships.setdefault(_candidate_key(item), []).append(index)
    while len(selected) < limit:
        added = False
        for items in ranked:
            candidate = next((item for item in items
                              if _candidate_key(item) not in keys), None)
            if candidate is None:
                continue
            key = _candidate_key(candidate)
            keys.add(key)
            selected.append((candidate, memberships[key]))
            added = True
            if len(selected) == limit:
                break
        if not added:
            break
    return selected


def _materialize_candidate(proof, item):
    start, end = item["start"], item["end"]
    if item["source_kind"] == "client":
        row, selector = item["row"], item["selector"]
        record = proof.record(row["id"])
        field = services.json_pointer(record["data"], selector)
        ident = proof.cite(record, selector, field)
        original = next(c for c in proof.result["citations"] if c["id"] == ident)
        return {"source_kind": "client", "source_role": _CLIENT_TEXT_ROLE,
                "title": record["name"],
                "theme_id": row["parent_native_id"] or row["native_id"],
                "record_id": row["id"], "selector": selector,
                "start": start, "end": end, "quote": field[start:end],
                "citation": _slice(original, start, end),
                "complete_source_field": start == 0 and end == len(field)}
    page = item["page"]
    return {"source_kind": "wiki", "source_role": _WIKI_TEXT_ROLE,
            "title": page["pin"]["title"], "theme_id": page["pin"]["theme_id"],
            "record_id": page["record"]["id"], "start": start, "end": end,
            "quote": page["text"][start:end], "citation": rag.citation(page, start, end),
            "complete_source_field": False}


def _lexical_match(query, item):
    """Small, explicit token diagnostics; no match is a fact or alias link."""
    terms = set(rag.tokens(query))
    title = terms & set(rag.tokens(plain(item["title"])))
    quote = terms & set(rag.tokens(plain(item["quote"])))
    matched = title | quote
    return {"query_token_count": len(terms), "title_terms": sorted(title)[:8],
            "quote_terms": sorted(quote)[:8], "title_match_count": len(title),
            "quote_match_count": len(quote), "unmatched_token_count": len(terms - matched),
            "numeric_match_count": sum(term.isdecimal() for term in matched),
            "numeric_only_match": bool(matched) and all(term.isdecimal() for term in matched),
            "title_only_match": bool(title) and not quote,
            "term_lists_truncated": len(title) > 8 or len(quote) > 8}


def _coverage_report(queries, coverage, hits):
    report = copy.deepcopy(coverage)
    for row in report:
        index = row["query_index"]
        shown = [hit for hit in hits if any(match["query_index"] == index
                                           for match in hit["lexical_matches"])]
        terms = set(rag.tokens(queries[index]))
        matched = set().union(*(terms & set(rag.tokens(plain(hit["title"] + " " + hit["quote"])))
                                for hit in shown))
        missing = sorted(terms - matched)
        row.update(displayed_hit_count=len(shown), query_token_count=len(terms),
            matched_token_count=len(matched), uncovered_terms=missing[:24],
            uncovered_term_count=len(missing), uncovered_terms_truncated=len(missing) > 24)
    return report


def search(context, args):
    """Return finite, source-replayed candidates; selection belongs to the model."""
    args = _validated(AnalysisSearchArguments, args)
    registry = _registry(context)
    proof = _Answer(context.session, context.release, context.original_question, context.store)
    queries = [args.query, *args.subqueries]
    client_index = _client_index(context, proof, args)
    wiki_index = _wiki_index(context, proof, args)
    ranked, query_coverage = [], []
    for index, query in enumerate(queries):
        window = args.offset + args.limit + 1
        client, client_count = _rank_index(client_index, query, window)
        wiki, wiki_count = _rank_index(wiki_index, query, window)
        candidates = _source_ranks(client, wiki, window)
        ranked.append(candidates)
        query_coverage.append({"query_index": index, "query": query,
            "matching_source_fields_or_passages": client_count + wiki_count,
            "candidate_count_before_shared_limit": len(candidates)})
    # Build a private projection first: byte-omitted candidates never obtain
    # new model-usable references. Existing run references stay untouched.
    original_refs = set(registry)
    staged_registry = copy.deepcopy(registry)
    hits = []
    candidates = _query_ranks(ranked, args.offset + args.limit + 1)
    page = candidates[args.offset:args.offset + args.limit]
    for spec, query_indices in page:
        item = _materialize_candidate(proof, spec)
        old = _register(staged_registry, item)
        citation = copy.deepcopy(staged_registry[old]["citation"])
        structure = _source_structure(context, proof.record(item["record_id"]), citation,
                                      item["source_kind"])
        hits.append({"evidence_ref": old, "title": item["title"], "theme_id": item["theme_id"],
                     "source_role": item["source_role"],
                     "quote": item["quote"], "complete_source_field": item["complete_source_field"],
                     "adjacent_context_may_be_required": not item["complete_source_field"],
                     "citation": citation, "source_context": structure,
                     "lexical_matches": [{"query_index": index,
                         **_lexical_match(queries[index], item)} for index in query_indices]})
    max_bytes = getattr(getattr(context, "settings", None), "tool_result_max_bytes", 32768)
    candidate_count = len(hits)
    data = {"release_id": context.release.id,
            "manifest_hash": context.release.manifest_hash, "query": args.query,
            "theme_id": args.theme_id, "hits": hits,
            "offset": args.offset,
            "next_offset": args.offset + len(page) if len(candidates) > args.offset + len(page) else None,
            "method": "lexical_source_fields_and_published_wiki_BM25",
            "query_execution": "model_authored_queries_shared_source_scan_and_total_hit_limit",
            "match_semantics": "literal_tokens_only_not_semantic_relevance_or_mechanic_support; "
                "title_only_or_numeric_only_matches_do_not_identify_the_requested_object; "
                "uncovered_terms_describe_only_displayed_candidates_not_the_entire_library",
            "answer_permission": "literal_observations_only; analysis_requires_independent_review",
            "empty_result_means": "not_found_in_this_search_not_proof_of_nonexistence"}
    while True:
        # Never skip candidates omitted by the byte envelope, including when
        # the caller must reduce limit or narrow the query before advancing.
        data["next_offset"] = (args.offset + len(hits)
            if len(hits) < len(page) or len(candidates) > args.offset + len(page) else None)
        if data["next_offset"] is not None and data["next_offset"] > 128:
            data.update(next_offset=None, pagination_limit_reached=True,
                        continuation_guidance="refine_model_authored_query; not_corpus_exhausted")
        data["query_coverage"] = _coverage_report(queries, query_coverage, hits)
        data["retrieval_display_budget"] = {
            "candidate_hit_count": candidate_count, "displayed_hit_count": len(hits),
            "omitted_hit_count": candidate_count - len(hits), "max_envelope_bytes": max_bytes,
            "omission_semantics": "whole_ranked_candidates_omitted_for_bytes_not_zero_matches"}
        if candidate_count and not hits:
            data["empty_result_means"] = "matching_candidates_omitted_for_bytes_not_zero_matches"
        result = {"ok": True, "data": data}
        if len(services.canonical_json(result).encode("utf-8")) <= max_bytes or not hits:
            break
        hits.pop()
    returned_refs = {hit["evidence_ref"] for hit in hits}
    registry.update({ref: item for ref, item in staged_registry.items()
                     if ref not in original_refs and ref in returned_refs})
    return result


def _replay(context, item, proof, wiki):
    if item["source_kind"] == "client":
        record = proof.record(item["record_id"])
        actual_theme = record["parent_native_id"] or record["native_id"]
        field = services.json_pointer(record["data"], item["selector"])
        if item.get("source_role") != _CLIENT_TEXT_ROLE or not _explanatory_field(item["selector"], field):
            raise AnalysisBoundaryError("analysis_non_explanatory_source",
                                        "该证据是结构标识、分类或数值配置，不是可用于解释问题的来源文案；"
                                        "请检索效果、条件或难度说明，缺少说明时明确无法核实。")
        ident = proof.cite(record, item["selector"], field)
        original = next(c for c in proof.result["citations"] if c["id"] == ident)
        citation = _slice(original, item["start"], item["end"])
    elif item["source_kind"] == "wiki":
        if item.get("source_role") != _WIKI_TEXT_ROLE:
            raise AnalysisBoundaryError("analysis_non_explanatory_source",
                                        "该证据未注册为本运行已核验的公开资料原文，请重新检索。")
        if not wiki:
            wiki.update({p["record"]["id"]: p for p in rag.pages(
                context.session, context.release, context.store)})
        page = wiki.get(item["record_id"])
        if page is None:
            raise Conflict("analysis wiki evidence is outside the current release")
        record = page["record"]
        actual_theme = page["pin"]["theme_id"]
        citation = rag.citation(page, item["start"], item["end"])
    else:
        raise Conflict("analysis evidence has an unknown source family")
    citation.pop("id", None)
    if (not services.same_json_value(citation, item["citation"])
            or citation["field_value"] != item["quote"]
            or actual_theme != item["theme_id"] or record["name"] != item["title"]):
        raise Conflict("analysis registered evidence differs from replayed source")
    return citation, record


def read(context, args):
    """Read a bounded raw span from a source already replayed in this run.

    Search previews are not the whole source. The model chooses a position,
    while the private registry supplies every source identity and authority.
    A read never upgrades a source role or accepts a caller-authored locator.
    """
    args = _validated(AnalysisReadArguments, args)
    registry = _registry(context)
    item = registry.get(args.evidence_ref)
    if not isinstance(item, dict):
        raise AnalysisBoundaryError("analysis_unknown_evidence_reference",
                                    "只能读取本运行实际返回的analysis引用，请先检索；不能编造来源ID。",
                                    fields=[{"loc": ["evidence_ref"]}])
    proof = _Answer(context.session, context.release, context.original_question, context.store)
    proof.catalog()
    wiki = {}
    _, record = _replay(context, item, proof, wiki)
    if item["source_kind"] == "client":
        field = services.json_pointer(record["data"], item["selector"])
    else:
        field = wiki[item["record_id"]]["text"]
    start = item["start"] if args.offset is None else args.offset
    if not 0 <= start < len(field):
        raise AnalysisBoundaryError("analysis_read_offset_out_of_range",
                                    "offset必须位于已核验同一原始字段内；请按返回的字符长度和分页位置读取。",
                                    fields=[{"loc": ["offset"], "evidence_ref": args.evidence_ref,
                                             "read": True}])
    end = min(start + args.length, len(field))
    if item["source_kind"] == "client":
        ident = proof.cite(record, item["selector"], field)
        original = next(c for c in proof.result["citations"] if c["id"] == ident)
        citation = _slice(original, start, end)
    else:
        citation = rag.citation(wiki[item["record_id"]], start, end)
    citation.pop("id", None)
    span = {**copy.deepcopy(item), "start": start, "end": end,
            "quote": field[start:end], "citation": citation,
            "complete_source_field": start == 0 and end == len(field)}
    ref = _register(registry, span)
    structure = _source_structure(context, record, citation, item["source_kind"],
                                  wiki_text=field if item["source_kind"] == "wiki" else None)
    return {"ok": True, "data": {
        "release_id": context.release.id, "manifest_hash": context.release.manifest_hash,
        "source_evidence_ref": args.evidence_ref, "evidence_ref": ref,
        "title": item["title"], "theme_id": item["theme_id"],
        "source_role": item["source_role"], "quote": span["quote"],
        "start": start, "end": end, "field_length": len(field),
        "previous_offset": max(0, start - args.length) if start else None,
        "next_offset": end if end < len(field) else None,
        "complete_source_field": span["complete_source_field"],
        "adjacent_context_may_be_required": not span["complete_source_field"],
        "citation": copy.deepcopy(citation),
        "source_context": structure,
        "answer_permission": "literal_observations_only; analysis_requires_independent_review",
    }}


def answer(context, args):
    """Stage an evidence-bounded answer, explicitly separating model inferences."""
    model_first = getattr(getattr(context, "settings", None), "agent_model_first", False)
    if model_first and isinstance(args, AnalysisAnswerArguments):
        args = args.model_dump()
    args = _validated(ModelFirstAnalysisAnswerArguments if model_first else AnalysisAnswerArguments, args)
    registry = _registry(context)
    assembled_refs = []
    if model_first:
        selected = {item.evidence_ref for item in args.observations}
        # The model has already selected these refs in its paragraphs. Assemble
        # originals, never infer an entity, choose new evidence, or fix quotes.
        for inference in args.analysis:
            for ref in inference.evidence_refs:
                if ref not in selected:
                    assembled_refs.append(ref)
                    selected.add(ref)
        args = ModelFirstAnalysisAnswerArguments.model_validate({
            **args.model_dump(), "observations": [
                *(item.model_dump() for item in args.observations),
                *({"evidence_ref": ref} for ref in assembled_refs)]})
    questions = [*previous_questions(context.previous_answer), context.original_question]
    request_errors = []
    for index, item in enumerate(args.request_quotes):
        if item.question_index >= len(questions):
            request_errors.append({"loc": ["request_quotes", index, "question_index"],
                                   "question_index": item.question_index})
        elif item.quote not in questions[item.question_index]:
            request_errors.append({"loc": ["request_quotes", index, "quote"],
                                   "question_index": item.question_index})
    if request_errors:
        raise AnalysisBoundaryError("analysis_nonliteral_request_quote",
                                    "analysis request quote is not in the authentic user question chain",
                                    fields=request_errors)
    if not any(item.question_index == len(questions) - 1 for item in args.request_quotes):
        raise AnalysisBoundaryError("analysis_missing_current_question_quote",
                                    "analysis request must include a literal quote of the current user question",
                                    fields=[{"loc": ["request_quotes"], "question_index": len(questions) - 1}])
    observation_refs = {item.evidence_ref for item in args.observations}
    missing = [{"loc": ["analysis", index, "evidence_refs", ref_index],
                "evidence_ref": ref, "read": True}
               for index, item in enumerate(args.analysis)
               for ref_index, ref in enumerate(item.evidence_refs) if ref not in observation_refs]
    if missing:
        raise AnalysisBoundaryError("analysis_missing_observation",
                                    "analysis inference needs an explicit literal observation for every source",
                                    fields=missing)
    proof = _Answer(context.session, context.release, context.original_question, context.store)
    proof.catalog()
    replayed, wiki = {}, {}
    result = proof.result
    observations, citations, records, registered_sources = [], [], {}, {}
    nonliteral = []
    for index, observation in enumerate(args.observations):
        ref = observation.evidence_ref
        item = registry.get(ref)
        if not isinstance(item, dict):
            raise AnalysisBoundaryError("analysis_unknown_evidence_reference",
                                        "analysis evidence reference was not returned in this request",
                                        fields=[{"loc": ["observations", index, "evidence_ref"]}])
        if args.theme_id is not None and item["theme_id"] != args.theme_id:
            raise AnalysisBoundaryError("analysis_evidence_theme_mismatch",
                                        "analysis evidence does not belong to the selected theme",
                                        fields=[{"loc": ["observations", index, "evidence_ref"]}])
        if ref not in replayed:
            replayed[ref] = _replay(context, item, proof, wiki)
        quote = observation.quote
        position = None
        if observation.start is not None or observation.end is not None:
            if (observation.start is None or observation.end is None or quote is not None
                    or not 0 <= observation.start < observation.end <= len(item["quote"])):
                raise AnalysisBoundaryError("analysis_nonliteral_observation", "invalid source span",
                    fields=[{"loc": ["observations", index, "quote"], "evidence_ref": ref, "read": True}])
            position = observation.start
            quote = item["quote"][position:observation.end]
        elif quote is None:
            quote = item["quote"]
            position = 0
        if quote not in item["quote"]:
            nonliteral.append({"loc": ["observations", index, "quote"],
                               "evidence_ref": ref, "read": True})
            continue
        original, record = replayed[ref]
        if position is None:
            position = item["quote"].index(quote)
        citation = _slice(original, position, position + len(quote))
        citation["id"] = f"c{len(citations) + 1}"
        structure = _source_structure(context, record, citation, item["source_kind"],
            wiki_text=wiki[item["record_id"]]["text"] if item["source_kind"] == "wiki" else None)
        if ref not in registered_sources:
            registered_citation = copy.deepcopy(original)
            # _replay has already proved these are the same untouched bytes.
            # Keep the full context once, without duplicating it inside its
            # citation or every observation that selects a shorter quote.
            registered_citation.pop("field_value")
            registered_sources[ref] = {"quote": item["quote"], "citation": registered_citation}
        citations.append(citation)
        records[record["id"]] = {key: record[key] for key in (
            "id", "dataset_id", "module", "kind", "native_id", "name", "parent_native_id", "evidence_ids",
        ) if key in record}
        observations.append({"evidence_ref": ref, "quote": quote,
                             "source_role": item["source_role"],
                             "title": item["title"], "theme_id": item["theme_id"],
                             "complete_source_field": item["complete_source_field"],
                             "adjacent_context_may_be_required": not item["complete_source_field"],
                             "source_context": structure,
                             "citation_ids": [citation["id"]]})
    if nonliteral:
        raise AnalysisBoundaryError("analysis_nonliteral_observation",
                                    "analysis observation is not a literal source substring",
                                    fields=nonliteral)
    result["citations"] = citations
    result["records"] = list(records.values())
    result["claims"] = [{"id": f"f{i}", "record_id": citations[i - 1]["record_id"],
                         "label": item["title"] + "资料原文", "value": item["quote"],
                         "citation_ids": item["citation_ids"],
                         "verification_status": "literal_replayed_source_observation"}
                        for i, item in enumerate(observations, 1)]
    result["theme_id"] = args.theme_id
    result["version"]["theme_id"] = args.theme_id
    result["analysis_request"] = {"summary": args.request_summary,
                                  "quotes": [q.model_dump() for q in args.request_quotes],
                                  "original_user_questions": questions,
                                  "interpretation_origin": "model_query_proposal"}
    if args.review_response:
        result["model_revision_response"] = args.review_response
    if args.follow_up_question:
        result["follow_up_question"] = args.follow_up_question
    result["source_observations"] = observations
    if assembled_refs:
        result["source_assembly"] = {
            "origin": "model_selected_inference_refs_replayed_by_server",
            "assembled_refs": assembled_refs, "new_evidence_selected_by_server": False}
    result["registered_sources_format"] = REGISTERED_SOURCES_FORMAT
    result["registered_sources"] = registered_sources
    if context.analysis_evidence.get("task_plan"):
        result["model_task_plan"] = copy.deepcopy(context.analysis_evidence["task_plan"])
    if context.analysis_evidence.get("organized_evidence"):
        result["model_evidence_assessment"] = [
            {key: value for key, value in group.items()
             if key in {"question", "model_interpretation", "missing_evidence", "interpretation_verified_by_server"}}
            for group in context.analysis_evidence["organized_evidence"]["groups"]]
    result["model_analysis"] = [{**item.model_dump(),
                                  "verification_status": "model_inference_not_runtime_verified",
                                  "citation_ids": list(dict.fromkeys(
                                      cid for ob in observations if ob["evidence_ref"] in item.evidence_refs
                                      for cid in ob["citation_ids"]))}
                                 for item in args.analysis]
    result["dialogue"] = {"intent": "evidence_analysis", "theme_id": args.theme_id,
                           "entity_name": None, "entity_native_id": None,
                           "context_used": context.previous_answer is not None,
                           "interpretation_origin": "model_query_proposal",
                           "user_reported_conditions": [], "pending_user_reported_conditions": [],
                           "unresolved_text": "", "analysis_request": result["analysis_request"]}
    result["source_authority"] = {"literal_observations_source_verified": bool(observations),
                                   "model_inferences_source_verified": False,
                                   "runtime_mechanics_verified": False,
                                   "requires_independent_semantic_review": True}
    result["limitations"] = [
        "资料原文与模型分析分开列出；分析是基于所引资料的推断，未完成实战触发验证。",
        "检索未找到不证明游戏中不存在，固定来源也不保证当前线上状态。", *args.limitations]
    lines = []
    model_first = getattr(getattr(context, "settings", None), "agent_model_first", False)
    if observations and not model_first:
        lines.append("资料依据：")
        lines.extend(f"- {item['title']}：{item['quote']} [{item['citation_ids'][0]}]"
                     for item in observations)
    if args.analysis:
        lines.append("基于这些资料的分析（推断，未实战验证）：")
        lines.extend("- " + item["text"] + " " + " ".join(f"[{cid}]" for cid in item["citation_ids"])
                     for item in result["model_analysis"])
    if args.limitations:
        lines.append("尚未能核实：")
        lines.extend("- " + item for item in args.limitations)
    if not observations:
        lines.insert(0, "这次检索没有取得能支持该问题的已核验资料，暂时无法给出可靠结论。")
    elif not args.analysis:
        lines.append("已找到的资料可在来源中查看；当前尚未形成能回答完整问题的可靠分析。" if model_first
                     else "这些资料只支持上述原文；当前尚未形成能回答完整问题的可靠分析。")
    if args.follow_up_question:
        lines.append(args.follow_up_question)
    result["answer"] = "\n".join(lines)
    result["status"] = "answered" if observations and args.analysis else "needs_verification"
    if args.follow_up_question and not args.analysis:
        result["status"] = "needs_clarification"
    result["tool_trace"].append({"tool": "evidence_analysis", "release_id": context.release.id,
                                  "source_replayed": True,
                                  "model_inferences_authorized_as_facts": False,
                                  "requires_independent_semantic_review": True})
    attach_state(result, context.original_question, context.previous_answer, context.previous_run_id)
    return {"ok": True, "data": result}
