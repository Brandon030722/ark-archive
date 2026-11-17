"""Candidate independent oracle for six documented garrison bundles.

Inputs are references already byte-replayed by SourceReplay, not model quotes.
Only source-identity metadata is read from the sealed primary record. Its
field_spec.value/value_hash, compiled policy and production answer builder are
never used. All expected values are reconstructed before comparing a claim.
This is a description/configuration oracle, not a gameplay probability engine.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

FIELDS = frozenset({
    "miracle_activation_description", "miracle_refresh_target_description",
    "miracle_documented_probability_formula", "miracle_funding_description",
    "investor_activation_description", "investor_trigger_description",
})


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _same(left, right):
    return _json(left) == _json(right)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


class InvalidProof(ValueError):
    pass


class UnknownProof(ValueError):
    pass


@dataclass
class BundleProof:
    expected_value: dict | None = None
    field_support: dict = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)
    unverified: list[dict] = field(default_factory=list)
    scope: dict = field(default_factory=dict)

    @property
    def passed(self):
        return False if self.errors else None if self.unverified or self.expected_value is None else True

    def compare(self, actual_value):
        """Keep the independent source expectation separate from the prediction."""
        errors = list(self.errors)
        if self.passed is True and not _same(actual_value, self.expected_value):
            errors.append({"code": "garrison_bundle_value_mismatch"})
        passed = False if errors else self.passed
        return {"passed": passed, "status": "failed" if errors else "passed" if passed else "unverified",
                "errors": errors, "unverified": self.unverified,
                "expected_value": self.expected_value, "field_support": self.field_support,
                "scope": self.scope}


def scope_metadata(primary_record):
    """Whitelist identity/pin fields only; never copy a fact value or checks."""
    spec = primary_record["data"]["field_spec"]
    keys = ("phase_id", "field_key", "activity_id", "bond_id", "effect_id", "revision",
            "snapshot_hash", "official_article_id", "official_hash")
    result = {key: spec[key] for key in keys}
    result["wiki_revisions"] = sorted(str(row["revision"]) for row in spec["wiki_evidence"])
    return result


def _anchor(ref, selector=""):
    return {"snapshot_hash": ref["snapshot"]["content_hash"], "source_revision": ref["snapshot"]["revision"],
            "pointer": ref["pointer"] + selector}


def _original_reference(ref, dataset_id, catalog_snapshots):
    """Recheck the verified-reference shape and its complete excerpt digest.

    Actual raw bytes/SQL ownership must already be checked by SourceReplay. This
    additional check rejects a changed value in an otherwise cached reference.
    """
    record, span, snapshot = ref["record"], ref["evidence"], ref["snapshot"]
    if record["module"] != "garrison" or record["dataset_id"] != dataset_id or span["dataset_id"] != dataset_id:
        raise InvalidProof("reference belongs to another module/dataset")
    pin = catalog_snapshots.get(snapshot["id"])
    if pin is None or any(pin.get(key) != snapshot[actual] for key, actual in (
        ("snapshot_hash", "content_hash"), ("revision", "revision"), ("url", "url"), ("media_type", "media_type"),
    )):
        raise InvalidProof("reference source differs from the fixed source catalog")
    if span["snapshot_id"] != snapshot["id"] or _digest(span["excerpt"]) != span["excerpt_hash"]:
        raise InvalidProof("reference evidence ownership/hash differs")
    base, pointer = span["pointer"], ref["pointer"]
    if pointer != base and not pointer.startswith(base + "/"):
        raise InvalidProof("reference field escapes its source excerpt")
    raw = span["excerpt"]
    relative = pointer[len(base):]
    for token in relative[1:].split("/") if relative else []:
        if re.search(r"~(?![01])", token):
            raise InvalidProof("invalid source pointer escape")
        key = token.replace("~1", "/").replace("~0", "~")
        raw = raw[int(key)] if isinstance(raw, list) else raw[key]
    if not _same(raw, ref["value"]):
        raise InvalidProof("reference value differs from its verified original excerpt")


def _one(refs, predicate, label):
    found = [ref for ref in refs if predicate(ref)]
    # Repeated references to the same raw field are allowed, but not competing sources.
    by_identity = {(r["snapshot"]["id"], r["pointer"]): r for r in found}
    if len(by_identity) != 1:
        raise InvalidProof(f"{label} does not have one complete original source")
    return next(iter(by_identity.values()))


def _plain_client(text):
    if not isinstance(text, str):
        raise UnknownProof("client description is not text")
    return html.unescape(re.sub(r"<(?:[@$][A-Za-z0-9_.]+|/)>", "", text)).replace("\\n", "\n")


def _wiki_section(text, name):
    if not isinstance(text, str):
        raise UnknownProof("wiki excerpt is not text")
    marker = "{{/盟约|盟约=" + name + "|"
    if text.count(marker) != 1:
        raise UnknownProof("named wiki bond section absent or ambiguous")
    start = text.index(marker)
    end = text.find("|干员=", start)
    if end < 0:
        raise UnknownProof("named wiki section boundary absent")
    section = text[start:end]
    # Only unwrap the simple color template used in these retained descriptions.
    for _ in range(8):
        changed = re.sub(r"\{\{color\|[^{}|]+\|([^{}]*)\}\}", r"\1", section)
        if changed == section:
            break
        section = changed
    return html.unescape(re.sub(r"<br\s*/?>", "\n", section))


def _number(value):
    if type(value) not in (int, float):
        raise InvalidProof("configuration parameter is not a JSON number")
    number = Decimal(str(value))
    if not number.is_finite():
        raise InvalidProof("configuration parameter is non-finite")
    return number


def _numeric_text(value):
    return format((value if isinstance(value, Decimal) else _number(value)).normalize(), "f")


def _blackboards(buff_list):
    if not isinstance(buff_list, list):
        raise InvalidProof("client buff configuration is not a list")
    groups = {}
    for buff in buff_list:
        keys = {}
        for entry in buff["blackboard"]:
            if entry["key"] in keys:
                raise InvalidProof("duplicate named blackboard key")
            keys[entry["key"]] = entry
        groups.setdefault(buff["key"], []).append(keys)
    return groups


def _official_start(refs, official_hash, article_id):
    eligible = [r for r in refs if r["snapshot"]["content_hash"] == official_hash
                and r["snapshot"]["media_type"] == "text/html"
                and r["record"]["native_id"] == article_id]
    leads = []
    for ref in eligible:
        text = ref["value"]
        if not isinstance(text, str) or "「卫戍协议：盟约」" not in text:
            continue
        match = re.search(r"(?:将于|将在)(\d{2})月(\d{2})日(\d{2}):(\d{2})\s*~\s*\d{2}:\d{2}的(?:停机维护|闪断更新)后开启", text)
        if match:
            leads.append((ref, match.groups()))
    if len(leads) != 1:
        raise InvalidProof("official phase announcement start is absent or ambiguous")
    lead, (month, day, hour, minute) = leads[0]
    dates = [re.fullmatch(r"(\d{4}) // (\d{2}) / (\d{2})", r["value"])
             for r in eligible if isinstance(r["value"], str)
             and r["record"]["id"] == lead["record"]["id"]]
    dates = [m for m in dates if m and (m[2], m[3]) == (month, day)]
    if len(dates) != 1:
        raise InvalidProof("official schedule lacks the same record's publication year/date")
    stamp = datetime(int(dates[0][1]), int(month), int(day), int(hour), int(minute), tzinfo=ZoneInfo("Asia/Shanghai"))
    return int(stamp.timestamp())


def derive_documented_bundle(refs, *, phase_id, field_key, source_catalog):
    """Derive a bundle solely from original fields and original descriptions.

    The caller supplies only the claim's already verified references. A missing
    or swapped proof is rejected; unfamiliar source grammar is unverified. The
    actual claimed value is intentionally not an argument to this function.
    """
    proof = BundleProof(scope={"phase_id": phase_id, "field_key": field_key})
    try:
        if phase_id not in {"V2_1", "V2_2"} or field_key not in FIELDS:
            raise InvalidProof("unsupported phase/field identity")
        if not isinstance(refs, list) or not refs:
            raise InvalidProof("bundle has no verified original references")
        dataset_id = source_catalog["source_pin"]["dataset_pins"]["garrison"]["dataset_id"]
        snapshots = {r["snapshot_id"]: r for r in source_catalog["snapshots"]}
        for ref in refs:
            _original_reference(ref, dataset_id, snapshots)
        native = f"{phase_id}:{field_key}"
        catalog_records = [r for r in source_catalog["records"] if r["module"] == "garrison"
                           and r["kind"] == "documented_mechanics_field" and r["native_id"] == native]
        if len(catalog_records) != 1:
            raise InvalidProof("requested field has no unique sealed catalog identity")
        primary = catalog_records[0]
        primary_ref = _one(refs, lambda r: r["record"]["id"] == primary["record_id"], "primary documented field")
        metadata = scope_metadata(primary_ref["record"])
        if metadata["phase_id"] != phase_id or metadata["field_key"] != field_key \
                or metadata["snapshot_hash"] != primary["evidence"]["snapshot_hash"] \
                or metadata["revision"] != primary["evidence"]["source_revision"]:
            raise InvalidProof("primary phase/field source pin differs from sealed catalog")
        prefix = f"/activity/AUTOCHESS_SEASON/{metadata['activity_id']}"
        bond_pointer = prefix + "/bondInfoDict/" + metadata["bond_id"]
        if primary["evidence"]["pointer"] != bond_pointer:
            raise InvalidProof("primary source pointer does not bind its named bond")
        client_hash = metadata["snapshot_hash"]
        client = [r for r in refs if r["snapshot"]["content_hash"] == client_hash
                  and r["snapshot"]["revision"] == metadata["revision"]]
        bond_ref = _one(client, lambda r: r["pointer"] == bond_pointer, "complete client bond")
        bond = bond_ref["value"]
        if bond["bondId"] != metadata["bond_id"] or bond["effectId"] != metadata["effect_id"]:
            raise InvalidProof("client bond/effect source identity differs")
        effect_ref = _one(client, lambda r: r["pointer"] == prefix + "/effectInfoDataDict/" + bond["effectId"], "complete client effect")
        if effect_ref["value"]["effectId"] != bond["effectId"]:
            raise InvalidProof("client effect does not belong to the named bond")
        buffs_ref = _one(client, lambda r: r["pointer"] == prefix + "/effectBuffInfoDataDict/" + bond["effectId"], "complete client buff list")
        phase_prefix = f"/autoChessData/versionInfoDict/{phase_id}"
        phase_start = _one(client, lambda r: r["pointer"] == phase_prefix + "/startTime", "phase start")
        phase_activity = _one(client, lambda r: r["pointer"] == phase_prefix + "/activityId", "phase activity")
        if phase_activity["value"] != metadata["activity_id"] \
                or not _same(phase_start["value"], _official_start(refs, metadata["official_hash"], metadata["official_article_id"])):
            raise InvalidProof("client phase/activity does not match its pinned official schedule")
        wiki_refs = [r for r in refs if str(r["snapshot"]["revision"]) in metadata["wiki_revisions"]
                     and "prts.wiki/" in r["snapshot"]["url"]]
        if {str(r["snapshot"]["revision"]) for r in wiki_refs} != set(metadata["wiki_revisions"]):
            raise InvalidProof("bundle omits its phase-specific fixed wiki source")
        wiki_ref = _one(wiki_refs, lambda r: isinstance(r["value"], str), "fixed named wiki description")
        section = _wiki_section(wiki_ref["value"], bond["name"])
        plain = _plain_client(bond["desc"])
        groups = _blackboards(buffs_ref["value"])
        support = {}

        def emit(key, value, kind, *anchors):
            support[key] = {"proof_kind": kind, "anchors": [_anchor(r, selector) for r, selector in anchors]}
            return value

        if field_key.endswith("activation_description"):
            count = bond["activeCount"]
            wiki_count = re.search(r"\|人数=(\d+)\|", section)
            if type(count) is not int or not wiki_count or int(wiki_count[1]) != count \
                    or bond["activeParamList"] != [str(count)] \
                    or bond["activeConditionTemplate"] != "count_threshold_upward":
                raise UnknownProof("activation count/scope configuration and named description do not agree")
            phrase = f"整备区的【{bond['name']}】干员也可用于激活盟约"
            if phrase not in plain or phrase not in section:
                raise UnknownProof("preparation-area inclusion lacks explicit matching original text")
            value = {
                "required_count": emit("required_count", count, "raw_scalar_and_named_description", (bond_ref, "/activeCount"), (wiki_ref, "")),
                "configured_scope": emit("configured_scope", bond["activeCondition"], "raw_scalar", (bond_ref, "/activeCondition")),
                "preparation_area_included_in_description": emit("preparation_area_included_in_description", True, "literal_source_statement", (bond_ref, "/desc"), (wiki_ref, "")),
            }
        elif field_key == "miracle_refresh_target_description":
            match = re.search(r"(刷新调度中心时)，有概率使(下次刷新不消耗资金)", plain)
            if not match or not all(part in section for part in match.groups()):
                raise UnknownProof("refresh trigger/target lack matching original phrases")
            value = {key: emit(key, text, "literal_source_substring", (bond_ref, "/desc"), (wiki_ref, ""))
                     for key, text in zip(("trigger_text", "target_text"), match.groups(), strict=True)}
        elif field_key == "miracle_funding_description":
            group = groups.get("bond_layer_gain_coin", [])
            if len(group) != 1:
                raise InvalidProof("funding source does not identify one named parameter group")
            match = re.search(r"<(每叠加([0-9.]+)层)>获得([0-9.]+)资金", plain)
            wiki_match = re.search(r"<(每叠加([0-9.]+)层)>获得([0-9.]+)资金", section)
            layer, amount = group[0]["layer"]["value"], group[0]["count"]["value"]
            if not match or not wiki_match or match.groups() != wiki_match.groups() \
                    or _number(layer) != Decimal(match[2]) or _number(amount) != Decimal(match[3]):
                raise UnknownProof("funding named parameters and original descriptions do not agree")
            value = {
                "layer_interval": emit("layer_interval", layer, "named_raw_parameter_and_description", (buffs_ref, ""), (bond_ref, "/desc"), (wiki_ref, "")),
                "fund_amount": emit("fund_amount", amount, "named_raw_parameter_and_description", (buffs_ref, ""), (bond_ref, "/desc"), (wiki_ref, "")),
                "trigger_text": emit("trigger_text", match[1], "literal_source_substring", (bond_ref, "/desc"), (wiki_ref, "")),
            }
        elif field_key == "investor_trigger_description":
            rows = groups.get("bond_layer_char_garrison_bonus", [])
            base = [r for r in rows if _number(r["layer"]["value"]) == 0]
            upgrade = [r for r in rows if _number(r["layer"]["value"]) > 0]
            if len(base) != 1 or len(upgrade) != 1:
                raise InvalidProof("investor trigger parameter groups are absent or ambiguous")
            base, upgrade = base[0], upgrade[0]
            event = base["event"]["valueStr"]
            if not isinstance(event, str) or event != upgrade["event"]["valueStr"]:
                raise UnknownProof("investor event configurations differ")
            expression = r"每次触发([0-9.]+)次\s*\n<(达到|高于|超过)([0-9.]+)层>“获得时”类特质每次触发([0-9.]+)次"
            original = re.search(expression, plain)
            wiki = re.search(expression, section)
            if not original or not wiki or original.groups() != wiki.groups():
                raise UnknownProof("investor original trigger/threshold wording does not agree")
            if tuple(map(Decimal, (original[1], original[3], original[4]))) != tuple(map(_number, (
                base["count"]["value"], upgrade["layer"]["value"], upgrade["count"]["value"],
            ))):
                raise UnknownProof("investor original description differs from named parameters")
            value = {
                "event_config": emit("event_config", event, "named_raw_parameter", (buffs_ref, "")),
                "base_trigger_count": emit("base_trigger_count", base["count"]["value"], "named_raw_parameter_and_description", (buffs_ref, ""), (bond_ref, "/desc")),
                "triple_trigger_layer": emit("triple_trigger_layer", upgrade["layer"]["value"], "named_raw_parameter_and_description", (buffs_ref, ""), (bond_ref, "/desc")),
                "triple_trigger_count": emit("triple_trigger_count", upgrade["count"]["value"], "named_raw_parameter_and_description", (buffs_ref, ""), (bond_ref, "/desc")),
                "threshold_text": emit("threshold_text", original[2], "literal_source_substring", (bond_ref, "/desc"), (wiki_ref, "")),
            }
        else:
            rows = groups.get("bond_refresh_shop_next_free", [])
            if len(rows) != 1:
                raise InvalidProof("miracle formula named parameter group absent or ambiguous")
            row = rows[0]
            formulas = re.findall(r"\(([0-9.]+)\s*\+\s*([0-9.]+)\s*×\s*层数\)\s*%", section)
            if len(formulas) != 1:
                raise UnknownProof("wiki does not state one recognized documented expression")
            first, second = map(Decimal, formulas[0])
            base, per_layer, coefficient = (row[key]["value"] for key in ("baseprob", "prob", "probk"))
            if _number(base) * 100 != first or _number(per_layer) * 100 != second or _number(coefficient) != second:
                raise UnknownProof("documented expression and named client parameters do not agree")
            expression = f"({_numeric_text(first)} + {_numeric_text(second)} × L)%"
            value = {
                "expression": emit("expression", expression, "original_document_expression_display_normalization", (wiki_ref, "")),
                "layer_symbol": emit("layer_symbol", "L", "document_variable_display_alias_for_层数", (wiki_ref, "")),
                "base_probability": emit("base_probability", base, "named_raw_parameter", (buffs_ref, "")),
                "per_layer_probability_parameter": emit("per_layer_probability_parameter", per_layer, "named_raw_parameter", (buffs_ref, "")),
                "percentage_coefficient_parameter": emit("percentage_coefficient_parameter", coefficient, "named_raw_parameter", (buffs_ref, "")),
                "runtime_probability_verified": emit("runtime_probability_verified", False, "scope_boundary_not_a_runtime_probability_calculation", (wiki_ref, "")),
            }
        if set(value) != set(support):
            raise InvalidProof("a derived field lacks its explicit independent proof")
        proof.expected_value, proof.field_support = value, support
        proof.scope.update(client_snapshot_hash=client_hash, client_revision=metadata["revision"],
                           wiki_revisions=metadata["wiki_revisions"], official_snapshot_hash=metadata["official_hash"],
                           runtime_calculation=False, compiled_policy_values_read=False)
    except UnknownProof as exc:
        proof.unverified.append({"code": "garrison_bundle_source_interpretation_unverified", "detail": str(exc)})
    except (InvalidProof, KeyError, IndexError, TypeError, ValueError, ArithmeticError) as exc:
        proof.errors.append({"code": "garrison_bundle_source_proof_invalid", "detail": str(exc)})
    return proof
