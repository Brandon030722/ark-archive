"""Fixed community corroboration for individual historical configuration fields.

The official sources establish the scheduled phase and adjustment category;
the numeric before/after and golden-state witnesses come from fixed original
Wiki revisions. No probability formula or actual engine effect is authorized.
"""

import httpx

from arknights_agent.garrison_policy import (
    HISTORICAL_SOURCES,
    NAMESPACE,
    OFFICIAL_SOURCE_HASHES,
    historical_source_key,
    historical_url,
)
from arknights_agent.ingestion.garrison import ANNOUNCEMENTS
from arknights_agent.ingestion.sample import ACTIVITY_ID
from arknights_agent.services import content_digest

CONTENT_POINTER = "/query/pages/0/revisions/0/slots/main/content"
MAPPING_BASIS = "historical_client_and_fixed_wiki_change_cross_checked"
VERIFICATION_STATUS = "client_and_fixed_wiki_cross_checked_field"
CHANGE_CONTEXT = "{{AKCollapse/title|3月27日更新#2调整}}"


def wiki_url(revision):
    return str(
        httpx.URL(
            "https://prts.wiki/api.php",
            params={
                "action": "query",
                "prop": "revisions",
                "revids": str(revision),
                "rvprop": "ids|timestamp|content",
                "rvslots": "main",
                "format": "json",
                "formatversion": "2",
            },
        )
    )


WIKI_SOURCES = {
    439189: {
        "revision": 439189,
        "title": "卫戍协议：盟约 下半",
        "source_key": "prts:garrison:439189",
        "url": wiki_url(439189),
        "page_url": "https://prts.wiki/index.php?oldid=439189",
        "snapshot_hash": "10c47f8872154cffb7dc74358c7a3aedea8bd0fa03845cd2aaf67f257ab2d201",
        "byte_size": 79383,
        "content_hash": "4f4b4a602886576ef1d06fa02ddc84815a579457e18fbe4c58cb900be6d826a8",
        "timestamp": "2026-10-06T09:55:55Z",
    },
    430572: {
        "revision": 430572,
        "title": "卫戍协议：盟约 下半/PRTS盟约记录",
        "source_key": "prts:garrison:430572",
        "url": wiki_url(430572),
        "page_url": "https://prts.wiki/index.php?oldid=430572",
        "snapshot_hash": "9f599d9a39f0d51a352662f1cee832922bf5c9d39a524dff5efb0a64877c5997",
        "byte_size": 168360,
        "content_hash": "f8734a712084f343c2a3616b918ed3500a045220bec4773adbf597dc64b23ceb",
        "timestamp": "2026-10-01T03:04:43Z",
    },
}

CHANGE_QUOTES = {
    "commercial": "* [Ⅴ阶]商业包装方案：效果触发的出售干员数量阈值由 初始7名/进阶6名 调整至 初始8名/进阶7名",
    "arcane": "* [Ⅲ阶]奥术法阵：新增效果“攻击使目标失去特殊能力5秒”",
    "miracle": "* 奇迹：下次刷新不消耗资金的基础概率从25%降至18%；叠层给予资金的效果由“每50层获得10资金”调整至“每100层获得20资金”",
    "investor": "* 投资人：【“获得时”类特质每次触发3次】的触发层级由70层升至100层",
}
ARCANE_STATE_QUOTE = (
    "|效果=【基础】法术抗性+20，{{修正|造成伤害时|原文=攻击|原因=6|group=奥术法阵|name=并非攻击}}使目标失去特殊能力5秒<br>"
    "{{color|#b78000|【进阶】法术抗性{{发光|#b78000|+30|1|文本颜色=white}}，"
    "{{修正|造成伤害时|原文=攻击|原因=6|group=奥术法阵|name=并非攻击}}使目标失去特殊能力5秒}}"
)


def _witness(revision, quote, context_quote, context_kind):
    return {
        **WIKI_SOURCES[revision],
        "content_pointer": CONTENT_POINTER,
        "quote": quote,
        "quote_hash": content_digest(quote),
        "context_quote": context_quote,
        "context_kind": context_kind,
    }


def _spec(
    phase, key, label, value, selector, unit, entity_type, effect_id, witnesses, checks, **identity
):
    revision = next(
        rev for rev, pin in HISTORICAL_SOURCES.items() if pin["candidate_phase_id"] == phase
    )
    article = "5114" if phase == "V2_1" else "8584"
    return {
        "activity_id": ACTIVITY_ID,
        "phase_id": phase,
        "field_key": key,
        "entity_type": entity_type,
        "state": "golden" if entity_type == "equipment" else "bond",
        "revision": revision,
        "snapshot_hash": HISTORICAL_SOURCES[revision]["snapshot_hash"],
        "source_key": historical_source_key(revision),
        "source_url": historical_url(revision),
        "label": label,
        "value": value,
        "unit": unit,
        "effect_id": effect_id,
        "evidence_pointer": f"{NAMESPACE}/effectBuffInfoDataDict/{effect_id}",
        "relative_selector": selector,
        "field_pointer": f"{NAMESPACE}/effectBuffInfoDataDict/{effect_id}{selector}",
        "buff_checks": checks,
        "wiki_evidence": witnesses,
        "official_article_id": article,
        "official_hash": OFFICIAL_SOURCE_HASHES[article],
        "official_quote": ANNOUNCEMENTS[article]["lead"],
        "official_evidence_role": "scheduled_phase_not_numeric_or_golden_proof",
        **identity,
    }


EXPANSION_FIELD_SPECS = {}
for _phase, _commercial, _layers, _funds, _baseprob, _investor in (
    ("V2_1", 6.0, 50.0, 10.0, 0.25, 70.0),
    ("V2_2", 7.0, 100.0, 20.0, 0.18, 100.0),
):
    _commercial_key = "commercial_golden_sell_count"
    EXPANSION_FIELD_SPECS[f"{_phase}:{_commercial_key}"] = _spec(
        _phase,
        _commercial_key,
        "商业包装方案",
        _commercial,
        "/0/blackboard/0/value",
        "名干员",
        "equipment",
        "eff_acgarm074",
        [_witness(439189, CHANGE_QUOTES["commercial"], CHANGE_CONTEXT, "march27_change_section")],
        [["/0/key", "sell_char_count_gain_equip_owner_bond"], ["/0/blackboard/0/key", "count"]],
        item_key="chess_item_5_07_e_b",
        normal_item_key="chess_item_5_07_e_a",
        golden_item_key="chess_item_5_07_e_b",
        char_id="trap_1074_acarm074",
    )
    for _key, _value, _selector, _unit, _checks in (
        (
            "miracle_fund_layer_threshold",
            _layers,
            "/1/blackboard/0/value",
            "层",
            [
                ["/1/key", "bond_layer_gain_coin"],
                ["/1/blackboard/0/key", "layer"],
                ["/1/blackboard/1/key", "count"],
            ],
        ),
        (
            "miracle_fund_gain",
            _funds,
            "/1/blackboard/1/value",
            "资金",
            [
                ["/1/key", "bond_layer_gain_coin"],
                ["/1/blackboard/0/key", "layer"],
                ["/1/blackboard/1/key", "count"],
            ],
        ),
        (
            "miracle_base_probability",
            _baseprob,
            "/0/blackboard/2/value",
            "基础概率配置参数",
            [["/0/key", "bond_refresh_shop_next_free"], ["/0/blackboard/2/key", "baseprob"]],
        ),
    ):
        EXPANSION_FIELD_SPECS[f"{_phase}:{_key}"] = _spec(
            _phase,
            _key,
            "奇迹",
            _value,
            _selector,
            _unit,
            "bond",
            "bondeffect_mira",
            [_witness(439189, CHANGE_QUOTES["miracle"], CHANGE_CONTEXT, "march27_change_section")],
            _checks,
            bond_id="miraShip",
        )
    _investor_key = "investor_triple_trigger_layer"
    EXPANSION_FIELD_SPECS[f"{_phase}:{_investor_key}"] = _spec(
        _phase,
        _investor_key,
        "投资人",
        _investor,
        "/1/blackboard/0/value",
        "层",
        "bond",
        "bondeffect_invest",
        [_witness(439189, CHANGE_QUOTES["investor"], CHANGE_CONTEXT, "march27_change_section")],
        [
            ["/1/key", "bond_layer_char_garrison_bonus"],
            ["/1/blackboard/0/key", "layer"],
            ["/1/blackboard/1/key", "event"],
            ["/1/blackboard/1/valueStr", "SERVER_GAIN"],
            ["/1/blackboard/2/key", "count"],
            ["/1/blackboard/2/value", 3.0],
        ],
        bond_id="investShip",
    )

EXPANSION_FIELD_SPECS["V2_2:arcane_golden_silence_seconds"] = _spec(
    "V2_2",
    "arcane_golden_silence_seconds",
    "奥术法阵",
    5.0,
    "/1/blackboard/1/value",
    "秒",
    "equipment",
    "eff_acgarm048",
    [
        _witness(439189, CHANGE_QUOTES["arcane"], CHANGE_CONTEXT, "march27_change_section"),
        _witness(430572, ARCANE_STATE_QUOTE, "|名称=奥术法阵", "named_item_block"),
    ],
    [
        ["/1/key", "env_gbuff_new_with_verify"],
        ["/1/blackboard/0/key", "key"],
        ["/1/blackboard/0/valueStr", "silence_attachment"],
        ["/1/blackboard/1/key", "silence"],
    ],
    item_key="chess_item_3_08_e_b",
    normal_item_key="chess_item_3_08_e_a",
    golden_item_key="chess_item_3_08_e_b",
    char_id="trap_1048_acarm048",
)
