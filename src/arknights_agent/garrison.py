"""Bounded garrison metadata and explicitly corroborated historic fields.

Historic rule payload assignment is an executable gate. A retained activity's
shop, chess, bond or equipment values are never silently attributed to a phase.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from arknights_agent import services
from arknights_agent.errors import Conflict, InvalidData
from arknights_agent.garrison_policy import (
    FIELD_SPECS,
    HISTORICAL_SOURCES,
    MAPPING_BASIS,
    NAMESPACE,
    VERIFICATION_STATUS,
)
from arknights_agent.ingestion.garrison import ANNOUNCEMENTS, TIMEZONE, html_document
from arknights_agent.ingestion.sample import ACTIVITY_ID

_RULE_WORDS = re.compile(
    r"(?:技能|天赋|模组|羁绊|盟约效果|策略|伤害|攻击|生命|法抗|防御|装备|"
    r"棋子|干员|商店|费用|回合|层数|机制|规则|玩法|强度|最强|阵容|怎么玩|"
    r"模式|模拟|上限|加成|掉落|概率|出售|卖出|计数|阈值|售价|价格|沉默|特殊能力|"
    r"普通|金色|进阶|奇迹|投资人|远见)"
)
_CURRENT_WORDS = re.compile(r"(?:当前|现在|最新|今天|今日|正在|最近|今年)")


def _rows(session, release, kind):
    result, offset = [], 0
    while True:
        page = services.list_records(
            session, release, module="garrison", kind=kind, limit=200, offset=offset
        )
        result.extend(page)
        if len(page) < 200:
            return result
        offset += 200


class _Answer:
    def __init__(self, session, release, question, store):
        self.session, self.release, self.store = session, release, store
        dataset = release.manifest.get("datasets", {}).get("garrison", {})
        self.result = {
            "status": "answered",
            "question": question,
            "answer": "",
            "module": "garrison",
            "release_id": release.id,
            "manifest_hash": release.manifest_hash,
            "version": {
                "release_id": release.id,
                "manifest_hash": release.manifest_hash,
                "source_revision": dataset.get("source_revision"),
                "effective_date_verified": False,
            },
            "scope": dataset.get("scope", {}),
            "coverage": dataset.get("coverage", {}),
            "claims": [],
            "citations": [],
            "tool_trace": [],
            "limitations": [
                "资料对应 act2autochess 盟约下半期历史活动；不表示当前活动。",
                "客户端起始时间是配置边界；官方公告规定维护后开启，未核验实际开服时刻。",
                "完整规则命名空间未证明分别适用于 V2_1 或 V2_2；仅可查询已交叉核验的字段。",
            ],
        }
        self.documents = {}

    def refuse(self, message, status="needs_verification"):
        self.result.update(status=status, answer=message, claims=[], citations=[])
        return self.result

    def cite(self, record, selector, expected):
        for evidence_id in record["evidence_ids"]:
            evidence = services.get_evidence(self.session, self.release, evidence_id)
            if services.content_digest(evidence["excerpt"]) != evidence["excerpt_hash"]:
                raise Conflict("garrison evidence failed excerpt hash verification")
            try:
                value = services.json_pointer(evidence["excerpt"], selector)
            except InvalidData:
                continue
            if not services.same_json_value(value, expected):
                continue
            snapshot = services.get_snapshot(self.session, self.release, evidence["snapshot_id"])
            full_pointer = evidence["pointer"] + selector
            if self.store is not None:
                digest = snapshot["content_hash"]
                if digest not in self.documents:
                    raw = self.store.read_verified(digest)
                    if len(raw) != snapshot["byte_size"]:
                        raise Conflict("garrison source snapshot size mismatch")
                    self.documents[digest] = (
                        html_document(raw)
                        if snapshot["media_type"].split(";")[0] == "text/html"
                        else json.loads(raw)
                    )
                if not services.same_json_value(
                    services.json_pointer(self.documents[digest], full_pointer), expected
                ):
                    raise Conflict("garrison field differs from original source snapshot")
                if not services.same_json_value(
                    services.json_pointer(self.documents[digest], evidence["pointer"]),
                    evidence["excerpt"],
                ):
                    raise Conflict("garrison evidence excerpt differs from original source snapshot")
            citation = {
                "id": f"c{len(self.result['citations']) + 1}",
                "record_id": record["id"],
                "dataset_id": record["dataset_id"],
                "evidence_id": evidence_id,
                "snapshot_id": snapshot["id"],
                "source_url": snapshot["url"],
                "source_revision": snapshot["revision"],
                "snapshot_hash": snapshot["content_hash"],
                "source_media_type": snapshot["media_type"],
                "pointer": full_pointer,
                "evidence_pointer": evidence["pointer"],
                "excerpt_hash": evidence["excerpt_hash"],
                "field_value": expected,
                "field_hash": services.content_digest(expected),
                "selector_semantics": snapshot.get("metadata", {}).get(
                    "selector_semantics", "RFC6901_on_original_JSON"
                ),
            }
            for previous in self.result["citations"]:
                if (
                    previous["snapshot_id"] == snapshot["id"]
                    and previous["pointer"] == full_pointer
                ):
                    return previous["id"]
            self.result["citations"].append(citation)
            return citation["id"]
        raise Conflict("garrison claim has no matching source field evidence")

    def claim(self, label, value, citations, status, derived=None):
        result = {
            "id": f"f{len(self.result['claims']) + 1}",
            "label": label,
            "value": value,
            "verification_status": status,
            "citation_ids": citations,
        }
        if derived is not None:
            result["derived"] = derived
        self.result["claims"].append(result)
        return result


def _selected_phase(question, phase_id):
    mentioned = [ident for ident in ("V2_1", "V2_2") if ident.lower() in question.lower()]
    if re.search(r"(?:第二|第2|二|2)阶段", question):
        mentioned.append("V2_2")
    if re.search(r"(?:第一|第1|一|1|初始)阶段", question):
        mentioned.append("V2_1")
    mentioned = list(dict.fromkeys(mentioned))
    if len(mentioned) > 1 or (mentioned and phase_id and mentioned[0] != phase_id):
        return None, "问题中的阶段与指定 phase_id 冲突，请明确 V2_1 或 V2_2。"
    if phase_id and phase_id not in ("V2_1", "V2_2"):
        return None, "该发布版本仅核验了 act2autochess 的 V2_1 和 V2_2。"
    if re.search(r"(?:第一|第二|第1|第2|一|二|1|2)期", question):
        return None, "请区分活动上半期/下半期与下半期中的第一阶段/第二阶段；可指定 V2_1 或 V2_2。"
    return phase_id or (mentioned[0] if mentioned else None), None


def _phase_claims(answer, phase, announcement):
    payload = phase["data"]
    article_id = payload.get("official_announcement_record")
    expected = ANNOUNCEMENTS.get(article_id)
    if (
        expected is None
        or phase["native_id"] != expected["phase_id"]
        or payload.get("activity_id") != ACTIVITY_ID
        or payload.get("mapping_status") != "scheduled_phase_metadata_verified"
        or announcement["native_id"] != article_id
    ):
        raise Conflict("garrison reviewed phase/announcement identity mismatch")
    stamp = payload.get("client_start_timestamp")
    if not services.same_json_value(
        stamp, int(datetime.fromisoformat(expected["maintenance_start"]).timestamp())
    ):
        raise Conflict("garrison reviewed phase timestamp differs from official schedule")
    client_citation = answer.cite(phase, "/startTime", stamp)
    identity_citation = answer.cite(phase, "/activityId", ACTIVITY_ID)
    official_citation = answer.cite(
        announcement, announcement["data"]["schedule_selector"], expected["lead"]
    )
    publication_citation = answer.cite(
        announcement,
        announcement["data"]["publication_date_selector"],
        expected["publication_date"],
    )
    iso = datetime.fromtimestamp(stamp, TIMEZONE).isoformat()
    answer.claim(
        f"{phase['native_id']} 客户端起始配置",
        iso,
        [client_citation, identity_citation],
        "client_official_schedule_corroborated",
        {"operation": "unix_timestamp_to_iso8601", "timezone": "Asia/Shanghai"},
    )
    answer.claim(
        f"{phase['native_id']} 官方维护窗口与开启条件",
        {
            "maintenance_start": expected["maintenance_start"],
            "maintenance_end": expected["maintenance_end"],
            "opening_condition": "after_maintenance",
            "actual_opening_time_verified": False,
            "official_label": expected["official_label"],
        },
        [official_citation, publication_citation, identity_citation],
        "official_schedule_verified",
        {"operation": "checked_official_announcement_schedule_extraction"},
    )
    return (
        f"{phase['native_id']}（{expected['phase_label']}）：客户端配置起始时间为 {iso}；"
        f"官方安排 {expected['maintenance_start']} 至 {expected['maintenance_end']} 维护，"
        "在维护后开启。实际开服时刻尚未核验。"
    )


def _rule_field_request(question):
    """Recognize only narrow scalar requests, rejecting unexplained remainder.

    A matching item name is not authorization to answer other effects, rewards,
    runtime interactions, or a second request hidden after a known question.
    """
    labels = [label for label in ("商业包装方案", "奥术法阵") if label in question]
    if not labels:
        return None, None
    if len(labels) != 1:
        return None, "问题包含多个装备；请分别指定一个已核验字段与阶段。"
    if _multiple_scalar_requests(question):
        return None, "问题包含多个问句；请一次只查询一个已核验配置字段，不能省略其他问题。"
    if re.search(r"(?:金色|进阶|升级|合成|golden|全部|所有|完整|规则|玩法)", question, re.I):
        return None, "仅核验普通（初始）状态的指定字段；进阶、完整规则与其他状态仍未开放。"
    label = labels[0]
    commercial = label == "商业包装方案"
    if commercial and not re.search(r"(?:出售|卖|计数|阈值|数量|人数)", question):
        return None, "商业包装方案目前仅可查询普通状态出售干员的计数阈值。"
    if not commercial and not re.search(r"(?:沉默|失去特殊能力|秒|时长|持续|新增效果)", question):
        return None, "奥术法阵目前仅可查询第二阶段普通状态失去特殊能力的持续秒数。"
    words = [
        label,
        "卫戍协议",
        "盟约",
        "下半期",
        "act2autochess",
        "2026年",
        "活动",
        "装备",
        "第二阶段",
        "第一阶段",
        "初始阶段",
        "第2阶段",
        "第1阶段",
        "二阶段",
        "一阶段",
        "2阶段",
        "1阶段",
        "V2_1",
        "V2_2",
        "普通",
        "初始",
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
        "是什么",
        "什么",
        "多少",
        "数量",
        "数值",
        "计数",
        "阈值",
        "次数",
        "人数",
        "几个",
        "几名",
        "多久",
        "持续",
        "时长",
        "时间",
        "秒数",
        "几秒",
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
    ]
    if commercial:
        words += ["出售", "卖出", "卖掉", "卖", "干员", "名", "位", "个", "人"]
    else:
        words += ["攻击使目标", "使目标", "失去特殊能力", "沉默", "新增效果", "秒"]
    remainder = question
    for word in sorted(set(words), key=len, reverse=True):
        remainder = re.sub(re.escape(word), "", remainder, flags=re.I)
    remainder = re.sub(r"[\s，,。.!！?？：:、（）()【】\[\]\-_/]+", "", remainder)
    if remainder:
        return None, "问题还包含未核验的字段或生效条件；请只查询出售计数或沉默持续秒数。"
    return "commercial_sell_count" if commercial else "arcane_silence_seconds", None


def _multiple_scalar_requests(question):
    """Do not remove a second question merely because its words are allowed.

    A scalar allowlist can recognize every word in "出售多少干员？干员是什么？"
    while authorizing only the first request. Count interrogatives before any
    destructive word stripping, including independent requests without punctuation.
    """
    return len(re.findall(
        r"(?:是什么|什么时候|何时|什么|多少|多久|几|是否|能否|会不会|能不能|怎么|如何|吗)",
        question,
    )) > 1


def _metadata_request(question):
    """A phase mention alone must not turn an unknown rule into a time answer."""
    if not re.search(
        r"(?:阶段|期次|哪一期|哪期|时间|日期|开启|开始|開始|结束|截止|维护|开服|范围|映射|版本)",
        question,
    ):
        return False
    words = [
        "卫戍协议",
        "盟约",
        "下半期",
        "act2autochess",
        "2026年",
        "V2_1",
        "V2_2",
        "第二阶段",
        "第一阶段",
        "初始阶段",
        "第2阶段",
        "第1阶段",
        "二阶段",
        "一阶段",
        "2阶段",
        "1阶段",
        "客户端",
        "起始配置",
        "开启条件",
        "官方",
        "维护窗口",
        "维护",
        "窗口",
        "配置",
        "起始",
        "开启",
        "开始",
        "開始",
        "结束",
        "截止",
        "奖励",
        "持续",
        "时间范围",
        "活动时间",
        "活动",
        "时间",
        "日期",
        "阶段",
        "期次",
        "范围",
        "版本",
        "映射",
        "安排",
        "元信息",
        "实际",
        "开服",
        "能区分",
        "区分",
        "几点",
        "何时",
        "什么时候",
        "哪一期",
        "哪期",
        "哪天",
        "查询",
        "请问",
        "请",
        "查",
        "什么",
        "多少",
        "是",
        "的",
        "和",
        "与",
        "及",
        "到",
        "吗",
        "呢",
        "能",
    ]
    remainder = question
    for word in sorted(set(words), key=len, reverse=True):
        remainder = re.sub(re.escape(word), "", remainder, flags=re.I)
    remainder = re.sub(r"[\s，,。.!！?？：:、（）()【】\[\]\-_/]+", "", remainder)
    return not remainder


def _policy_cite(answer, record, selector, value, spec, pointer, official=False):
    """Require the compiled source pin, original pointer, and verified raw bytes."""
    citation_id = answer.cite(record, selector, value)
    citation = next(row for row in answer.result["citations"] if row["id"] == citation_id)
    snapshot = services.get_snapshot(answer.session, answer.release, citation["snapshot_id"])
    if official:
        article = spec["official_article_id"]
        expected = {
            "source_key": f"arknights-official:news/{article}",
            "source_type": "official_announcement",
            "revision": f"sha256:{spec['official_hash']}",
            "content_hash": spec["official_hash"],
            "url": f"https://ak.hypergryph.com/news/{article}",
            "media_type": "text/html",
        }
    else:
        expected = {
            "source_key": spec["source_key"],
            "source_type": "historical_client_data",
            "revision": spec["revision"],
            "content_hash": spec["snapshot_hash"],
            "url": spec["source_url"],
            "byte_size": HISTORICAL_SOURCES[spec["revision"]]["byte_size"],
            "media_type": "application/json",
        }
    if any(
        not services.same_json_value(snapshot.get(key), expected_value)
        for key, expected_value in expected.items()
    ):
        raise Conflict("garrison verified field source differs from audited immutable pin")
    if citation["evidence_pointer"] != pointer or citation["pointer"] != pointer + selector:
        raise Conflict("garrison verified field evidence has an unexpected identity pointer")
    return citation_id


def _verified_field_answer(answer, selected, field_key):
    coverage = answer.result["coverage"]
    spec_key = f"{selected}:{field_key}"
    spec = FIELD_SPECS.get(spec_key)
    if (
        spec is None
        or not coverage.get("verified_field_qa_enabled")
        or spec_key not in coverage.get("verified_rule_fields", [])
    ):
        return answer.refuse(
            "该发布版本未核验此阶段、状态与字段，不能借用其他阶段或普通/进阶配置。"
        )
    if answer.store is None:
        return answer.refuse("该字段需要重放原始快照核验；本次查询未提供快照存储，暂不回答。")
    records = _rows(answer.session, answer.release, "verified_rule_field")
    matches = [row for row in records if row["native_id"] == spec_key]
    if len(matches) != 1:
        raise Conflict("garrison verified field record is missing or ambiguous")
    record = matches[0]
    payload = record["data"]
    if (
        not services.same_json_value(payload.get("field_spec"), spec)
        or payload.get("mapping_basis") != MAPPING_BASIS
        or payload.get("verification_status") != VERIFICATION_STATUS
        or payload.get("field_value_hash") != services.content_digest(spec["value"])
        or payload.get("gameplay_application_verified") is not False
        or payload.get("namespace_phase_mapping") != "unverified"
    ):
        raise Conflict("garrison field projection differs from the bounded audit policy")
    citations = [
        _policy_cite(
            answer,
            record,
            spec["relative_selector"],
            spec["value"],
            spec,
            spec["evidence_pointer"],
        )
    ]
    buff_identity = (
        [("/0/key", "sell_char_count_gain_equip_owner_bond"), ("/0/blackboard/0/key", "count")]
        if field_key == "commercial_sell_count"
        else [
            ("/1/key", "env_gbuff_new_with_verify"),
            ("/1/blackboard/0/key", "key"),
            ("/1/blackboard/0/valueStr", "silence_attachment"),
            ("/1/blackboard/1/key", "silence"),
        ]
    )
    for selector, value in buff_identity:
        citations.append(
            _policy_cite(answer, record, selector, value, spec, spec["evidence_pointer"])
        )
    phase_expected = ANNOUNCEMENTS[spec["official_article_id"]]
    phase_stamp = int(datetime.fromisoformat(phase_expected["maintenance_start"]).timestamp())
    identity_specs = {
        "shop": (
            "historical_shop",
            spec["item_key"],
            NAMESPACE + f"/trapShopChessDatas/{spec['item_key']}",
            {
                "itemId": spec["item_key"],
                "goldenItemId": spec["golden_item_key"],
                "trapId": spec["char_id"],
            },
        ),
        "equipment": (
            "historical_equipment",
            spec["item_key"],
            NAMESPACE + f"/trapChessDataDict/{spec['item_key']}",
            {
                "chessId": spec["item_key"],
                "charId": spec["char_id"],
                "isGolden": False,
                "effectId": spec["effect_id"],
            },
        ),
        "effect": (
            "historical_effect",
            spec["effect_id"],
            NAMESPACE + f"/effectInfoDataDict/{spec['effect_id']}",
            {"effectId": spec["effect_id"], "effectName": spec["label"]},
        ),
        "phase": (
            "historical_phase",
            selected,
            f"/autoChessData/versionInfoDict/{selected}",
            {"activityId": ACTIVITY_ID, "seasonName": "盟约", "startTime": phase_stamp},
        ),
    }
    relations = payload.get("relation_record_ids", {})
    for relation, (kind, native_key, pointer, fields) in identity_specs.items():
        expected_id = f"{spec['revision']}:{relation}:{native_key}"
        if relations.get(relation) != expected_id:
            raise Conflict("garrison field identity chain references another source or phase")
        matches = [
            row
            for row in _rows(answer.session, answer.release, kind)
            if row["native_id"] == expected_id
        ]
        if len(matches) != 1:
            raise Conflict("garrison verified field identity record is missing or ambiguous")
        for field, value in fields.items():
            citations.append(_policy_cite(answer, matches[0], f"/{field}", value, spec, pointer))
    announcement_matches = [
        row
        for row in _rows(answer.session, answer.release, "official_phase_announcement")
        if row["native_id"] == spec["official_article_id"]
    ]
    if len(announcement_matches) != 1:
        raise Conflict("garrison field has no unique official announcement evidence")
    announcement = announcement_matches[0]
    if (
        announcement["data"].get("phase_id") != selected
        or announcement["data"].get("activity_id") != ACTIVITY_ID
    ):
        raise Conflict("garrison field official announcement belongs to another phase")
    for selector, value in (
        (spec["official_selector"], spec["official_quote"]),
        (announcement["data"]["schedule_selector"], phase_expected["lead"]),
        (announcement["data"]["publication_date_selector"], phase_expected["publication_date"]),
    ):
        citations.append(
            _policy_cite(answer, announcement, selector, value, spec, "", official=True)
        )
    answer.claim(
        f"{selected} {spec['label']}普通状态{'出售计数' if field_key == 'commercial_sell_count' else '失去特殊能力持续秒数'}",
        spec["value"],
        citations,
        VERIFICATION_STATUS,
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
    )
    text = (
        f"{selected}，{spec['label']}普通（初始）状态：出售干员计数阈值为 {spec['value']:g} 名干员。"
        if field_key == "commercial_sell_count"
        else f"{selected}，奥术法阵普通（初始）状态：失去特殊能力的配置持续时间为 {spec['value']:g} 秒。"
    )
    answer.result["answer"] = (
        text + "\n范围：act2autochess 盟约下半期。字段映射由历史修订与官方变更交叉佐证；"
        "完整规则命名空间归期及实际引擎生效条件仍未核验。"
        + f"\n版本：{answer.release.id}；历史配置修订：{spec['revision']}。"
        + f"\n来源：[固定历史配置]({spec['source_url']})；"
        f"[官方公告 {spec['official_article_id']}](https://ak.hypergryph.com/news/{spec['official_article_id']})。"
        + "\n证据："
        + "、".join(f"[{item}]" for item in dict.fromkeys(citations))
    )
    answer.result["tool_trace"].append(
        {
            "tool": "garrison_verified_field_gate",
            "field_spec_key": spec_key,
            "phase_id": selected,
            "source_revision": spec["revision"],
            "mapping_basis": MAPPING_BASIS,
            "namespace_phase_mapping": "unverified",
            "snapshot_verification": "sha256_and_document_pointer",
            "identity_chain": "shop_to_normal_equipment_to_effect_to_field",
            "rules_engine_enabled": False,
        }
    )
    return answer.result


def answer_garrison(
    session,
    release,
    question: str,
    activity_id: str | None = None,
    phase_id: str | None = None,
    store=None,
) -> dict:
    """Answer only metadata and field pins checked in this immutable release.

    No active release resolution, network request or rule calculation occurs here.
    """
    if not isinstance(question, str) or not question.strip() or len(question) > 2000:
        raise InvalidData("question must contain 1 to 2000 characters")
    answer = _Answer(session, release, question.strip(), store)
    coverage = answer.result["coverage"]
    answer.result["tool_trace"].append(
        {
            "tool": "garrison_phase_gate",
            "release_id": release.id,
            "activity_id": activity_id,
            "phase_id": phase_id,
            "mapping_status": coverage.get("mapping_status"),
            "rules_qa_enabled": coverage.get("rules_qa_enabled", False),
        }
    )
    if (
        activity_id not in (None, ACTIVITY_ID)
        or "上半期" in question
        or "act1autochess" in question
    ):
        return answer.refuse(
            "此发布版本仅核验了 act2autochess 盟约下半期的阶段元信息。", "out_of_scope"
        )
    other_activities = re.findall(r"act\d+autochess", question)
    years = re.findall(r"(20\d{2})年", question)
    if any(ident != ACTIVITY_ID for ident in other_activities) or any(
        year != "2026" for year in years
    ):
        return answer.refuse("该问题的活动或年份不在本发布版本的核验范围内。", "out_of_scope")
    version_keys = re.findall(r"V\d+_\d+", question, re.I)
    if any(key.upper() not in ("V2_1", "V2_2") for key in version_keys):
        return answer.refuse(
            "此发布版本仅核验 act2autochess 的 V2_1 和 V2_2，不能合并其他期次。", "out_of_scope"
        )
    if _CURRENT_WORDS.search(question):
        return answer.refuse(
            "该来源保留历史活动配置，未确认当前有效活动，不能据此回答当前期次或规则。"
        )
    if (
        not coverage.get("metadata_qa_enabled")
        or coverage.get("mapping_status") != "partial_verified"
    ):
        return answer.refuse("此发布版本尚未核验活动期次与规则映射，卫戍问答暂未开放。")
    selected, error = _selected_phase(question, phase_id)
    if error:
        return answer.refuse(error, "needs_clarification")
    from arknights_agent.garrison_mechanics import answer_garrison_mechanics

    mechanics_result = answer_garrison_mechanics(answer, selected, question)
    if mechanics_result is not None:
        return mechanics_result

    from arknights_agent.garrison_expansion import answer_garrison_expansion

    expansion_result = answer_garrison_expansion(answer, selected, question)
    if expansion_result is not None:
        return expansion_result
    field_key, field_error = _rule_field_request(question)
    if field_error:
        return answer.refuse(field_error)
    if field_key:
        if selected is None:
            return answer.refuse(
                "出售计数与沉默参数必须限定阶段；请指定 V2_1 或 V2_2。",
                "needs_clarification",
            )
        return _verified_field_answer(answer, selected, field_key)
    if _RULE_WORDS.search(question):
        return answer.refuse(
            "已核验 act2autochess 的 V2_1/V2_2 起始配置与官方维护安排；"
            "本发布版本没有该问题的完整字段、状态或运行条件映射证据。"
            "已核验的个别字段不代表完整规则已归期，不能扩大白名单，其他规则继续拒答。"
        )
    if not _metadata_request(question):
        return answer.refuse(
            "问题未限定为已核验的时间、维护安排或字段，不能用阶段元信息代替未核验规则。"
            "请明确所需元信息或已核验字段。",
            "needs_verification" if selected else "needs_clarification",
        )
    phases = {row["native_id"]: row for row in _rows(session, release, "verified_phase")}
    announcements = {
        row["native_id"]: row for row in _rows(session, release, "official_phase_announcement")
    }
    if set(phases) != set(coverage.get("verified_phase_ids", [])):
        raise Conflict("garrison verified phase coverage/records differ")
    if selected and selected not in phases:
        return answer.refuse("指定阶段没有本发布版本内的核验记录，不能使用其他阶段代替。")
    selected_rows = [phases[selected]] if selected in phases else list(phases.values())
    if not selected_rows:
        return answer.refuse("此发布版本没有可引用的核验期次记录。")
    lines = ["范围：act2autochess，卫戍协议：盟约下半期活动。"]
    if any(word in question for word in ("结束", "截止", "持续", "时间范围", "活动时间")):
        activities = _rows(session, release, "verified_activity")
        if len(activities) != 1 or activities[0]["native_id"] != ACTIVITY_ID:
            raise Conflict("garrison verified activity record is missing/ambiguous")
        activity = activities[0]
        for label, field, source_field in (
            ("活动配置开始", "client_start_time", "startTime"),
            ("活动配置结束", "client_end_time", "endTime"),
            ("奖励配置截止", "client_reward_end_time", "rewardEndTime"),
        ):
            iso = activity["data"][field]
            stamp = int(datetime.fromisoformat(iso).timestamp())
            citation = answer.cite(activity, f"/{source_field}", stamp)
            answer.claim(
                label,
                iso,
                [citation],
                "retained_client_configuration",
                {
                    "operation": "unix_timestamp_to_iso8601",
                    "timezone": "Asia/Shanghai",
                },
            )
            lines.append(f"{label}：{iso}（保留客户端配置）。")
    for phase in sorted(selected_rows, key=lambda row: row["native_id"]):
        article = phase["data"].get("official_announcement_record")
        if article not in announcements:
            raise Conflict("garrison phase has no official announcement record")
        lines.append(_phase_claims(answer, phase, announcements[article]))
    if not answer.result["claims"] or not answer.result["citations"]:
        raise Conflict("garrison factual answer must have claims and source citations")
    answer.result["answer"] = "\n".join(lines)
    answer.result["answer"] += (
        f"\n\n版本：{release.id}；来源修订：{answer.result['version']['source_revision']}。\n证据："
        + "、".join(f"[{citation['id']}]" for citation in answer.result["citations"])
    )
    answer.result["tool_trace"].append(
        {
            "tool": "verify_garrison_field_evidence",
            "release_id": release.id,
            "phase_id": selected,
            "citation_count": len(answer.result["citations"]),
            "snapshot_verification": "sha256_and_document_pointer"
            if store
            else "sealed_excerpt_and_field",
        }
    )
    return answer.result
