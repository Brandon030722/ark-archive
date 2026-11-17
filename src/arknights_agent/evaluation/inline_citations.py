"""Independent full JSON wire with citations declared inside each model fact.

Only representation changes here: model-declared citation objects are placed in
one global array and their array positions are generated locally. Citation
identity, pointer, quoted value, fact value and conditions are never completed
from a source packet, an older response, an Agent answer, or an expectation.
The unchanged original decoder and prediction validator remain authoritative.
"""

from __future__ import annotations

from pydantic import Field

from arknights_agent.errors import InvalidData
from arknights_agent.evaluation.baselines import (
    PROMPT_VERSION as BASE_PROMPT_VERSION,
)
from arknights_agent.evaluation.baselines import (
    ModelOutput,
    StrictModel,
    WireCitation,
    _decode_output,
    _instructions,
    _json,
    _strict_loads,
    build_public_input,
)

PROMPT_VERSION = BASE_PROMPT_VERSION + "+deepseek-json-inline-citations-v1"


class InlineWireFact(StrictModel):
    key: str = Field(min_length=1, max_length=240)
    value_json: str = Field(max_length=100_000)
    citations: list[WireCitation] = Field(max_length=100)


class InlineModelOutput(ModelOutput):
    facts: list[InlineWireFact] = Field(max_length=200)


def response_schema():
    """Full original schema with inline citations; all fields remain required."""
    schema = InlineModelOutput.model_json_schema()

    def visit(node):
        if isinstance(node, dict):
            node.pop("default", None)
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(schema)
    return schema


def _messages(public_input, arm):
    instructions = _instructions(arm).replace(
        "事实citation_indices是citations数组的0基索引。",
        "每个事实直接提供citations数组，内含完整引用对象，不生成数字引用索引。"
        "顶层citations用于公招或总体证据，不会自动关联任何事实。"
        "引用对象只能由模型根据原文明确给出，不能用输入片段编号代替pointer。",
    )
    example = {
        "status": "answered",
        "conditions_json": '{"operators":["合成干员"],"skill_index":1,"rank":7}',
        "facts": [{
            "key": "合成干员::技力消耗", "value_json": "7",
            "citations": [{
                "source_key": "synthetic-source", "source_revision": "synthetic-revision",
                "source_url": "https://example.invalid/synthetic.json",
                "snapshot_hash": "0" * 64, "pointer": "/synthetic/levels/6/spData/spCost",
                "field_value_json": "7", "field_hash": None,
            }],
        }],
        "combinations": [], "citations": [], "answer": "合成例子声明消耗7。",
    }
    return [
        {
            "role": "system",
            "content": instructions
            + "\n只返回严格JSON，下列schema全部字段必须出现，不能有额外字段："
            + _json(response_schema())
            + "\n以下是独立合成数据的格式示例，姓名、值和pointer均为示例中的模型声明，"
            "不能作为当前题答案或引用："
            + _json(example),
        },
        {"role": "user", "content": _json(public_input)},
    ]


def build_prompt(case, arm):
    """Use the unchanged public whitelist and complete original source packet."""
    return _messages(build_public_input(case, arm), arm)


def _citation_key(citation):
    """Typed JSON identity; no equality coercion and no field-value guessing.

    ``field_value_json=None`` means no quoted value was declared. The JSON
    string ``"null"`` explicitly declares a null value and keeps that field.
    Lexical whitespace/key ordering may deduplicate; JSON value types do not.
    """
    result = {
        key: citation[key]
        for key in ("source_key", "source_revision", "source_url", "snapshot_hash", "pointer")
    }
    if citation["field_value_json"] is not None:
        result["field_value"] = _strict_loads(citation["field_value_json"])
    if citation["field_hash"] is not None:
        result["field_hash"] = citation["field_hash"]
    return _json(result)


def to_prediction(content, case, arm, trace, latency_ms):
    """Flatten only model-supplied inline citations, then use the old decoder."""
    output = InlineModelOutput.model_validate(_strict_loads(content)).model_dump()
    citations, positions = [], {}

    def append(citation):
        key = _citation_key(citation)
        if key not in positions:
            if len(citations) >= 300:
                raise InvalidData("inline model citations exceed the original 300-citation limit")
            positions[key] = len(citations)
            citations.append(citation)
        return positions[key]

    # Global evidence remains independent. Its presence never adds a fact link.
    for citation in output["citations"]:
        append(citation)
    facts = []
    for fact in output["facts"]:
        indices = []
        for citation in fact["citations"]:
            position = append(citation)
            if position not in indices:
                indices.append(position)
        facts.append({
            "key": fact["key"], "value_json": fact["value_json"],
            "citation_indices": indices,
        })
    output["facts"], output["citations"] = facts, citations
    return _decode_output(
        {
            "status": "completed",
            "output": [{"type": "message", "content": [
                {"type": "output_text", "text": _json(output)},
            ]}],
        },
        case, arm, {**trace, "inline_citations_version": PROMPT_VERSION}, latency_ms,
    )
