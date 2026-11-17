"""Audited field policy; it never assigns an entire rules namespace to a phase.

Pins refer to original source bytes, not synthesized fixtures or normalized JSON.
The official wording corroborates individual client fields. Neither mirror dates
nor these constants establish actual server activation or engine behavior.
"""

from arknights_agent.ingestion.sample import ACTIVITY_ID, source_key

NAMESPACE = f"/activity/AUTOCHESS_SEASON/{ACTIVITY_ID}"
MAPPING_BASIS = "historical_revision_and_official_change_cross_checked"
VERIFICATION_STATUS = "official_cross_checked_field"

HISTORICAL_SOURCES = {
    "1457587e48fe65361f523821d24e6d14c3b91713": {
        "snapshot_hash": "1ccc12d2c93a42b6b632b6a03528bdf2e41e03061491710d13efdd26deab8933",
        "byte_size": 12804830,
        "commit_timestamp": "2026-03-14T08:15:03Z",
        "candidate_phase_id": "V2_1",
    },
    "86b6e749d3384dd0c7b825abbb795845dc6df933": {
        "snapshot_hash": "f4b4d057fd4714915fb96d5719de7e8882da31026abbb8c38272b832cdb10e95",
        "byte_size": 12886408,
        "commit_timestamp": "2026-03-27T08:14:58Z",
        "candidate_phase_id": "V2_2",
    },
}

OFFICIAL_SOURCE_HASHES = {
    "5114": "2b32870001566ff1a034353d33d282ad6cb426bb72c6fb2885086dee457349e8",
    "8584": "eca8481d526eb0d6e959fb52b3ee837c5e8d4a3961d17a294771fa87937ab9e7",
}


def historical_source_key(revision):
    return source_key("activity_table.json") + "@" + revision


def historical_url(revision):
    return (
        "https://raw.githubusercontent.com/Kengxxiao/ArknightsGameData/"
        f"{revision}/zh_CN/gamedata/excel/activity_table.json"
    )


def _spec(
    phase_id,
    field_key,
    item_key,
    golden_item_key,
    char_id,
    effect_id,
    label,
    value,
    relative_selector,
    unit,
    official_article_id,
    official_selector,
    official_quote,
):
    revision = next(
        key for key, pin in HISTORICAL_SOURCES.items() if pin["candidate_phase_id"] == phase_id
    )
    return {
        "activity_id": ACTIVITY_ID,
        "phase_id": phase_id,
        "field_key": field_key,
        "state": "normal",
        "revision": revision,
        "snapshot_hash": HISTORICAL_SOURCES[revision]["snapshot_hash"],
        "source_key": historical_source_key(revision),
        "source_url": historical_url(revision),
        "item_key": item_key,
        "golden_item_key": golden_item_key,
        "char_id": char_id,
        "effect_id": effect_id,
        "label": label,
        "value": value,
        "unit": unit,
        "evidence_pointer": NAMESPACE + f"/effectBuffInfoDataDict/{effect_id}",
        "relative_selector": relative_selector,
        "field_pointer": NAMESPACE + f"/effectBuffInfoDataDict/{effect_id}" + relative_selector,
        "official_article_id": official_article_id,
        "official_selector": official_selector,
        "official_quote": official_quote,
        "official_hash": OFFICIAL_SOURCE_HASHES[official_article_id],
    }


FIELD_SPECS = {
    "V2_1:commercial_sell_count": _spec(
        "V2_1",
        "commercial_sell_count",
        "chess_item_5_07_e_a",
        "chess_item_5_07_e_b",
        "trap_1074_acarm074",
        "eff_acarm074",
        "商业包装方案",
        7.0,
        "/0/blackboard/0/value",
        "名干员",
        "5114",
        "/html_text_nodes/55",
        "-装备【商业包装方案】，效果调整为：每出售7名干员，额外获得一个此装备携带者的同盟约初始干员（不高于当前调度中心等级）",
    ),
    "V2_2:commercial_sell_count": _spec(
        "V2_2",
        "commercial_sell_count",
        "chess_item_5_07_e_a",
        "chess_item_5_07_e_b",
        "trap_1074_acarm074",
        "eff_acarm074",
        "商业包装方案",
        8.0,
        "/0/blackboard/0/value",
        "名干员",
        "8584",
        "/html_text_nodes/45",
        "-装备【商业包装方案】，效果调整为：每出售8名干员，额外获得一个此装备携带者的同盟约初始干员（不高于当前调度中心等级）",
    ),
    "V2_2:arcane_silence_seconds": _spec(
        "V2_2",
        "arcane_silence_seconds",
        "chess_item_3_08_e_a",
        "chess_item_3_08_e_b",
        "trap_1048_acarm048",
        "eff_acarm048",
        "奥术法阵",
        5.0,
        "/1/blackboard/1/value",
        "秒",
        "8584",
        "/html_text_nodes/44",
        "-装备【奥术法阵】，新增效果：攻击使目标失去特殊能力5秒",
    ),
}
