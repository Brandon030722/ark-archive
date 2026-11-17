"""Bounded historical equipment and bond fields corroborated by fixed wiki revisions.

The supplementary wiki wording can corroborate a specific configuration field;
it does not promote a complete rules namespace or a probability formula to a
verified gameplay rule. Legacy releases must explicitly enable these fields.
"""

from __future__ import annotations

import re
from datetime import datetime

from arknights_agent import services
from arknights_agent.errors import Conflict
from arknights_agent.ingestion.garrison import ANNOUNCEMENTS
from arknights_agent.ingestion.sample import ACTIVITY_ID


def _request(question):
    """Return whether this expansion owns the query, its scalar key, and an error."""
    labels = [
        label for label in ("商业包装方案", "奥术法阵", "奇迹", "投资人") if label in question
    ]
    golden = bool(re.search(r"(?:金色|进阶|golden)", question, re.I))
    if not labels or (labels in (["商业包装方案"], ["奥术法阵"]) and not golden):
        return False, None, None
    if len(labels) != 1:
        return True, None, "问题包含多个装备或盟约；请分别指定一个字段与阶段。"
    from arknights_agent.garrison import _multiple_scalar_requests

    if _multiple_scalar_requests(question):
        return True, None, "问题包含多个问句；请一次只查询一个已核验配置字段，不能省略其他问题。"
    if re.search(
        r"(?:当前|实际|实战|最终|完整|全部|所有|公式|免疫|Boss|boss|上限|如何|怎么算|规则|机制|玩法)",
        question,
        re.I,
    ):
        return True, None, "仅核验指定配置字段；实战生效、完整规则、概率公式与叠加条件仍未开放。"
    label = labels[0]
    if label in ("奇迹", "投资人") and golden:
        return True, None, "奇迹与投资人是盟约字段，不能套用装备的普通或金色状态。"
    if golden and re.search(r"(?:普通|基础状态|初始状态)", question):
        return True, None, "问题同时指定普通与金色状态，请分别查询。"
    field = None
    extra_words = []
    if label == "商业包装方案":
        if re.search(r"(?:出售|卖|计数|阈值|人数)", question):
            field = "commercial_golden_sell_count"
            extra_words = [
                "出售",
                "卖出",
                "卖掉",
                "卖",
                "计数",
                "人数",
                "干员",
                "名",
                "位",
                "个",
                "人",
            ]
    elif label == "奥术法阵":
        if re.search(r"(?:沉默|失去特殊能力|秒|时长|持续)", question):
            field = "arcane_golden_silence_seconds"
            extra_words = [
                "攻击使目标",
                "使目标",
                "失去特殊能力",
                "沉默",
                "持续",
                "时长",
                "秒数",
                "几秒",
                "多久",
                "秒",
            ]
    elif label == "奇迹":
        probability = bool(re.search(r"(?:概率|baseprob)", question, re.I))
        layers = "层" in question.replace("叠层获得资金", "获得资金")
        funds = bool(
            re.search(r"(?:资金|发钱|给钱|钱)", question.replace("下次刷新不消耗资金", ""))
        )
        if probability and (layers or funds):
            return True, None, "问题包含多个字段或概率的生效条件；请分别查询单个配置字段。"
        if (
            probability
            and re.search(r"(?:配置|参数|baseprob)", question, re.I)
            and re.search(r"(?:基础概率|初始概率|baseprob)", question, re.I)
        ):
            field = "miracle_base_probability"
            extra_words = [
                "免费刷新",
                "下次刷新不消耗资金",
                "基础概率",
                "初始概率",
                "概率",
                "baseprob",
                "基础",
                "初始",
            ]
        elif layers and funds:
            if re.search(r"每(?:一|1)?层", question):
                return True, None, "每层资金属于平均值或计算结果，不能用层数配置间隔代替。"
            field = "miracle_fund_layer_threshold"
            extra_words = [
                "叠层获得资金",
                "每叠加",
                "叠加",
                "累计",
                "层数",
                "层数间隔",
                "间隔",
                "几层",
                "层",
                "获得资金",
                "资金奖励",
                "资金",
                "发钱",
                "给钱",
                "钱",
            ]
            if re.search(
                r"(?:多少钱|多少资金|(?:资金|发钱|给钱).*?(?:数额|数量)|单次数量)", question
            ):
                return True, None, "问题同时查询层数阈值和资金数额；请分别查询。"
        elif funds and re.search(r"(?:多少资金|多少钱|资金.*(?:数额|数量)|单次数量)", question):
            if "次数" in question.replace("单次数量", ""):
                return True, None, "资金数量配置不能用来回答发钱次数；请分别指定一个字段。"
            field = "miracle_fund_gain"
            extra_words = [
                "叠层获得资金",
                "单次数量",
                "单次",
                "每次",
                "获得",
                "资金奖励",
                "奖励资金",
                "资金数额",
                "资金数量",
                "资金",
                "发钱",
                "给钱",
                "钱",
                "奖励",
            ]
    else:
        if "层" in question and re.search(r"(?:3次|三次)", question):
            if re.search(r"(?:次数|几次|多少次)", question):
                return True, None, "问题同时查询层数阈值和触发次数；这里只核验触发3次的层数阈值。"
            field = "investor_triple_trigger_layer"
            extra_words = [
                "获得时",
                "类特质",
                "特质",
                "触发3次",
                "触发三次",
                "3次",
                "三次",
                "层数",
                "几层",
                "层",
            ]
    if field is None:
        return True, None, "请指定已核验的单个配置字段；笼统规则、概率与阈值无法唯一定位。"
    words = [
        label,
        "卫戍协议",
        "盟约",
        "下半期",
        "act2autochess",
        "2026年",
        "活动",
        "装备",
        "V2_1",
        "V2_2",
        "第一阶段",
        "第二阶段",
        "初始阶段",
        "第1阶段",
        "第2阶段",
        "一阶段",
        "二阶段",
        "1阶段",
        "2阶段",
        "金色",
        "进阶",
        "golden",
        "状态",
        "配置",
        "参数",
        "请问",
        "帮我",
        "告诉我",
        "查询",
        "查一下",
        "查",
        "请",
        "需要",
        "达到",
        "触发",
        "阈值",
        "数值",
        "数额",
        "数量",
        "是什么",
        "什么",
        "多少",
        "次数",
        "是在",
        "为",
        "每",
        "要",
        "才",
        "的",
        "在",
        "中",
        "是",
        "值",
        "呢",
        "吗",
        "几",
        "到",
        *extra_words,
    ]
    remainder = question
    for word in sorted(set(words), key=lambda item: (len(item), item), reverse=True):
        remainder = re.sub(re.escape(word), "", remainder, flags=re.I)
    remainder = re.sub(r"[\s，,。.!！?？：:、（）()【】\[\]\-_/\"“”]+", "", remainder)
    if remainder:
        return True, None, "问题还包含未核验的字段或生效条件；请只查询一个已核验配置字段。"
    return True, field, None


def answer_garrison_expansion(answer, selected_phase, question):
    owned, field_key, error = _request(question)
    if not owned:
        return None
    if error:
        return answer.refuse(error)
    if selected_phase is None:
        return answer.refuse(
            "请明确盟约下半期 V2_1 或 V2_2；不能借用另一阶段的配置。", "needs_clarification"
        )
    return _field_answer(answer, selected_phase, field_key)


def _field_answer(answer, selected, field_key):
    # Imported at call time: the parent garrison router owns the answer object.
    from arknights_agent.garrison_expansion_policy import EXPANSION_FIELD_SPECS

    spec_key = f"{selected}:{field_key}"
    spec = EXPANSION_FIELD_SPECS.get(spec_key)
    coverage = answer.result["coverage"]
    if (
        spec is None
        or not coverage.get("cross_checked_field_qa_enabled")
        or spec_key not in coverage.get("cross_checked_rule_fields", [])
    ):
        return answer.refuse(
            "该发布版本尚未核验此阶段、状态与字段；不能借用其他阶段或普通/金色配置。"
        )
    if answer.store is None:
        return answer.refuse("该字段需要重放原始快照核验；本次查询未提供快照存储，暂不回答。")
    return _verified_answer(answer, selected, field_key, spec_key, spec)


def _verified_answer(answer, selected, field_key, spec_key, spec):
    from arknights_agent.garrison import _policy_cite
    from arknights_agent.garrison_expansion_policy import MAPPING_BASIS, VERIFICATION_STATUS
    from arknights_agent.garrison_policy import NAMESPACE

    records = _rows(answer, "cross_checked_rule_field")
    record = _unique(records, spec_key, "expanded field")
    payload = record["data"]
    if (
        not services.same_json_value(payload.get("field_spec"), spec)
        or payload.get("field_value_hash") != services.content_digest(spec["value"])
        or payload.get("mapping_basis") != MAPPING_BASIS
        or payload.get("verification_status") != VERIFICATION_STATUS
        or payload.get("gameplay_application_verified") is not False
        or payload.get("namespace_phase_mapping") != "unverified"
        or payload.get("official_announcement_record") != spec["official_article_id"]
    ):
        raise Conflict("garrison expanded projection differs from the bounded audit policy")
    citations = [
        _policy_cite(
            answer, record, spec["relative_selector"], spec["value"], spec, spec["evidence_pointer"]
        )
    ]
    for selector, expected in spec["buff_checks"]:
        citations.append(
            _policy_cite(answer, record, selector, expected, spec, spec["evidence_pointer"])
        )
    official = ANNOUNCEMENTS[spec["official_article_id"]]
    stamp = int(datetime.fromisoformat(official["maintenance_start"]).timestamp())
    identities = {
        "phase": (
            "historical_phase",
            selected,
            f"/autoChessData/versionInfoDict/{selected}",
            {"activityId": ACTIVITY_ID, "seasonName": "盟约", "startTime": stamp},
        ),
        "effect": (
            "historical_effect",
            spec["effect_id"],
            f"{NAMESPACE}/effectInfoDataDict/{spec['effect_id']}",
            {
                "effectId": spec["effect_id"],
                "effectName": spec["label"],
                "effectType": "EQUIP" if spec["entity_type"] == "equipment" else "BOND",
            },
        ),
    }
    if spec["entity_type"] == "equipment":
        identities.update(
            shop=(
                "historical_shop",
                spec["normal_item_key"],
                f"{NAMESPACE}/trapShopChessDatas/{spec['normal_item_key']}",
                {
                    "itemId": spec["normal_item_key"],
                    "goldenItemId": spec["golden_item_key"],
                    "trapId": spec["char_id"],
                },
            ),
            normal_equipment=(
                "historical_equipment",
                spec["normal_item_key"],
                f"{NAMESPACE}/trapChessDataDict/{spec['normal_item_key']}",
                {
                    "chessId": spec["normal_item_key"],
                    "charId": spec["char_id"],
                    "isGolden": False,
                    "upgradeChessId": spec["golden_item_key"],
                },
            ),
            equipment=(
                "historical_equipment",
                spec["golden_item_key"],
                f"{NAMESPACE}/trapChessDataDict/{spec['golden_item_key']}",
                {
                    "chessId": spec["golden_item_key"],
                    "charId": spec["char_id"],
                    "isGolden": True,
                    "effectId": spec["effect_id"],
                },
            ),
        )
    elif spec["entity_type"] == "bond":
        identities["bond"] = (
            "historical_bond",
            spec["bond_id"],
            f"{NAMESPACE}/bondInfoDict/{spec['bond_id']}",
            {"bondId": spec["bond_id"], "name": spec["label"], "effectId": spec["effect_id"]},
        )
    else:
        raise Conflict("garrison expanded field has an unsupported entity type")
    relations = payload.get("relation_record_ids", {})
    if set(relations) != set(identities):
        raise Conflict("garrison expanded field native chain differs from policy")
    for role, (kind, native_key, pointer, fields) in identities.items():
        native_role = "equipment" if role == "normal_equipment" else role
        ident = f"{spec['revision']}:{native_role}:{native_key}"
        if relations.get(role) != ident:
            raise Conflict(
                "garrison expanded field identity references another state, revision or phase"
            )
        related = _unique(_rows(answer, kind), ident, "expanded identity")
        for key, value in fields.items():
            citations.append(_policy_cite(answer, related, f"/{key}", value, spec, pointer))
    announcement = _unique(
        _rows(answer, "official_phase_announcement"),
        spec["official_article_id"],
        "expanded official phase",
    )
    if (
        announcement["data"].get("phase_id") != selected
        or announcement["data"].get("activity_id") != ACTIVITY_ID
    ):
        raise Conflict("garrison expanded field official phase differs")
    for selector, value in (
        (announcement["data"]["schedule_selector"], official["lead"]),
        (announcement["data"]["publication_date_selector"], official["publication_date"]),
    ):
        citations.append(
            _policy_cite(answer, announcement, selector, value, spec, "", official=True)
        )
    expected_wiki_ids = [str(proof["revision"]) for proof in spec["wiki_evidence"]]
    if payload.get("wiki_content_record_ids") != expected_wiki_ids:
        raise Conflict("garrison expanded field fixed wiki revision references differ")
    for proof in spec["wiki_evidence"]:
        citations.append(_wiki_citation(answer, proof))
    labels = {
        "commercial_golden_sell_count": "金色（进阶）商业包装方案出售计数配置阈值",
        "arcane_golden_silence_seconds": "金色（进阶）奥术法阵失去特殊能力持续时间配置",
        "miracle_fund_layer_threshold": "奇迹叠层获得资金效果的层数配置间隔",
        "miracle_fund_gain": "奇迹叠层获得资金效果的资金数量配置",
        "miracle_base_probability": "奇迹 baseprob 基础概率配置参数",
        "investor_triple_trigger_layer": "投资人“获得时”类特质触发3次的层数配置阈值",
    }
    label = labels[field_key]
    answer.claim(
        f"{selected} {label}", spec["value"], list(dict.fromkeys(citations)), VERIFICATION_STATUS
    )
    version = answer.result["version"]
    version.update(
        base_metadata_source_revision=version["source_revision"],
        source_revision=spec["revision"],
        phase_id=selected,
        activity_id=ACTIVITY_ID,
        verified_field_key=field_key,
        mapping_basis=MAPPING_BASIS,
        namespace_phase_mapping="unverified",
        gameplay_application_verified=False,
        fixed_wiki_revisions=expected_wiki_ids,
    )
    value = f"{spec['value']:g}"
    unit = "" if field_key == "miracle_base_probability" else f" {spec['unit']}"
    message = f"{selected}，{label}：{value}{unit}。"
    if field_key == "miracle_base_probability":
        message += "这是基础参数，不等于任意层数下的实战概率；概率公式尚未核验。"
    links = "；".join(
        f"[PRTS 固定修订 {proof['revision']}]({proof['page_url']})"
        for proof in spec["wiki_evidence"]
    )
    answer.result["answer"] = (
        message
        + "\n范围：act2autochess 盟约下半期。字段由历史客户端配置与 PRTS 固定原始修订交叉核验；"
        "官方公告用于佐证阶段安排，不单独证明本字段数值或金色状态。"
        "完整规则命名空间归期及实际引擎生效条件仍未核验。"
        + f"\n版本：{answer.release.id}；历史配置修订：{spec['revision']}。"
        + f"\n来源：[固定历史配置]({spec['source_url']})；{links}；"
        + f"[官方阶段公告 {spec['official_article_id']}](https://ak.hypergryph.com/news/{spec['official_article_id']})。"
        + "\n证据："
        + "、".join(f"[{item}]" for item in dict.fromkeys(citations))
    )
    answer.result["tool_trace"].append(
        {
            "tool": "garrison_cross_checked_field_gate",
            "field_spec_key": spec_key,
            "phase_id": selected,
            "source_revision": spec["revision"],
            "verification_status": VERIFICATION_STATUS,
            "mapping_basis": MAPPING_BASIS,
            "fixed_wiki_revisions": expected_wiki_ids,
            "namespace_phase_mapping": "unverified",
            "snapshot_verification": "sha256_and_original_document_pointer",
            "identity_chain": "normal_shop_to_golden_equipment_to_effect"
            if spec["entity_type"] == "equipment"
            else "bond_to_effect_to_named_buff_parameter",
            "wiki_template_expansion": "not_used",
            "rules_engine_enabled": False,
        }
    )
    return answer.result


def _rows(answer, kind):
    from arknights_agent.garrison import _rows as query_rows

    return query_rows(answer.session, answer.release, kind)


def _unique(rows, ident, label):
    matches = [row for row in rows if row["native_id"] == ident]
    if len(matches) != 1:
        raise Conflict(f"garrison {label} record is missing or ambiguous")
    return matches[0]


def _wiki_citation(answer, proof):
    from arknights_agent.garrison_expansion_policy import CONTENT_POINTER, WIKI_SOURCES

    pin = WIKI_SOURCES[proof["revision"]]
    if (
        any(
            not services.same_json_value(proof[key], pin[key])
            for key in ("title", "source_key", "url", "page_url", "snapshot_hash", "byte_size")
        )
        or proof["content_pointer"] != CONTENT_POINTER
        or proof["quote_hash"] != services.content_digest(proof["quote"])
    ):
        raise Conflict("garrison fixed wiki witness differs from the bounded audit pin")
    record = _unique(_rows(answer, "expansion_wiki_content"), str(proof["revision"]), "fixed wiki")
    text = record["data"]
    if (
        not isinstance(text, str)
        or services.content_digest(text) != pin["content_hash"]
        or text.count(proof["quote"]) != 1
        or text.count(proof["context_quote"]) != 1
        or text.index(proof["context_quote"]) >= text.index(proof["quote"])
    ):
        raise Conflict("garrison fixed wiki quote or phase context differs")
    context_offset, quote_offset = text.index(proof["context_quote"]), text.index(proof["quote"])
    if proof["context_kind"] == "march27_change_section":
        if (
            "|名称=卫戍协议：盟约 下半\n" not in text
            or "|活动开始时间=2026-03-14 16:00\n" not in text
            or quote_offset > text.index("</div>", context_offset)
        ):
            raise Conflict("garrison fixed wiki quote is outside the phase change section")
    elif proof["context_kind"] == "named_item_block":
        next_name = text.find("|名称=", context_offset + len(proof["context_quote"]))
        if next_name >= 0 and quote_offset >= next_name:
            raise Conflict("garrison fixed wiki state quote belongs to another item block")
    else:
        raise Conflict("garrison fixed wiki context type is unsupported")
    citation_id = answer.cite(record, "", text)
    citation = next(row for row in answer.result["citations"] if row["id"] == citation_id)
    snapshot = services.get_snapshot(answer.session, answer.release, citation["snapshot_id"])
    expected = {
        "source_key": pin["source_key"],
        "source_type": "community_wiki_revision",
        "url": pin["url"],
        "revision": str(pin["revision"]),
        "content_hash": pin["snapshot_hash"],
        "byte_size": pin["byte_size"],
        "media_type": "application/json",
    }
    if (
        any(not services.same_json_value(snapshot.get(key), value) for key, value in expected.items())
        or citation["evidence_pointer"] != CONTENT_POINTER
        or citation["pointer"] != CONTENT_POINTER
    ):
        raise Conflict("garrison fixed wiki source or original pointer differs from audit pin")
    citation["fixed_wiki_witness"] = {
        "quote": proof["quote"],
        "quote_hash": proof["quote_hash"],
        "quote_offset": quote_offset,
        "context_quote": proof["context_quote"],
        "context_offset": context_offset,
        "page_url": proof["page_url"],
        "template_expansion": "not_used",
        "offset_semantics": "unicode_codepoints_in_original_content_field",
    }
    return citation_id
