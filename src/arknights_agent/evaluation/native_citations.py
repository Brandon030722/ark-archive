"""Independent native JSON protocol; no model value is parsed a second time.

Only model-declared inline citations are flattened to prediction positions.
Facts, conditions, pointers, quoted values, and candidate names are never read
from a source packet or expectation to complete or repair an answer. The
unchanged original prediction validator and evidence scoring remain final.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue

from arknights_agent.errors import InvalidData
from arknights_agent.evaluation.baselines import (
    PROMPT_VERSION as BASE_PROMPT_VERSION,
)
from arknights_agent.evaluation.baselines import (
    Combination,
    StrictModel,
    _empty,
    _instructions,
    _json,
    _strict_loads,
    _validate_prediction,
    build_public_input,
)

PROMPT_VERSION = BASE_PROMPT_VERSION + "+deepseek-native-json-inline-citations-v2"


class NativeCitation(StrictModel):
    source_key: str = Field(min_length=1, max_length=160)
    source_revision: str = Field(min_length=1, max_length=160)
    source_url: str = Field(min_length=1, max_length=2048)
    snapshot_hash: str = Field(pattern="^[0-9a-f]{64}$")
    pointer: str = Field(max_length=2048)
    # model_fields_set distinguishes an omitted value from an explicit null.
    field_value: JsonValue = None
    field_hash: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")


class NativeFact(StrictModel):
    key: str = Field(min_length=1, max_length=240)
    value: JsonValue
    citations: list[NativeCitation] = Field(max_length=100)


class NativeModelOutput(StrictModel):
    status: Literal[
        "answered", "computed", "needs_clarification", "unsupported",
        "needs_verification", "out_of_scope", "invalid_input", "pending_manual",
    ]
    conditions: dict[str, JsonValue]
    facts: list[NativeFact] = Field(max_length=200)
    combinations: list[Combination] = Field(max_length=25)
    citations: list[NativeCitation] = Field(max_length=300)
    answer: str = Field(max_length=100_000)


def response_schema():
    """Forbid model extras while preserving arbitrary native JSON value keys."""
    schema = NativeModelOutput.model_json_schema()
    # Pydantic's recursive JsonValue validator can emit an unconstrained {}.
    # Spell out the native JSON shape for the model-facing schema instead.
    schema["$defs"]["JsonValue"] = {"anyOf": [
        {"type": "null"}, {"type": "boolean"}, {"type": "integer"},
        {"type": "number"}, {"type": "string"},
        {"type": "array", "items": {"$ref": "#/$defs/JsonValue"}},
        {"type": "object", "additionalProperties": {"$ref": "#/$defs/JsonValue"}},
    ]}

    def visit(node):
        if isinstance(node, dict):
            node.pop("default", None)
            # JsonValue dictionaries and conditions are data maps. Making them
            # additionalProperties=False would silently forbid valid values.
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(schema)
    return schema


def _messages(public_input, arm):
    instructions = _instructions(arm)
    instructions = instructions.replace(
        "value_json是合法JSON值字符串；", "value直接是原生JSON值；"
    ).replace(
        "条件conditions_json是合法JSON对象字符串；不要把数字写成字符串。",
        "条件conditions直接是JSON对象；不要把数字写成字符串。",
    ).replace(
        "事实citation_indices是citations数组的0基索引。citations.field_value_json可为合法JSON值"
        "字符串或null，field_hash未知时为null；answer用于中文解释，不能替代结构化事实。",
        "每个事实直接声明citations数组，内含完整引用对象，不生成数字引用索引。"
        "顶层citations用于公招或总体证据，不会自动关联任何事实。"
        "引用field_value直接是原生JSON值；未声明时省略此键，明确声明null时写null。"
        "field_hash未知时省略或为null；answer用于中文解释，不能替代结构化事实。",
    )
    instructions += (
        "技能rank从1开始，原始levels数组从0开始，因此rank对应levels[rank-1]；"
        "rank8读取levels[7]中的所有技能数值；技能编号skill_index也从1开始。"
        "不要把rank本身当作levels索引。"
        "public请求中的题面条件不得猜测、默认或改写；无法确认的条件应请求澄清。"
        "公招候选池取指定版本公开recruitDetail名单与character表的交集；"
        "不能借用其他版本或名单之外的干员。540分钟时排除1星和2星；"
        "6星仅在组合含高级资深干员标签时保留。仍需列全题面标签的全部非空1..3标签组合。"
        "天赋逐个原始家族独立处理：只在满足题面phase、level和potential条件的分支中"
        "选择最高已解锁分支，不能跨家族比较后丢失另一家族或遗漏潜能分支。"
        "所有值使用原生JSON表示，不再嵌套JSON文本；字符串保持普通字符串。"
        "引用pointer必须是实际声明的原文路径，不能用片段序号或事实序号替代。"
        "引用field_value是原文字符串时，富文本标签、换行和标点均按原文保留，"
        "不能先清洗文本再把清洗结果当作原始引用值。"
        "facts只能含公开fact_keys要求的键；其他解释放在answer中。"
        "只可使用schema声明的字段，不得添加facts_note、reason、comment或annotation。"
    )
    example = {
        "status": "answered",
        "conditions": {"operators": ["合成干员"], "skill_index": 1, "rank": 7},
        "facts": [{
            "key": "合成干员::技力消耗", "value": 7,
            "citations": [{
                "source_key": "synthetic-source", "source_revision": "synthetic-revision",
                "source_url": "https://example.invalid/synthetic.json",
                "snapshot_hash": "0" * 64, "pointer": "/synthetic/levels/6/spData/spCost",
                "field_value": 7,
            }],
        }],
        "combinations": [], "citations": [], "answer": "合成例子声明消耗7。",
    }
    return [
        {
            "role": "system",
            "content": instructions
            + "\n只返回严格JSON，schema中required字段必须出现，不得有额外字段："
            + _json(response_schema())
            + "\n以下独立合成格式示例的姓名、数值和路径均为虚构，"
            "不得作为当前问题的答案、条件或引用："
            + _json(example),
        },
        {"role": "user", "content": _json(public_input)},
    ]


def build_prompt(case, arm):
    """Use the unchanged public whitelist and complete original source packet."""
    return _messages(build_public_input(case, arm), arm)


def _citation_key(citation):
    # Canonical JSON distinguishes bool/int, int/float and omitted/null quotes.
    # An unknown hash (omitted or null) has the original no-hash semantics.
    result = dict(citation)
    if result.get("field_hash") is None:
        result.pop("field_hash", None)
    return _json(result)


def to_prediction(content, case, arm, trace, latency_ms):
    """Flatten declarations only, without nested JSON parsing or fact repair."""
    output = NativeModelOutput.model_validate(_strict_loads(content)).model_dump(
        exclude_unset=True
    )
    if len(_json(output["conditions"])) > 20_000:
        raise InvalidData("native conditions exceed the original 20000-character limit")
    citations, positions = [], {}

    def append(citation):
        citation = dict(citation)
        if citation.get("field_hash") is None:
            citation.pop("field_hash", None)
        key = _citation_key(citation)
        if key not in positions:
            if len(citations) >= 300:
                raise InvalidData("native model citations exceed the original 300-citation limit")
            positions[key] = len(citations)
            citations.append(citation)
        return positions[key]

    for citation in output.pop("citations"):
        append(citation)
    facts = []
    for fact in output.pop("facts"):
        if len(_json(fact["value"])) > 100_000:
            raise InvalidData("native fact value exceeds the original 100000-character limit")
        indices = []
        for citation in fact["citations"]:
            position = append(citation)
            if position not in indices:
                indices.append(position)
        facts.append({"key": fact["key"], "value": fact["value"], "citation_indices": indices})
    row = {
        **_empty(case, arm, output["status"], "completed"), **output,
        "facts": facts, "citations": citations,
        "trace": {**trace, "native_citations_version": PROMPT_VERSION},
        "latency_ms": latency_ms,
    }
    if row["status"] in {"answered", "computed"} and not facts and not row["combinations"]:
        row["status"] = "pending_manual"
        row["citations"] = []
        row["trace"]["reason"] = "text_only_requires_manual_normalization"
    return _validate_prediction(row, case)
