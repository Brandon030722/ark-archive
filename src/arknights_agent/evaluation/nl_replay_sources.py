"""Independent, offline source checks for the development-only NL replay.

This module never invokes an answer builder, provider, HTTP client or budget
ledger.  It verifies declarations against a pinned read-only database and raw
snapshots; that is deliberately narrower than judging natural-language intent.
Unimplemented derivations are reported as ``unverified``, never accepted because
their citations happen to be valid.  A nonfactual refusal without a strict
expectation is not an answered test.
"""

from __future__ import annotations

import copy
import hashlib
import html
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from itertools import combinations
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select

from arknights_agent import services
from arknights_agent.errors import Conflict, InvalidData, NotFound
from arknights_agent.evaluation.nl_replay_garrison_bundle import derive_documented_bundle
from arknights_agent.models import EvidenceSpan, KnowledgeRecord, RecordEvidence, SourceSnapshot


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _same(left, right):
    # Python's True == 1 and 1 == 1.0 cannot certify quoted JSON fields.
    return _json(left) == _json(right)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _pointer(document, pointer):
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise InvalidData("source selector is not an RFC6901 pointer")
    if re.search(r"~(?![01])", pointer):
        raise InvalidData("source selector contains an invalid RFC6901 escape")
    return services.json_pointer(document, pointer)


@dataclass
class SourceAudit:
    """Three-valued result: false means proved failure, None means unverified."""

    errors: list[dict] = field(default_factory=list)
    unverified: list[dict] = field(default_factory=list)
    checks: Counter = field(default_factory=Counter)
    applicable: bool = False

    @property
    def passed(self):
        if self.errors:
            return False
        if self.unverified or not self.applicable:
            return None
        return True

    @property
    def status(self):
        if self.errors:
            return "failed"
        return "unverified" if self.unverified else "passed" if self.applicable else "not_applicable"

    def summary(self):
        return {
            "status": self.status, "passed": self.passed,
            "checks": dict(self.checks), "errors": self.errors,
            "unverified": self.unverified,
        }


class SourceReplay:
    """Source verifier scoped to one immutable release/read-only transaction.

    ``source_catalog`` accepts an already parsed JSON object or an explicitly
    named source-catalog*.json file.  The caller owns ``readonly_session`` and
    must discard this instance when changing a transaction or injecting faults.
    Cached DB identities are verified once in that fixed transaction. Raw files
    are rehashed when their filesystem identity changes; ``recheck_snapshots``
    performs a final unconditional byte check. ``cache_metrics`` makes that
    distinction visible rather than counting cache hits as new source replays.
    """

    def __init__(self, session, release, store, source_catalog):
        if isinstance(source_catalog, (str, Path)):
            path = Path(source_catalog)
            for candidate in (path, path.resolve()):
                if any(part.lower() in {"holdout", "heldout", "sealed"} for part in candidate.parts):
                    raise InvalidData("sealed questions are not source catalog inputs")
            if any(not re.fullmatch(r"source-catalog[^/]*\.json", item.name)
                   for item in (path, path.resolve())):
                raise InvalidData("expected an explicitly named source catalog")
            source_catalog = json.loads(path.read_text())
        if not isinstance(source_catalog, dict):
            raise InvalidData("source catalog must be a JSON object")
        self.session, self.release, self.store = session, release, store
        self.catalog = copy.deepcopy(source_catalog)
        self.catalog_records = {r["record_id"]: r for r in self.catalog.get("records", [])}
        if len(self.catalog_records) != len(self.catalog.get("records", [])):
            raise InvalidData("source catalog has duplicate record IDs")
        self.catalog_snapshots = {s["snapshot_id"]: s for s in self.catalog.get("snapshots", [])}
        self._records, self._evidence, self._snapshots = {}, {}, {}
        self._links, self._documents, self._record_spans = {}, {}, {}
        self._module_rows = {}
        self.cache_metrics = Counter()
        self._check_pin(self.catalog.get("source_pin"))
        self._manifest = copy.deepcopy(self.release.manifest)
        self._release_identity = (self.release.id, self.release.manifest_hash)

    def _check_pin(self, pin):
        if _hash(self.release.manifest) != self.release.manifest_hash:
            raise Conflict("fixed release manifest has changed")
        if not isinstance(pin, dict) or pin.get("release_id") != self.release.id:
            raise Conflict("source pin belongs to another release")
        if pin.get("manifest_hash") != self.release.manifest_hash:
            raise Conflict("source pin manifest hash differs")
        for module, wanted in pin.get("dataset_pins", {}).items():
            actual = self.release.manifest["datasets"].get(module)
            if actual is None or any(not _same(wanted.get(key), actual.get(key))
                                     for key in ("dataset_id", "source_revision", "version")):
                raise Conflict("source pin dataset identity differs")
        if set(pin.get("dataset_pins", {})) != set(self.release.manifest["datasets"]):
            raise Conflict("source pin omits a release dataset")

    def _entry(self, module):
        # Full manifest digest is checked once at each public audit boundary;
        # repeated field reads use a private copy in this same fixed transaction.
        if (self.release.id, self.release.manifest_hash) != self._release_identity:
            raise Conflict("fixed release identity has changed")
        try:
            return self._manifest["datasets"][module]
        except KeyError as exc:
            raise NotFound("module absent from fixed release") from exc

    def _record(self, ident, module):
        entry = self._entry(module)
        if ident not in self._records:
            row = self.session.scalar(select(KnowledgeRecord).where(
                KnowledgeRecord.id == ident, KnowledgeRecord.dataset_id == entry["dataset_id"],
                KnowledgeRecord.module == module,
            ))
            if row is None:
                raise NotFound("record outside fixed module/release")
            self._records[ident] = {
                "id": row.id, "dataset_id": row.dataset_id, "module": row.module,
                "kind": row.kind, "native_id": row.native_id, "name": row.name,
                "parent_native_id": row.parent_native_id, "data": copy.deepcopy(row.payload),
            }
            self.cache_metrics["record_reads"] += 1
        else:
            self.cache_metrics["record_cache_hits"] += 1
        record = self._records[ident]
        if record["dataset_id"] != entry["dataset_id"] or record["module"] != module:
            raise Conflict("cached record is outside requested module")
        return record

    def _span(self, ident, module):
        entry = self._entry(module)
        if ident not in self._evidence:
            row = self.session.scalar(select(EvidenceSpan).where(
                EvidenceSpan.id == ident, EvidenceSpan.dataset_id == entry["dataset_id"],
            ))
            if row is None:
                raise NotFound("evidence outside fixed module/release")
            self._evidence[ident] = {
                "id": row.id, "dataset_id": row.dataset_id, "snapshot_id": row.snapshot_id,
                "pointer": row.pointer, "excerpt": copy.deepcopy(row.excerpt),
                "excerpt_hash": row.excerpt_hash,
            }
            self.cache_metrics["evidence_reads"] += 1
        else:
            self.cache_metrics["evidence_cache_hits"] += 1
        span = self._evidence[ident]
        if span["dataset_id"] != entry["dataset_id"]:
            raise Conflict("cached evidence is outside requested module")
        return span

    def _linked(self, record_id, evidence_id, module):
        key = (record_id, evidence_id, module)
        if key not in self._links:
            self._links[key] = self.session.scalar(select(RecordEvidence.record_id).where(
                RecordEvidence.record_id == record_id, RecordEvidence.evidence_id == evidence_id,
                RecordEvidence.dataset_id == self._entry(module)["dataset_id"],
            )) is not None
            self.cache_metrics["link_reads"] += 1
        if not self._links[key]:
            raise Conflict("record is not linked to this evidence in the fixed dataset")

    def _snapshot(self, ident, module):
        if ident not in self._entry(module).get("source_snapshot_ids", []):
            raise Conflict("snapshot outside fixed module/release inventory")
        if ident not in self._snapshots:
            row = self.session.get(SourceSnapshot, ident)
            if row is None:
                raise NotFound("pinned snapshot missing")
            self._snapshots[ident] = {
                "id": row.id, "content_hash": row.content_hash, "byte_size": row.byte_size,
                "media_type": row.media_type, "source_key": row.source_key,
                "url": row.url, "revision": row.revision,
            }
            self.cache_metrics["snapshot_reads"] += 1
        return self._snapshots[ident]

    def _document(self, snapshot, *, force=False):
        digest = snapshot["content_hash"]
        path = self.store.path_for(digest)
        stat = path.stat()
        stamp = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        cached = self._documents.get(digest)
        if force or cached is None or cached[0] != stamp:
            raw = self.store.read_verified(digest)
            if len(raw) != snapshot["byte_size"]:
                raise Conflict("snapshot byte size differs from fixed source")
            document = services.snapshot_document(raw, snapshot["media_type"])
            self._documents[digest] = (stamp, document)
            self.cache_metrics["raw_snapshot_reads"] += 1
            self.cache_metrics["verified_bytes"] += len(raw)
        else:
            self.cache_metrics["document_cache_hits"] += 1
        return self._documents[digest][1]

    def recheck_snapshots(self):
        """Unconditionally rehash every raw object read by this instance."""
        result = SourceAudit(applicable=True)
        for snapshot in self._snapshots.values():
            try:
                self._document(snapshot, force=True)
            except (OSError, Conflict, InvalidData) as exc:
                result.errors.append({"code": "snapshot_recheck_failed", "detail": str(exc)})
            else:
                result.checks["raw_snapshot_rechecked"] += 1
        return result

    def _verify_citation(self, citation, module):
        if not isinstance(citation, dict):
            raise InvalidData("citation must be an object")
        digest = citation.get("snapshot_hash", citation.get("content_hash"))
        if "snapshot_hash" in citation and "content_hash" in citation and digest != citation["content_hash"]:
            raise Conflict("contradictory snapshot hashes")
        snapshot = self._snapshot(citation.get("snapshot_id"), module)
        if digest != snapshot["content_hash"] or citation.get("source_url") != snapshot["url"] \
                or citation.get("source_revision") != snapshot["revision"]:
            raise Conflict("citation source identity differs from fixed snapshot")
        if citation.get("source_key", snapshot["source_key"]) != snapshot["source_key"]:
            raise Conflict("citation source key differs")
        if citation.get("source_media_type", snapshot["media_type"]) != snapshot["media_type"]:
            raise Conflict("citation source media type differs")
        entry = self._entry(module)
        if citation.get("dataset_id", entry["dataset_id"]) != entry["dataset_id"]:
            raise Conflict("citation dataset differs")
        document = self._document(snapshot)
        evidence = self._span(citation.get("evidence_id"), module)
        if evidence["snapshot_id"] != snapshot["id"]:
            raise Conflict("evidence refers to another raw snapshot")
        base, pointer = evidence["pointer"], citation.get("pointer")
        if not isinstance(pointer, str) or (pointer != base and not pointer.startswith(base + "/")):
            raise Conflict("citation pointer outside linked evidence")
        if citation.get("evidence_pointer", base) != base:
            raise Conflict("citation evidence pointer differs")
        original = _pointer(document, base)
        if not _same(original, evidence["excerpt"]) or _hash(original) != evidence["excerpt_hash"]:
            raise Conflict("stored evidence differs from original raw snapshot")
        if citation.get("excerpt_hash") != evidence["excerpt_hash"]:
            raise Conflict("citation excerpt hash differs")
        value = _pointer(document, pointer)
        semantics = citation.get("selector_semantics")
        if semantics == "RFC6901_then_Unicode_codepoint_slice":
            start, end = citation.get("start"), citation.get("end")
            if not isinstance(value, str) or type(start) is not int or type(end) is not int \
                    or not 0 <= start < end <= len(value):
                raise InvalidData("invalid Unicode evidence range")
            value = value[start:end]
        elif "start" in citation or "end" in citation:
            raise InvalidData("range missing explicit Unicode selector semantics")
        elif semantics == "RFC6901_on_parsed_visible_text_projection":
            if snapshot["media_type"].split(";", 1)[0] != "text/html":
                raise InvalidData("visible HTML selector applied to a non-HTML snapshot")
        elif semantics not in (None, "RFC6901_on_original_JSON"):
            raise InvalidData("unknown selector semantics")
        if "field_value" in citation and not _same(value, citation["field_value"]):
            raise Conflict("citation field value differs from original raw field")
        if "field_hash" in citation and citation["field_hash"] != _hash(value):
            raise Conflict("citation field hash differs from original raw field")
        record = self._record(citation.get("record_id"), module)
        self._linked(record["id"], evidence["id"], module)
        return {"value": value, "pointer": pointer, "evidence": evidence,
                "record": record, "snapshot": snapshot}

    def audit_provenance(self, case):
        result = SourceAudit(applicable=True)
        try:
            if case.get("split") != "development":
                raise InvalidData("this replay only accepts development cases")
            provenance = case["provenance"]
            self._check_pin(provenance["source_pin"])
            if not _same(provenance["source_pin"], self.catalog["source_pin"]):
                raise Conflict("case source pin differs from source catalog")
            refs = provenance["source_refs"]
            if not isinstance(refs, list):
                raise InvalidData("source_refs is not a list")
            for ref in refs:
                wanted = self.catalog_records.get(ref.get("record_id"))
                if wanted is None or any(not _same(value, wanted.get(key)) for key, value in ref.items()):
                    raise Conflict("case source reference differs from pinned catalog")
                record = self._record(ref["record_id"], ref["module"])
                for key in ("dataset_id", "module", "kind", "native_id", "name"):
                    if not _same(ref.get(key), record[key]):
                        raise Conflict("case record identity differs from fixed database")
                if ref.get("theme_id") != (record["parent_native_id"] if ref["module"] == "roguelike" else None):
                    raise Conflict("case source theme differs from actual record")
                evidence = ref["evidence"]
                span = self._span(evidence["evidence_id"], ref["module"])
                snapshot = self._snapshot(evidence["snapshot_id"], ref["module"])
                citation = {
                    "record_id": ref["record_id"], "dataset_id": ref["dataset_id"],
                    **evidence, "source_url": snapshot["url"],
                }
                self._verify_citation(citation, ref["module"])
                if span["pointer"] != evidence["pointer"]:
                    raise Conflict("case source must identify the complete linked evidence")
                declared = self.catalog_snapshots.get(snapshot["id"])
                if declared is None or any(declared.get(key) != snapshot[actual] for key, actual in (
                    ("snapshot_hash", "content_hash"), ("revision", "revision"),
                    ("url", "url"), ("media_type", "media_type"), ("byte_size", "byte_size"),
                )):
                    raise Conflict("catalog snapshot differs from fixed source inventory")
                result.checks["provenance_reference"] += 1
            result.checks["source_pin"] += 1
        except (Conflict, InvalidData, NotFound, KeyError, TypeError, ValueError, OSError) as exc:
            result.errors.append({"code": "provenance_invalid", "detail": str(exc)})
        return result

    def _rows(self, module, kind=None):
        key = (module, kind)
        if key not in self._module_rows:
            query = select(KnowledgeRecord.id).where(
                KnowledgeRecord.dataset_id == self._entry(module)["dataset_id"],
                KnowledgeRecord.module == module,
            )
            if kind:
                query = query.where(KnowledgeRecord.kind == kind)
            ids = self.session.scalars(query.order_by(KnowledgeRecord.kind, KnowledgeRecord.native_id))
            self._module_rows[key] = [self._record(ident, module) for ident in ids]
        return self._module_rows[key]

    def _raw_record(self, record):
        """Return one complete source excerpt, checking real record ownership."""
        module, ident = record["module"], record["id"]
        if ident not in self._record_spans:
            self._record_spans[ident] = self.session.scalars(select(RecordEvidence.evidence_id).where(
                RecordEvidence.record_id == ident,
                RecordEvidence.dataset_id == self._entry(module)["dataset_id"],
            )).all()
            self.cache_metrics["record_evidence_inventory_reads"] += 1
        evidence_ids = self._record_spans[ident]
        if not evidence_ids:
            raise Conflict("record has no linked source evidence")
        raw = None
        for evidence_id in evidence_ids:
            span = self._span(evidence_id, module)
            snapshot = self._snapshot(span["snapshot_id"], module)
            value = _pointer(self._document(snapshot), span["pointer"])
            if not _same(value, span["excerpt"]) or _hash(value) != span["excerpt_hash"]:
                raise Conflict("record source excerpt differs from retained raw bytes")
            if raw is not None and not _same(value, raw):
                raise Conflict("record has differing complete source excerpts")
            raw = value
        return raw

    def _claim_value(self, claim, refs, module):
        """Return True/False, or None for an explicitly unimplemented derivation."""
        value = claim["value"]
        derived = claim.get("derived")
        values = [ref["value"] for ref in refs]
        if not derived:
            if any(_same(value, item) for item in values):
                return True
            return any(ref["record"]["kind"] == "recruitment_tag"
                       and isinstance(ref["value"], dict)
                       and _same(value, ref["value"].get("tagName")) for ref in refs)
        if not isinstance(derived, dict):
            return False
        operation = derived.get("operation")
        if operation == "TIER_n_to_stars":
            return type(value) is int and any(item == f"TIER_{value}" for item in values)
        if operation == "zero_based_potential_rank_plus_one":
            return type(value) is int and any(type(item) is int and value == item + 1 for item in values)
        if operation == "one_based_level_index":
            index = derived.get("source_index")
            return type(index) is int and type(value) is int and value == index + 1 \
                and any(isinstance(item, list) and 0 <= index < len(item) for item in values)
        if operation == "unix_timestamp_to_iso8601":
            return derived.get("timezone") == "Asia/Shanghai" and any(
                type(item) in (int, float)
                and datetime.fromtimestamp(item, ZoneInfo("Asia/Shanghai")).isoformat() == value
                for item in values
            )
        if operation == "enum_display":
            mappings = {
                "profession": {"PIONEER": "先锋", "WARRIOR": "近卫", "SNIPER": "狙击", "CASTER": "术师", "MEDIC": "医疗", "SUPPORT": "辅助", "TANK": "重装", "SPECIAL": "特种"},
                "position": {"MELEE": "近战位", "RANGED": "远程位", "ALL": "全部位置"},
                "spType": {"INCREASE_WITH_TIME": "自动回复", "INCREASE_WHEN_ATTACK": "攻击回复", "INCREASE_WHEN_TAKEN_DAMAGE": "受击回复", "NONE": "无", None: "来源未指定"},
                "skillType": {"MANUAL": "手动触发", "AUTO": "自动触发", "PASSIVE": "被动"},
            }
            for ref in refs:
                raw, field_name = ref["value"], ref["pointer"].rsplit("/", 1)[-1]
                if not _same(derived.get("raw"), raw) or field_name not in mappings:
                    continue
                display = mappings[field_name].get(raw)
                if display is None and field_name in {"spType", "skillType"}:
                    display = f"未核验{'回复' if field_name == 'spType' else '触发'}类型（来源编码 {raw}）"
                if _same(value, display):
                    return True
            return False
        if operation == "allowlisted_client_template_display":
            templates = [v for v in values if isinstance(v, str)]
            boards = [v for v in values if isinstance(v, list)]
            if len(templates) != 1 or len(boards) != 1:
                return False
            rendered, substitutions, unresolved = _render_template(templates[0], boards[0])
            return _same(value, rendered) and _same(derived.get("substitutions"), substitutions) \
                and _same(derived.get("unresolved_templates"), unresolved)
        if operation == "reviewed_recruitment_selector_mapping":
            member = self._recruitment_pool()[0].get(claim.get("record_id"))
            return member is not None and _same(value, {
                key: member[key] for key in ("name", "rarity_stars", "tag_names")
            })
        if operation == "checked_official_announcement_schedule_extraction":
            return _official_schedule(value, refs)
        # Composite audited garrison descriptions are not independently proved
        # merely by finding their individual numbers somewhere in 45 citations.
        return None

    def _recruitment_pool(self):
        cache = self.__dict__.get("_recruitment_checked")
        if cache is not None:
            self.cache_metrics["recruitment_pool_cache_hits"] += 1
            return cache
        rows = self._rows("recruitment")
        tags, rules, members = {}, {}, {}
        professions = {"PIONEER": "先锋干员", "WARRIOR": "近卫干员", "SNIPER": "狙击干员", "CASTER": "术师干员", "MEDIC": "医疗干员", "SUPPORT": "辅助干员", "TANK": "重装干员", "SPECIAL": "特种干员"}
        positions = {"MELEE": "近战位", "RANGED": "远程位"}
        originals = {row["id"]: self._raw_record(row) for row in rows}
        for row in rows:
            raw = originals[row["id"]]
            if row["kind"] == "recruitment_tag":
                if not isinstance(raw, dict) or row["name"] != raw.get("tagName") \
                        or row["native_id"] != str(raw.get("tagId")) or not _same(row["data"], raw):
                    raise Conflict("recruitment tag differs from original source")
                tags[row["name"]] = raw["tagId"]
            if row["kind"] in {"recruitment_rules", "recruitment_calculator_rules"}:
                rules[row["native_id"]] = raw
        for row in rows:
            if row["kind"] != "recruitment_member":
                continue
            raw = originals[row["id"]]
            stars = int(raw["rarity"].removeprefix("TIER_"))
            names = [professions[raw["profession"]], positions[raw["position"]], *raw["tagList"]]
            if stars in (5, 6):
                names.append("高级资深干员" if stars == 6 else "资深干员")
            item = {"record_id": row["id"], "native_id": row["native_id"], "name": raw["name"],
                    "rarity_stars": stars, "tag_names": names, "tag_ids": sorted(tags[n] for n in names)}
            if row["name"] != raw["name"] or any(not _same(row["data"].get(k), item[k])
                    for k in ("name", "rarity_stars", "tag_names", "tag_ids")):
                raise Conflict("recruitment normalized member differs from raw source")
            members[row["id"]] = item
        detail = rules.get("recruitDetail")
        if not isinstance(detail, str):
            raise Conflict("recruitment membership description absent")
        # Only explicit star sections establish membership; opening prose does not.
        clean = html.unescape(re.sub(r"<[^>]*>", "", detail)).replace("\\n", "\n")
        declared = []
        sections = re.findall(r"(★{1,6})\s*\n(.*?)(?=\n[-]{3,}|\Z)", clean, re.S)
        for stars, names in sections:
            declared.extend((name.strip(), len(stars)) for name in names.split("/") if name.strip())
        if sorted(declared) != sorted((m["name"], m["rarity_stars"]) for m in members.values()):
            raise Conflict("recruitment full pool differs from explicit source membership")
        coverage = self._entry("recruitment").get("coverage", {})
        if len(members) != coverage.get("member_count") or len(tags) != coverage.get("selectable_tag_count"):
            raise Conflict("recruitment full selector scope differs from manifest coverage")
        self._recruitment_checked = (members, tags, rules)
        self.cache_metrics["recruitment_full_pool_replays"] += 1
        return self._recruitment_checked

    def _catalog(self, answer, module):
        catalog = answer["catalog"]
        proposal = answer.get("query_interpretation", {}).get("proposal", {})
        if module == "operator":
            universe = self._rows(module, "operator")
            expected_ids = self._entry(module).get("coverage", {}).get("operator_ids")
            if expected_ids is not None and set(expected_ids) != {r["native_id"] for r in universe}:
                raise Conflict("complete operator scope differs from release coverage")
            all_rows = []
            for row in universe:
                raw = self._raw_record(row)
                if row["name"] != raw.get("name") or not _same(row["data"], raw):
                    raise Conflict("operator catalog selector differs from raw source")
                all_rows.append({"native_id": row["native_id"], "name": row["name"], "rarity": raw["rarity"]})
            if catalog.get("rarity") is not None:
                all_rows = [r for r in all_rows if r["rarity"] == f"TIER_{catalog['rarity']}"]
            field_name = "items"
        elif module == "recruitment":
            all_rows = list(self._recruitment_pool()[0].values())
            if catalog.get("rarity") is not None:
                all_rows = [r for r in all_rows if r["rarity_stars"] == catalog["rarity"]]
            field_name = "members"
        else:
            raise InvalidData("catalog is not a supported module shape")
        if proposal.get("entity_names"):
            selected = []
            for name in proposal["entity_names"]:
                matches = [r for r in all_rows if name in (r["name"], r["native_id"])]
                if len(matches) != 1:
                    raise Conflict("catalog declared selector cannot uniquely bind source object")
                selected.extend(matches)
            all_rows = selected
        offset, limit = catalog.get("offset"), catalog.get("limit")
        if type(offset) is not int or type(limit) is not int or offset < 0 or limit < 1:
            raise InvalidData("catalog pagination has invalid typed bounds")
        shown = all_rows[offset:offset + limit]
        if type(catalog.get("count")) is not int or catalog["count"] != len(shown) \
                or type(catalog.get("total_count")) is not int or catalog["total_count"] != len(all_rows):
            raise Conflict("catalog counts omit or invent source matches")
        actual = catalog.get(field_name)
        if not isinstance(actual, list) or len(actual) != len(shown):
            raise Conflict("catalog page does not contain the full declared page")
        keys = ("native_id", "name") if module == "operator" else ("native_id", "name", "rarity_stars", "tag_names")
        if any(not _same({k: got.get(k) for k in keys}, {k: want[k] for k in keys})
               for got, want in zip(actual, shown, strict=True)):
            raise Conflict("catalog page identity differs from source filtering")
        for key, wanted in (("complete_for_published_scope", offset == 0 and len(shown) == len(all_rows)),
                            ("has_more", offset + len(shown) < len(all_rows))):
            if key in catalog and not _same(catalog[key], wanted):
                raise Conflict("catalog page falsely claims completeness")
        if module == "recruitment":
            tags = sorted({name for row in all_rows for name in row["tag_names"]})
            if not _same(catalog.get("tags"), tags):
                raise Conflict("catalog related tags differ from complete source matching scope")
            by_tag = self._recruitment_pool()[1]
            rare = self._recruitment_pool()[2]["specialTagRarityTable"]
            expected_rare = [name for name in tags if catalog.get("rarity") is not None
                             and rare.get(str(by_tag[name])) == [catalog["rarity"] - 1]]
            if not _same(catalog.get("rarity_specific_tags"), expected_rare):
                raise Conflict("catalog rarity-specific tags differ from fixed special-tag mapping")

    def _calculation(self, answer):
        members, tags, rules = self._recruitment_pool()
        inputs = answer["input"]
        names, duration = inputs["normalized_tags"], inputs["duration_minutes"]
        if not isinstance(names, list) or len(names) > 5 or len(set(names)) != len(names) \
                or any(name not in tags for name in names) or type(duration) is not int \
                or not 60 <= duration <= 540 or duration % 10:
            raise InvalidData("calculation input is not source-selectable")
        timer = rules["recruitRarityTable"][str(duration)]
        lower, upper = timer["rarityStart"] + 1, timer["rarityEnd"] + 1
        if not _same(answer.get("normal_rarity_range_stars"), [lower, upper]):
            raise Conflict("calculation ordinary rarity range differs from source duration rule")
        selections = [c for size in range(1, min(3, len(names)) + 1) for c in combinations(names, size)] or [()]
        actual = answer["combinations"]
        if not isinstance(actual, list) or len(actual) != len(selections) \
                or type(answer.get("combination_count")) is not int or answer["combination_count"] != len(selections):
            raise Conflict("calculation must return every nonempty 1..3-tag combination")
        found = {}
        for combo in actual:
            key = tuple(combo.get("selected_tags", []))
            if key in found:
                raise Conflict("calculation repeats a combination")
            found[key] = combo
        if set(found) != set(selections):
            raise Conflict("calculation combination coverage differs from actual input")
        for selected in selections:
            combo = found[selected]
            ids = {tags[name] for name in selected}
            special = next((tag for tag in (11, 14) if tag in ids), None)
            allowed = {rules["specialTagRarityTable"][str(special)][0] + 1} if special else set(range(lower, upper + 1))
            expected = [m for m in members.values() if m["rarity_stars"] in allowed
                        and (ids - {11, 14}).issubset(m["tag_ids"])]
            expected.sort(key=lambda m: (m["rarity_stars"], m["native_id"]))
            keys = ("native_id", "name", "rarity_stars", "tag_names", "record_id")
            candidates = combo.get("candidates", [])
            if not _same([{k: c.get(k) for k in keys} for c in candidates],
                         [{k: m[k] for k in keys} for m in expected]):
                raise Conflict("calculation candidate intersection differs from source rules")
            for candidate in candidates:
                wanted_evidence = self._record_spans.get(candidate["record_id"], [])
                declared_evidence = candidate.get("evidence_ids")
                if not isinstance(declared_evidence, list) or len(declared_evidence) != len(set(declared_evidence)) \
                        or set(declared_evidence) != set(wanted_evidence):
                    raise Conflict("calculation candidate evidence binding differs from its source record")
            minimum = min((m["rarity_stars"] for m in expected), default=None)
            checks = {"selected_tag_ids": sorted(ids), "candidate_count": len(expected),
                      "minimum_rarity_if_tags_retained": minimum,
                      "status": "conditional_candidates" if expected else "no_candidate_if_tags_retained",
                      "ordinary_tags_may_drop": True,
                      "special_tags_protected_at_this_duration": special is not None and duration == 540}
            if any(not _same(combo.get(k), v) for k, v in checks.items()):
                raise Conflict("calculation conditional summary differs from source intersection")
        if answer.get("probabilities") is not None:
            raise Conflict("source calculator does not establish recruitment probabilities")
        self._recruitment_presentation(answer, tags)

    def _recruitment_presentation(self, answer, tags):
        """Check display copies independently of the production projection/renderer.

        Old saved calculations have no display contract and remain valid. New
        display metadata must select complete, already source-replayed rows;
        hashes alone cannot establish the selected row's identity or contents.
        """
        complete = answer["combinations"]
        focus = answer.get("requested_focus")
        if focus is not None:
            if not isinstance(focus, dict) or type(focus.get("rarity_stars")) is not int \
                    or not 1 <= focus["rarity_stars"] <= 6:
                raise Conflict("rarity focus has no supported explicit source rarity")
            expected_focus = []
            for row in complete:
                matches = [c for c in row["candidates"] if c["rarity_stars"] == focus["rarity_stars"]]
                expected_focus.append({
                    "selected_tags": row["selected_tags"],
                    "original_minimum_rarity_if_tags_retained": row["minimum_rarity_if_tags_retained"],
                    "matching_candidate_count": len(matches), "matching_candidates": matches,
                    "evidence_ids": row["evidence_ids"],
                })
            if not _same(focus, {
                "rarity_stars": focus["rarity_stars"],
                "operation": "source_candidate_rarity_view_without_modifying_combinations",
                "combinations_preserved": True, "guarantee_or_probability": False,
                "combinations": expected_focus,
            }):
                raise Conflict("rarity focus differs from the complete source calculation")
        display = answer.get("recruitment_display")
        proposal = answer.get("query_interpretation", {}).get("proposal", {})
        if display is None:
            if proposal.get("recruitment_scope") is not None:
                raise Conflict("explicit recruitment scope has no verified display projection")
            return
        if not isinstance(display, dict) or display.get("scope") not in {
            "all_combinations", "exact_selected_tags",
        }:
            raise Conflict("recruitment display scope is invalid")
        scope = display["scope"]
        if proposal.get("recruitment_scope") is not None and proposal["recruitment_scope"] != scope:
            raise Conflict("recruitment display scope differs from its typed proposal")
        selected_ids = sorted(tags[name] for name in answer["input"]["normalized_tags"])
        if scope == "exact_selected_tags":
            if len(selected_ids) > 3:
                raise Conflict("exact recruitment scope cannot select more than three tags")
            rows = [row for row in complete if _same(row["selected_tag_ids"], selected_ids)]
            if len(rows) != 1:
                raise Conflict("exact recruitment scope has no unique complete source row")
        else:
            rows = complete
        expected = {
            "scope": scope, "selected_tag_ids": selected_ids,
            "displayed_combination_count": len(rows), "total_combination_count": len(complete),
            "combinations_preserved": True, "combinations": rows,
            "proof": {"complete_combinations_sha256": _hash(complete),
                      "displayed_combinations_sha256": _hash(rows)},
        }
        if not _same(display, expected):
            raise Conflict("recruitment display is not the complete verified source projection")
        text = answer.get("answer")
        if not isinstance(text, str):
            raise Conflict("recruitment display has no visible answer")
        minutes = answer["input"]["duration_minutes"]
        hours, remainder = divmod(minutes, 60)
        duration = (f"{hours}小时" + (f"{remainder}分钟" if remainder else "")) if hours else f"{minutes}分钟"
        chosen = "、".join(answer["input"]["normalized_tags"]) or "无标签"
        header = (
            f"共{len(complete)}个组合，以下全部列出。候选与最低星级均以所选标签保留为条件。"
            if scope == "all_combinations" else
            "仅展示这组标签同时保留时的交集。候选与最低星级均以所选标签保留为条件。"
        )
        if text.splitlines()[:2] != [f"标签：{chosen}；招募时长：{duration}（{minutes}分钟）。", header]:
            raise Conflict("recruitment visible header differs from source inputs/display scope")
        expected_rows = []
        for row in rows:
            label = " + ".join(row["selected_tags"]) or "无标签"
            if row["candidates"]:
                by_rarity = {}
                for candidate in row["candidates"]:
                    by_rarity.setdefault(candidate["rarity_stars"], []).append(candidate["name"])
                names = "；".join(f"{stars}星：{'、'.join(by_rarity[stars])}" for stars in sorted(by_rarity))
                outcome = (f"最低{row['minimum_rarity_if_tags_retained']}星，"
                           f"共{row['candidate_count']}名候选（{names}）")
            else:
                outcome = "所选标签全部保留时交集为空，无候选；不能据此预测实际掉标签结果"
            protection = "；该时长稀有标签受保护" if row["special_tags_protected_at_this_duration"] else ""
            expected_rows.append(f"- {label}：{outcome}{protection}。")
        primary_rows = list(expected_rows)
        focus_rows = []
        if focus is not None:
            for row in rows:
                names = "、".join(c["name"] for c in row["candidates"]
                                 if c["rarity_stars"] == focus["rarity_stars"]) or "无该星级候选"
                label = " + ".join(row["selected_tags"]) or "无标签"
                focus_rows.append(f"- {label}：{names}。")
            expected_rows.extend(focus_rows)
            if "关注星级只提供候选视图，不把完整组合筛成保证该星级；仍以所选标签保留为条件，不代表概率或必出结果。" \
                    not in text.splitlines():
                raise Conflict("recruitment visible rarity view omits its conditional boundary")
        if not _same([line for line in text.splitlines() if line.startswith("- ")], expected_rows):
            raise Conflict("recruitment visible rows differ from the verified display scope")
        if any(f"注意：{warning}" not in text.splitlines() for warning in answer["warnings"]):
            raise Conflict("recruitment visible display omits source calculation warnings")
        lines = text.splitlines()
        # Besides the pinned source footer, every visible line belongs to the
        # reviewed calculation presentation. Extra prose cannot add a guarantee
        # while leaving all structured candidates and their hashes untouched.
        source_index = 2 + len(primary_rows) + len(answer["warnings"])
        if len(lines) <= source_index or not lines[source_index].startswith("来源："):
            raise Conflict("recruitment visible display has no source footer")
        expected_lines = [*lines[:2], *primary_rows,
                          *(f"注意：{warning}" for warning in answer["warnings"]), lines[source_index]]
        if focus is not None:
            expected_lines.extend([
                "", f"关注{focus['rarity_stars']}星的来源候选视图（完整组合与原最低星级另行完整保留）：",
                *focus_rows,
                "关注星级只提供候选视图，不把完整组合筛成保证该星级；仍以所选标签保留为条件，不代表概率或必出结果。",
            ])
        if lines != expected_lines:
            raise Conflict("recruitment visible display adds or changes unverified prose")

    def _structured_sources(self, answer, verified, module):
        checked = Counter()
        candidates = answer.get("candidates", [])
        if not isinstance(candidates, list):
            raise InvalidData("candidates must be a list")
        for index, candidate in enumerate(candidates, 1):
            refs = [verified[c] for c in candidate.get("citation_ids", [])]
            if not refs or type(candidate.get("number")) is not int or candidate["number"] != index:
                raise Conflict("candidate lacks an ordered real source binding")
            if not any(ref["record"]["native_id"] == candidate.get("native_id")
                       and ref["record"]["name"] == candidate.get("name") for ref in refs):
                raise Conflict("candidate identity differs from its cited real record")
            checked["candidate_identity"] += 1
        for record in answer.get("records", []):
            actual = self._record(record["id"], module)
            if any(not _same(record.get(key), actual[key]) for key in (
                "dataset_id", "module", "kind", "native_id", "name", "parent_native_id", "data"
            )) or not _same(record.get("data"), self._raw_record(actual)):
                raise Conflict("returned source record differs from original complete object")
            checked["returned_record"] += 1
        for relation in answer.get("relations", []):
            source = self._record(relation["source_record_id"], module)
            target = self._record(relation["target_record_id"], module)
            if source["parent_native_id"] != target["parent_native_id"] \
                    or relation.get("theme_id") != source["parent_native_id"]:
                raise Conflict("relation joins source objects from different themes")
            reference = _pointer(self._raw_record(source), relation["reference_field"])
            if not _same(reference, relation.get("reference_value")) \
                    or not _same(self._raw_record(target).get("id"), reference):
                raise Conflict("relation reference is not established by original source objects")
            refs = [verified[c] for c in relation.get("citation_ids", [])]
            if not all(any(ref["record"]["id"] == ident and _same(ref["value"], reference)
                           for ref in refs) for ident in (source["id"], target["id"])):
                raise Conflict("relation has no explicit source and target reference citations")
            checked["source_relation"] += 1
        return checked

    def check_selector_scope(self, answer, *, module, expectations, verified=None):
        """Check typed requested selectors against delivered fields, not echoes.

        Context-dependent target resolution remains the caller's responsibility;
        this method consumes the resolved partial selectors, never parses a user
        question, and never fills missing conditions from another expectation.
        """
        result = SourceAudit()
        selectors = expectations.get("selectors", {})
        intent = expectations.get("intent")
        if not isinstance(selectors, dict):
            result.errors.append({"code": "invalid_expected_selectors"})
            return result
        if not selectors:
            return result
        try:
            if verified is None:
                verified = {c.get("id", f"@{i}"): self._verify_citation(c, module)
                            for i, c in enumerate(answer.get("citations", []))}
            refs = list(verified.values())
            # Empty refusals do not demonstrate any factual selector scope. A
            # strict answered expectation is already checked by audit_answer.
            if not answer.get("claims") and not answer.get("combinations") and not answer.get("candidates"):
                return result
            result.applicable = True
            if "catalog" in answer:
                catalog = answer["catalog"]
                for key, default in (("rarity", None), ("offset", 0), ("limit", 30)):
                    wanted = selectors.get(key, default)
                    if wanted is None and key in {"offset", "limit"}:
                        wanted = default
                    if not _same(catalog.get(key), wanted):
                        raise Conflict(f"returned catalog {key} differs from requested selector")
            if module == "recruitment" and answer.get("combinations"):
                inputs = answer["input"]
                for wanted, actual in (("tags", "normalized_tags"), ("duration_minutes", "duration_minutes")):
                    if wanted in selectors and not _same(inputs.get(actual), selectors[wanted]):
                        raise Conflict(f"computed {wanted} differs from requested input")
                if selectors.get("recruitment_scope") is not None \
                        and answer.get("recruitment_display", {}).get("scope") != selectors["recruitment_scope"]:
                    raise Conflict("computed recruitment display differs from requested scope")
                rarity = selectors.get("rarity")
                focus = answer.get("requested_focus")
                if rarity is not None:
                    if not isinstance(focus, dict) or not _same(focus.get("rarity_stars"), rarity):
                        raise Conflict("computed rarity focus differs from requested selector")
                    if len(focus.get("combinations", [])) != len(answer["combinations"]):
                        raise Conflict("rarity focus omits original combinations")
                    for original, view in zip(answer["combinations"], focus["combinations"], strict=True):
                        candidates = [c for c in original["candidates"] if c["rarity_stars"] == rarity]
                        if not _same(view.get("selected_tags"), original["selected_tags"]) \
                                or not _same(view.get("matching_candidates"), candidates):
                            raise Conflict("rarity focus changes source combinations/candidates")
            if module == "operator" and intent != "catalog":
                # Unused citations cannot stand in for an omitted family/field.
                used = {ident for claim in answer.get("claims", []) for ident in claim.get("citation_ids", [])}
                factual_refs = [ref for ident, ref in verified.items() if ident in used]
                self._operator_scope(answer, selectors, intent, factual_refs, result)
            if module == "garrison":
                phase, field_key = selectors.get("phase_id"), selectors.get("field_key")
                if field_key:
                    key = f"{phase}:{field_key}"
                    if not any(ref["record"]["native_id"] == key for ref in refs):
                        raise Conflict("garrison output lacks requested phase/field source binding")
                    if answer.get("version", {}).get("phase_id") != phase:
                        raise Conflict("garrison output uses another historical phase")
                elif phase and any(ref["record"]["kind"] == "verified_phase"
                                   and ref["record"]["native_id"] != phase for ref in refs):
                    raise Conflict("garrison metadata includes another requested phase")
                activity = selectors.get("activity_id")
                if activity and not any(ref["value"] == activity for ref in refs):
                    raise Conflict("garrison output lacks requested activity's raw identity")
            if module == "roguelike":
                theme = selectors.get("theme_id")
                if theme is not None and answer.get("theme_id") != theme:
                    raise Conflict("roguelike delivered source theme differs from requested theme")
                name = selectors.get("entity_name")
                if name and answer.get("records") and not any(
                    name in (record.get("name"), record.get("native_id"))
                    for record in answer["records"]
                ):
                    raise Conflict("roguelike delivered objects omit the requested explicit identity")
            result.checks["typed_selector_scope"] += 1
        except (Conflict, InvalidData, NotFound, KeyError, IndexError, TypeError, ValueError, OSError) as exc:
            result.errors.append({"code": "selector_scope_mismatch", "detail": str(exc)})
        return result

    def _operator_scope(self, answer, selectors, intent, refs, audit):
        names = selectors.get("entity_names")
        if not names:
            audit.unverified.append({"code": "operator_scope_requires_resolved_entity_names"})
            return
        operators = self._rows("operator", "operator")
        selected = []
        for name in names:
            matches = [r for r in operators if name in (r["native_id"], r["name"])]
            if len(matches) != 1:
                raise Conflict("requested operator is not unique in the fixed source")
            selected.append(matches[0])
        source_operators = {r["native_id"]: self._raw_record(r) for r in selected}
        returned = {ref["record"]["native_id"] for ref in refs if ref["record"]["kind"] == "operator"}
        if returned != set(source_operators):
            raise Conflict("delivered operator set differs from explicit requested operators")
        owned_skills = {slot.get("skillId") for raw in source_operators.values() for slot in raw.get("skills", [])}
        for ref in refs:
            record = ref["record"]
            if record["kind"] == "skill" and record["native_id"] not in owned_skills:
                raise Conflict("delivered skill is not owned by a requested operator")
            if record["kind"] in {"talent", "module"} and record["parent_native_id"] not in source_operators:
                raise Conflict("delivered talent/module is not owned by a requested operator")
        if selectors.get("rarity") is not None and any(
            raw.get("rarity") != f"TIER_{selectors['rarity']}" for raw in source_operators.values()
        ):
            raise Conflict("delivered operator rarity differs from requested source filter")
        if intent == "operator_skill":
            skills = set()
            for raw in source_operators.values():
                slots = raw.get("skills", [])
                index = selectors.get("skill_index")
                if index is not None:
                    if type(index) is not int or not 1 <= index <= len(slots):
                        raise Conflict("requested skill slot is absent from the original operator")
                    skills.add(slots[index - 1]["skillId"])
                else:
                    skills.update(slot["skillId"] for slot in slots)
            if selectors.get("skill_name"):
                skills = {r["native_id"] for r in self._rows("operator", "skill")
                          if r["native_id"] in skills and r["name"] == selectors["skill_name"]}
            actual = {ref["record"]["native_id"] for ref in refs if ref["record"]["kind"] == "skill"}
            if not actual or not actual.issubset(skills):
                raise Conflict("delivered skill belongs to another requested slot/operator")
            rank = selectors.get("rank")
            for ref in refs:
                if ref["record"]["kind"] != "skill":
                    continue
                relative = ref["pointer"][len(ref["evidence"]["pointer"]):]
                match = re.match(r"/levels/(\d+)(?:/|$)", relative)
                if rank is not None and match and int(match[1]) != rank - 1:
                    raise Conflict("delivered skill field is from a different requested rank")
            if rank is not None and any(
                claim.get("derived", {}).get("operation") == "one_based_level_index"
                and (not _same(claim.get("value"), rank)
                     or claim["derived"].get("source_index") != rank - 1)
                for claim in answer.get("claims", [])
            ):
                raise Conflict("delivered skill level mapping differs from requested rank")
        elif intent == "operator_attributes":
            phase = selectors.get("elite_phase")
            level = selectors.get("level")
            if phase is None or level is None:
                audit.unverified.append({"code": "attribute_scope_requires_phase_and_level"})
                return
            phase_index = int(phase.removeprefix("PHASE_"))
            expected = {}
            for native, raw in source_operators.items():
                source_phase = raw["phases"][phase_index]
                actual_level = source_phase["maxLevel"] if level == "max" else level
                frames = source_phase["attributesKeyFrames"]
                matches = [i for i, frame in enumerate(frames) if _same(frame.get("level"), actual_level)]
                if len(matches) != 1:
                    raise Conflict("requested attribute level lacks an original source keyframe")
                expected[native] = (phase_index, matches[0])
            for ref in refs:
                match = re.search(r"/phases/(\d+)/attributesKeyFrames/(\d+)(?:/|$)", ref["pointer"])
                if match and (int(match[1]), int(match[2])) != expected[ref["record"]["native_id"]]:
                    raise Conflict("attribute claim uses another phase/level keyframe")
        elif intent == "operator_talent":
            expected = set()
            for native, raw in source_operators.items():
                if not isinstance(raw.get("talents"), list) or not raw["talents"]:
                    audit.unverified.append({"code": "legacy_talent_family_source_not_available"})
                    return
                for family_index, family in enumerate(raw["talents"]):
                    eligible = []
                    for index, candidate in enumerate(family.get("candidates") or []):
                        condition = candidate.get("unlockCondition", {})
                        if candidate.get("isHideTalent") is True:
                            continue
                        if selectors.get("elite_phase") and condition.get("phase") != selectors["elite_phase"]:
                            continue
                        if type(selectors.get("level")) is int and condition.get("level", 0) > selectors["level"]:
                            continue
                        potential = selectors.get("potential_rank")
                        if potential is not None and candidate["requiredPotentialRank"] > potential:
                            continue
                        eligible.append((index, candidate))
                    if selectors.get("potential_rank") is not None:
                        by_gate = {}
                        for index, candidate in eligible:
                            condition = candidate.get("unlockCondition", {})
                            gate = (condition.get("phase"), condition.get("level"), candidate.get("prefabKey"))
                            by_gate.setdefault(gate, []).append((index, candidate))
                        eligible = [pair for group in by_gate.values() for pair in group
                                    if pair[1]["requiredPotentialRank"] == max(c["requiredPotentialRank"] for _, c in group)]
                    expected.update(f"{native}:talent:{family_index}:candidate:{i}" for i, _ in eligible)
            actual = {ref["record"]["native_id"] for ref in refs if ref["record"]["kind"] == "talent"}
            if actual != expected:
                raise Conflict("talent output omits a family or uses another phase/level/potential branch")
            for native in expected:
                fields = {ref["pointer"][len(ref["evidence"]["pointer"]):] for ref in refs
                          if ref["record"]["native_id"] == native}
                if not {"/name", "/description", "/unlockCondition", "/requiredPotentialRank"}.issubset(fields):
                    raise Conflict("talent output omits an original family description or condition")
                if selectors.get("include_blackboard") is True and "/blackboard" not in fields:
                    raise Conflict("requested talent blackboard is absent from delivered facts")
        elif intent == "operator_module":
            selected_modules = []
            for native in source_operators:
                modules = [r for r in self._rows("operator", "module") if r["parent_native_id"] == native]
                query = selectors.get("module_query")
                if query:
                    modules = [r for r in modules if query in (r["name"], r["native_id"])
                               or query.upper() == str(self._raw_record(r).get("typeName2", "")).upper()]
                if len(modules) != 1:
                    raise Conflict("requested module does not bind one original module per operator")
                selected_modules.extend(modules)
            expected_modules = {r["native_id"] for r in selected_modules}
            actual_modules = {ref["record"]["native_id"] for ref in refs if ref["record"]["kind"] == "module"}
            if actual_modules != expected_modules:
                raise Conflict("delivered module differs from requested operator/module")
            phases = [r for r in self._rows("operator", "module_phase")
                      if r["parent_native_id"] in expected_modules
                      and _same(self._raw_record(r).get("equipLevel"), selectors.get("module_level"))]
            actual_phases = {ref["record"]["id"] for ref in refs if ref["record"]["kind"] == "module_phase"}
            if actual_phases != {r["id"] for r in phases} or len(phases) != len(expected_modules):
                raise Conflict("delivered module level differs from requested original phase")

    def audit_answer(self, answer, *, module, expectations=None):
        result = SourceAudit()
        if not isinstance(answer, dict):
            result.errors.append({"code": "answer_not_an_object"})
            return result
        expectations = expectations or {}
        try:
            self._check_pin(self.catalog["source_pin"])
            self._entry(module)
            if answer.get("release_id") != self.release.id or answer.get("manifest_hash") != self.release.manifest_hash:
                raise Conflict("answer belongs to another release/manifest")
            if answer.get("module", module) != module:
                raise Conflict("answer belongs to another module")
            expected_status = expectations.get("expected_status")
            if expected_status:
                result.applicable = True
                if answer.get("status") != expected_status:
                    result.errors.append({"code": "expected_status_mismatch", "expected": expected_status,
                                          "actual": answer.get("status")})
                else:
                    result.checks["strict_status"] += 1
            citations, claims = answer.get("citations", []), answer.get("claims", [])
            if not isinstance(citations, list) or not isinstance(claims, list):
                raise InvalidData("answer claims/citations must be lists")
            verified, by_evidence = {}, {}
            for index, citation in enumerate(citations):
                ident = citation.get("id", f"@{index}") if isinstance(citation, dict) else None
                if not isinstance(ident, str) or not ident or ident in verified:
                    raise InvalidData("answer citation ID absent, invalid or duplicated")
                try:
                    verified[ident] = self._verify_citation(citation, module)
                except (Conflict, InvalidData, NotFound, KeyError, TypeError, ValueError, OSError) as exc:
                    result.errors.append({"code": "citation_invalid", "citation_id": ident, "detail": str(exc)})
                else:
                    by_evidence.setdefault(citation["evidence_id"], []).append(verified[ident])
                    result.checks["citation"] += 1
            seen = set()
            for index, claim in enumerate(claims):
                result.applicable = True
                ident = claim.get("id", f"@{index}") if isinstance(claim, dict) else None
                try:
                    if not isinstance(ident, str) or not ident or ident in seen or "value" not in claim:
                        raise InvalidData("claim ID/value absent, invalid or duplicated")
                    seen.add(ident)
                    if "citation_ids" in claim:
                        refs = [verified[c] for c in claim["citation_ids"]]
                    else:
                        refs = [ref for e in claim.get("evidence_ids", []) for ref in by_evidence.get(e, [])]
                    if not refs:
                        raise Conflict("claim has no valid explicit source references")
                    if claim.get("record_id") and any(ref["record"]["id"] != claim["record_id"] for ref in refs):
                        raise Conflict("claim is linked to another record's citations")
                    derived = claim.get("derived")
                    if module == "garrison" and isinstance(derived, dict) \
                            and derived.get("method") == "audited_configuration_and_original_description":
                        # All references above have already passed real PG/raw
                        # ownership checks. Derive from those originals before
                        # comparing the prediction; compiled fact values are
                        # never an input to this independent proof.
                        version = answer.get("version", {})
                        proof = derive_documented_bundle(
                            refs, phase_id=version.get("phase_id"),
                            field_key=version.get("documented_field_key"), source_catalog=self.catalog,
                        )
                        comparison = proof.compare(claim["value"])
                        if comparison["errors"]:
                            raise Conflict(_json(comparison["errors"]))
                        if comparison["unverified"]:
                            result.unverified.extend({**issue, "claim_id": ident} for issue in comparison["unverified"])
                            continue
                        support = comparison["passed"]
                    else:
                        support = self._claim_value(claim, refs, module)
                    if support is None:
                        result.unverified.append({"code": "derived_proof_not_implemented", "claim_id": ident,
                                                  "derived": claim.get("derived")})
                    elif not support:
                        raise Conflict("claim value is not established by its cited original fields")
                    else:
                        result.checks["claim"] += 1
                except (Conflict, InvalidData, NotFound, KeyError, TypeError, ValueError, OSError, OverflowError) as exc:
                    result.errors.append({"code": "claim_unsupported_by_source", "claim_id": ident, "detail": str(exc)})
            if "catalog" in answer:
                result.applicable = True
                self._catalog(answer, module)
                result.checks["selector_scope_complete"] += 1
            if answer.get("status") == "computed" or answer.get("combinations"):
                result.applicable = True
                if module != "recruitment":
                    raise InvalidData("unsupported computed answer module")
                self._calculation(answer)
                result.checks["complete_combinations"] += len(answer["combinations"])
            structured = self._structured_sources(answer, verified, module)
            result.checks.update(structured)
            if structured:
                result.applicable = True
            scope = self.check_selector_scope(answer, module=module, expectations=expectations, verified=verified)
            result.errors.extend(scope.errors)
            result.unverified.extend(scope.unverified)
            result.checks.update(scope.checks)
            result.applicable = result.applicable or scope.applicable
            if answer.get("status") in {"answered", "computed"}:
                result.applicable = True
                if not claims and not answer.get("combinations"):
                    raise Conflict("successful answer has no structured source-supported facts")
                if not citations:
                    raise Conflict("successful answer has no raw source citations")
            result.checks["release_manifest"] += 1
        except (Conflict, InvalidData, NotFound, KeyError, TypeError, ValueError, OSError) as exc:
            result.errors.append({"code": "answer_source_invalid", "detail": str(exc)})
        return result


def _render_template(template, blackboard):
    """Small independent client-display oracle; never a runtime/game simulator."""
    values, duplicate = {}, set()
    for item in blackboard:
        if isinstance(item, dict) and isinstance(item.get("key"), str):
            key = item["key"].lower()
            if key in values:
                duplicate.add(key)
            values[key] = item.get("value")
    used, unresolved = [], []

    def substitute(match):
        parsed = re.fullmatch(r"(-?)([A-Za-z_][A-Za-z0-9_.]*)(?::(0(?:\.0{1,4})?%?))?", match[1])
        if not parsed:
            unresolved.append(match[0])
            return match[0]
        sign, key, spec = parsed.groups()
        raw = values.get(key.lower())
        if key.lower() in duplicate or type(raw) not in (int, float):
            unresolved.append(match[0])
            return match[0]
        try:
            number = Decimal(str(raw)) * (-1 if sign else 1)
            if not number.is_finite():
                raise InvalidOperation
            if spec and spec.endswith("%"):
                number *= 100
            if spec:
                precision = len(spec.rstrip("%").partition(".")[2])
                text = f"{number:.{precision}f}" + ("%" if spec.endswith("%") else "")
            else:
                text = format(number.normalize(), "f")
        except (InvalidOperation, ValueError):
            unresolved.append(match[0])
            return match[0]
        used.append({"key": key, "value": raw, "format": spec, "negated": bool(sign)})
        return text

    text = re.sub(r"\{([^{}]+)\}", substitute, template)
    text = re.sub(r"<(?:[@$][A-Za-z0-9_.]+|/)>", "", text).replace("\\n", "\n")
    return html.escape(text, quote=False), used, list(dict.fromkeys(unresolved))


def _official_schedule(value, refs):
    """Extract the pinned announcement's schedule, never an actual live opening."""
    recognized = False
    for lead in refs:
        text = lead["value"]
        if not isinstance(text, str) or lead.get("snapshot", {}).get("media_type") != "text/html":
            continue
        maintenance = re.search(
            r"」([^「」]+?)将于(\d{2})月(\d{2})日(\d{2}):(\d{2})\s*~\s*"
            r"(\d{2}):(\d{2})的停机维护后开启", text,
        )
        flash_update = re.search(
            r"将在(\d{2})月(\d{2})日(\d{2}):(\d{2})\s*~\s*"
            r"(\d{2}):(\d{2})的闪断更新后开启([^，。]+)[，。]", text,
        )
        if maintenance:
            label, month, day, start_hour, start_minute, end_hour, end_minute = maintenance.groups()
        elif flash_update:
            month, day, start_hour, start_minute, end_hour, end_minute, label = flash_update.groups()
        else:
            continue
        recognized = True
        for publication in refs:
            if publication["record"]["id"] != lead["record"]["id"]:
                continue
            stamp = publication["value"]
            date = re.fullmatch(r"(\d{4}) // (\d{2}) / (\d{2})", stamp) if isinstance(stamp, str) else None
            if not date or (date[2], date[3]) != (month, day):
                continue
            timezone = ZoneInfo("Asia/Shanghai")
            start = datetime(int(date[1]), int(month), int(day), int(start_hour), int(start_minute), tzinfo=timezone)
            end = datetime(int(date[1]), int(month), int(day), int(end_hour), int(end_minute), tzinfo=timezone)
            expected = {"maintenance_start": start.isoformat(), "maintenance_end": end.isoformat(),
                        "opening_condition": "after_maintenance", "actual_opening_time_verified": False,
                        "official_label": label}
            return end >= start and _same(value, expected)
    # A new source wording needs a reviewed extraction rule; it is not proof
    # that a correctly cited product claim is false.
    return False if recognized else None
