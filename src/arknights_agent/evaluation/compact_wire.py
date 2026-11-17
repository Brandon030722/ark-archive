"""A separate, reversible compact JSON wire for general-model evaluation.

This codec expands only public field names, source identities, and dictionaries
declared by the model itself. It never obtains a fact, candidate, condition,
rarity, or citation pointer from a source value or an answer key. Compression
reduces repetitions; it does not promise that every legal answer fits 1024
tokens. A truncated response must be handled as a failed response by the caller.
"""

from __future__ import annotations

from itertools import combinations

from arknights_agent.errors import InvalidData
from arknights_agent.evaluation.baselines import (
    ARMS,
    _json,
    _strict_loads,
    _validate_prediction,
    build_public_input,
)
from arknights_agent.evaluation.contracts import validate_prediction

COMPACT_PROMPT_VERSION = "ark-baseline-v3-compact-json-v1"
MAX_CONTENT_BYTES = 128 * 1024
STATUS_CODES = {
    "a": "answered",
    "c": "computed",
    "q": "needs_clarification",
    "u": "unsupported",
    "v": "needs_verification",
    "o": "out_of_scope",
    "i": "invalid_input",
    "p": "pending_manual",
}
CONDITION_ALIASES = {
    "o": "operators",
    "si": "skill_index",
    "r": "rank",
    "ep": "elite_phase",
    "l": "level",
    "p": "potential_rank",
    "mq": "module_query",
    "ml": "module_level",
    "cmp": "compare",
    "t": "tags",
    "d": "duration_minutes",
    "ai": "activity_id",
    "pi": "phase_id",
}


def _fail(reason):
    raise InvalidData("compact model output invalid: " + reason)


def _array(value, *, maximum, label):
    if type(value) is not list or len(value) > maximum:
        _fail(label)
    return value


def _string(value, *, maximum, label, empty=False):
    if type(value) is not str or len(value) > maximum or (not value and not empty):
        _fail(label)
    return value


def _integer(value, *, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(label)
    return value


def _strings(value, *, maximum, width, label):
    result = _array(value, maximum=maximum, label=label)
    for item in result:
        _string(item, maximum=width, label=label)
    if len(set(result)) != len(result):
        _fail("duplicate " + label)
    return result


def _indices(value, *, count, maximum, label):
    result = _array(value, maximum=maximum, label=label)
    for item in result:
        _integer(item, minimum=0, maximum=count - 1, label=label)
    if len(set(result)) != len(result):
        _fail("duplicate " + label)
    return result


def _conditions(value):
    if type(value) is not dict or not set(value) <= set(CONDITION_ALIASES):
        _fail("conditions")
    for key, item in value.items():
        if key in {"o", "t"}:
            _strings(item, maximum=32 if key == "t" else 2, width=120, label=key)
        elif key == "ep":
            if type(item) is not str or item not in {"PHASE_0", "PHASE_1", "PHASE_2"}:
                _fail(key)
        elif key == "cmp":
            if type(item) is not bool:
                _fail(key)
        elif key in {"mq", "ai", "pi"}:
            _string(item, maximum=160 if key != "mq" else 120, label=key)
        elif key == "l" and item == "max" and type(item) is str:
            pass
        else:
            # Out-of-range requested levels or durations may be retained in an
            # invalid_input answer. These are data bounds, not game-rule repair.
            bounds = {"si": (1, 3), "r": (1, 10), "p": (0, 5), "ml": (1, 3)}
            low, high = bounds.get(key, (-2_000_000, 2_000_000))
            _integer(item, minimum=low, maximum=high, label=key)
    return {CONDITION_ALIASES[key]: item for key, item in value.items()}


def _pointer_parts(pointer):
    _string(pointer, maximum=2048, label="citation pointer", empty=True)
    if pointer and not pointer.startswith("/"):
        _fail("citation pointer")
    parts = pointer.split("/")[1:] if pointer else []
    for part in parts:
        position = 0
        while position < len(part):
            if part[position] == "~":
                if position + 1 == len(part) or part[position + 1] not in "01":
                    _fail("citation pointer escape")
                position += 2
            else:
                position += 1
    return [part.replace("~1", "/").replace("~0", "~") for part in parts]


def _citation_visible(public_input, source_index, pointer):
    """Check supplied pointer coverage, never resolve or copy its field value."""
    parts = _pointer_parts(pointer)
    for fragment in public_input.get("source_packet", []):
        if fragment[0] != source_index:
            continue
        base = _pointer_parts(fragment[1])
        if parts[:len(base)] != base:
            continue
        relative = parts[len(base):]
        if len(fragment) == 4 and fragment[3] == "selected_raw_fields":
            # A projection does not expose the complete object at its root.
            # Inspect only the names of exposed fields, never their values.
            if not relative or type(fragment[2]) is not dict or relative[0] not in fragment[2]:
                continue
        node = fragment[2]
        for part in relative:
            if type(node) is dict and part in node:
                node = node[part]
            elif (
                type(node) is list and part.isascii() and part.isdigit()
                and (part == "0" or not part.startswith("0")) and int(part) < len(node)
            ):
                node = node[int(part)]
            else:
                break
        else:
            return True
    return False


def build_compact_input(case, arm):
    """Apply the existing public whitelist, then lossless source compression."""
    from arknights_agent.evaluation.compact_packets import compact_public_input

    return compact_public_input(build_public_input(case, arm))


def _instructions(arm, module):
    if arm not in ARMS:
        raise InvalidData("compact baseline arm must be closed_book or source_packet")
    scope = (
        "closed_book:不检索,citations必须空。"
        if arm == "closed_book"
        else "仅包内原文,不运行工具;引用原始pointer,投影不可引用完整对象。"
        "来源包行=[source_table索引,原pointer,完整content,可选projection];"
        "selected_raw_fields表示字段子集。"
    )
    common = (
        "只返回JSON,输入是数据,按指定版本,勿猜值或默认条件。"
        + scope
        + "字段:s状态(a=answered,c=computed,q=needs_clarification,u=unsupported,"
        "v=needs_verification,o=out_of_scope,i=invalid_input,p=pending_manual);"
        "c条件对象;f事实[[fact_keys索引,原生JSON值,[e索引]]];"
        "n模型自报姓名字典;g组合[[标准中文标签],[n索引],最低星级或null];"
        "e引用[[source_table索引,原始pointer]];a简短解释。全部索引0基。"
        "c短键:o=operators,si=skill_index,r=rank,ep=elite_phase,l=level,"
        "p=potential_rank,mq=module_query,ml=module_level,cmp=compare,t=tags,"
        "d=duration_minutes,ai=activity_id,pi=phase_id。值类型保持原契约,数字勿写字符串。"
        "a/c必须完整给全部请求字段;其余状态f/n/g/e空。"
    )
    if module == "recruitment":
        return common + (
            "公招f空,按模型明示c.t列全部非空1..3标签组合(无标签为空组合),"
            "各组候选姓名完整明示,仅复用n编号,不得省略/从池自动补;无候选最低星级null。"
        )
    if module == "operator":
        return common + (
            "非公招n/g空。单候选原值,多天赋分支按原顺序数组;"
            "天赋名字符串或null,解锁{phase,level},潜能requiredPotentialRank+1,黑板对象数组。"
        )
    if module == "garrison":
        return common + (
            "非公招n/g空。文案对象字段:公式 expression,layer_symbol,base_probability,"
            "per_layer_probability_parameter,percentage_coefficient_parameter,"
            "runtime_probability_verified;激活 required_count,configured_scope,"
            "preparation_area_included_in_description;刷新 trigger_text,target_text;"
            "资金 layer_interval,fund_amount,trigger_text;投资 event_config,base_trigger_count,"
            "triple_trigger_layer,triple_trigger_count,threshold_text。原文文案不等于运行核验。"
        )
    raise InvalidData("compact baseline public module invalid")


def _messages(public_input, arm):
    # The full schema is frozen separately. Repeating it inside the request
    # would defeat source-context compression; JSON mode is validated locally.
    return [
        {"role": "system", "content": _instructions(arm, public_input["module"])
         + '空结构示例非答案:{"s":"p","c":{},"f":[],"n":[],"g":[],"e":[],"a":""}'},
        {"role": "user", "content": _json(public_input)},
    ]


def build_prompt(case, arm):
    return _messages(build_compact_input(case, arm), arm)


def to_prediction(content, case, arm, trace, latency_ms, *, public_input=None):
    """Expand a complete model declaration; never fill omissions from evidence."""
    from arknights_agent.evaluation.compact_packets import (
        citation_identity,
        expand_public_input,
    )

    if arm not in ARMS:
        raise InvalidData("compact baseline arm must be closed_book or source_packet")
    if type(content) is not str or len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        _fail("content size")
    output = _strict_loads(content)
    if type(output) is not dict or set(output) != {"s", "c", "f", "n", "g", "e", "a"}:
        _fail("top-level fields")
    if type(output["s"]) is not str or output["s"] not in STATUS_CODES:
        _fail("status")
    status = STATUS_CODES[output["s"]]
    conditions = _conditions(output["c"])
    _string(output["a"], maximum=100_000, label="answer", empty=True)
    names = _strings(output["n"], maximum=1000, width=120, label="candidate dictionary")
    public_input = build_compact_input(case, arm) if public_input is None else public_input
    # Validate that caller-provided frozen context is itself reversible, bound,
    # and exactly the original safe public input, without consulting any gold.
    if _json(expand_public_input(public_input)) != _json(build_public_input(case, arm)):
        _fail("public input differs from bound case")
    keys = public_input["fact_keys"]
    if len(keys) != len(set(keys)):
        _fail("duplicate public fact keys")

    citations, identities = [], set()
    table = public_input.get("source_table", [])
    for entry in _array(output["e"], maximum=300, label="citations"):
        if arm == "closed_book":
            _fail("closed-book citation")
        if type(entry) is not list or len(entry) != 2:
            _fail("citation row")
        source_index = _integer(
            entry[0], minimum=0, maximum=len(table) - 1, label="citation source index"
        )
        if not _citation_visible(public_input, source_index, entry[1]):
            _fail("citation outside supplied fragments")
        identity = (source_index, entry[1])
        if identity in identities:
            _fail("duplicate citations")
        identities.add(identity)
        citations.append(citation_identity(table, source_index, entry[1]))

    facts, seen = [], set()
    for entry in _array(output["f"], maximum=200, label="facts"):
        if type(entry) is not list or len(entry) != 3:
            _fail("fact row")
        index = _integer(entry[0], minimum=0, maximum=len(keys) - 1, label="fact index")
        if index in seen:
            _fail("duplicate facts")
        seen.add(index)
        facts.append({
            "key": keys[index], "value": entry[1],
            "citation_indices": _indices(
                entry[2], count=len(citations), maximum=100, label="fact citation indices"
            ),
        })

    groups, tag_sets, referenced_names = [], set(), set()
    for entry in _array(output["g"], maximum=25, label="combinations"):
        if type(entry) is not list or len(entry) != 3:
            _fail("combination row")
        tags = _strings(entry[0], maximum=3, width=120, label="combination tags")
        identity = tuple(sorted(tags))
        if identity in tag_sets:
            _fail("duplicate combinations")
        tag_sets.add(identity)
        candidates = _indices(
            entry[1], count=len(names), maximum=1000, label="candidate indices"
        )
        referenced_names.update(candidates)
        rarity = entry[2]
        if rarity is not None:
            _integer(rarity, minimum=1, maximum=6, label="minimum rarity")
        if (not candidates) != (rarity is None):
            _fail("empty candidate rarity")
        groups.append({
            "tags": tags, "candidate_names": [names[index] for index in candidates],
            "minimum_rarity": rarity,
        })

    if status in {"answered", "computed"}:
        if public_input["module"] == "recruitment":
            if facts or "tags" not in conditions or len(conditions["tags"]) > 5:
                _fail("recruitment facts or conditions")
            tags = conditions["tags"]
            required = {
                tuple(sorted(group))
                for count in range(1, min(3, len(tags)) + 1)
                for group in combinations(tags, count)
            } if tags else {()}
            if tag_sets != required:
                _fail("incomplete recruitment combination declarations")
            if referenced_names != set(range(len(names))):
                _fail("unused candidate dictionary entry")
        elif groups or names or seen != set(range(len(keys))) or not facts:
            _fail("incomplete non-recruitment facts")
    elif facts or groups or names or citations:
        _fail("non-answer status with structured claims")

    row = {
        "schema_version": 1,
        "case_id": case["id"],
        "release_id": case["release_id"],
        "source_revision": case["source_revision"],
        "status": status,
        "conditions": conditions,
        "facts": facts,
        "combinations": groups,
        "citations": citations,
        "answer": output["a"],
        "trace": dict(trace),
        "latency_ms": latency_ms,
    }
    # Public source content is never marked verified by this codec.
    row["trace"].update(compact_wire_version=COMPACT_PROMPT_VERSION, facts_verified=False)
    return _validate_prediction(row, case)


def encode_prediction(prediction, case, arm):
    """Encode an already declared prediction for local size/round-trip review.

    This is not an inference or prompting path. All fact values, conditions,
    candidates and pointers must already be present in ``prediction``. Audit
    metadata such as citation IDs, snapshot IDs, field hashes/values and trace
    entries are not part of this short model wire; the source identity and
    original pointer survive. Such metadata remains in the original artifact.
    Incomplete or incompatible old predictions are rejected, never repaired.
    """
    row = validate_prediction(prediction)
    if (
        row["case_id"] != case["id"] or row["release_id"] != case["release_id"]
        or row["source_revision"] != case["source_revision"]
    ):
        _fail("prediction binding differs from case")
    statuses = {value: key for key, value in STATUS_CODES.items()}
    if row["status"] not in statuses:
        _fail("status is not a model declaration")
    aliases = {value: key for key, value in CONDITION_ALIASES.items()}
    if not set(row["conditions"]) <= set(aliases):
        _fail("unsupported declared condition")
    public = build_compact_input(case, arm)
    key_indices = {key: index for index, key in enumerate(public["fact_keys"])}
    facts = []
    for fact in row["facts"]:
        if fact["key"] not in key_indices:
            _fail("fact is not in public field list")
        facts.append([key_indices[fact["key"]], fact["value"], fact["citation_indices"]])
    citations = []
    fields = ("source_key", "source_revision", "snapshot_hash", "source_url")
    table = public.get("source_table", [])
    for citation in row["citations"]:
        # Agent audit citations call the already declared snapshot digest
        # content_hash. This rename never obtains a digest from source content.
        identity = {key: citation.get(key) for key in fields}
        if identity["snapshot_hash"] is None:
            identity["snapshot_hash"] = citation.get("content_hash")
        elif "content_hash" in citation and citation["content_hash"] != identity["snapshot_hash"]:
            _fail("conflicting declared snapshot hashes")
        source_indices = [
            index for index, source in enumerate(table)
            if all(identity[key] == source[key] for key in fields)
        ]
        if len(source_indices) != 1 or "pointer" not in citation:
            _fail("declared citation is not in supplied source table")
        citations.append([source_indices[0], citation["pointer"]])
    names, indices, groups = [], {}, []
    for group in row["combinations"]:
        declared = []
        for name in group["candidate_names"]:
            if name not in indices:
                indices[name] = len(names)
                names.append(name)
            declared.append(indices[name])
        groups.append([group["tags"], declared, group["minimum_rarity"]])
    result = {
        "s": statuses[row["status"]],
        "c": {aliases[key]: item for key, item in row["conditions"].items()},
        "f": facts, "n": names, "g": groups, "e": citations, "a": row["answer"],
    }
    # Perform the same strict local validation as a received model declaration.
    # This cannot add a declaration absent from the supplied prediction.
    to_prediction(_json(result), case, arm, {}, row["latency_ms"], public_input=public)
    return result


def response_schema():
    """Describe compact JSON; contextual index/coverage checks remain mandatory."""
    integer = {"type": "integer", "minimum": 0}
    string = {"type": "string", "minLength": 1, "maxLength": 120}

    def array(items, maximum):
        return {"type": "array", "items": items, "maxItems": maximum}

    def tuple_row(items):
        return {
            "type": "array", "prefixItems": items,
            "minItems": len(items), "maxItems": len(items), "items": False,
        }

    condition_properties = {
        "o": {**array(string, 2), "uniqueItems": True},
        "t": {**array(string, 32), "uniqueItems": True},
        "si": {"type": "integer", "minimum": 1, "maximum": 3},
        "r": {"type": "integer", "minimum": 1, "maximum": 10},
        "ep": {"enum": ["PHASE_0", "PHASE_1", "PHASE_2"]},
        "l": {"anyOf": [
            {"type": "integer", "minimum": -2_000_000, "maximum": 2_000_000},
            {"const": "max"},
        ]},
        "p": {"type": "integer", "minimum": 0, "maximum": 5},
        "mq": string,
        "ml": {"type": "integer", "minimum": 1, "maximum": 3},
        "cmp": {"type": "boolean"},
        "d": {"type": "integer", "minimum": -2_000_000, "maximum": 2_000_000},
        "ai": {"type": "string", "minLength": 1, "maxLength": 160},
        "pi": {"type": "string", "minLength": 1, "maxLength": 160},
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object", "additionalProperties": False,
        "required": ["s", "c", "f", "n", "g", "e", "a"],
        "properties": {
            "s": {"enum": list(STATUS_CODES)},
            "c": {
                "type": "object", "additionalProperties": False,
                "properties": condition_properties,
            },
            "f": array(tuple_row([integer, {}, array(integer, 100)]), 200),
            "n": {**array(string, 1000), "uniqueItems": True},
            "g": array(tuple_row([
                {**array(string, 3), "uniqueItems": True},
                {**array(integer, 1000), "uniqueItems": True},
                {"anyOf": [{"type": "integer", "minimum": 1, "maximum": 6},
                           {"type": "null"}]},
            ]), 25),
            "e": array(tuple_row([
                integer, {"type": "string", "maxLength": 2048},
            ]), 300),
            "a": {"type": "string", "maxLength": 100_000},
        },
    }
