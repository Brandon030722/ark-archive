"""Transaction-bound snapshot ingestion, immutable releases and scoped read services.

Callers own commit/rollback. No read service falls back to staging or another release.
"""

import hashlib
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import case, select

from arknights_agent import __version__
from arknights_agent.errors import Conflict, InvalidData, NotFound
from arknights_agent.models import (
    ActiveRelease,
    Dataset,
    DatasetSource,
    Entity,
    EntityAlias,
    EvidenceSpan,
    FactAssertion,
    GarrisonUnit,
    KnowledgeRecord,
    ModulePhase,
    Operator,
    RecordEvidence,
    RecruitPoolMember,
    Release,
    ReleaseDataset,
    Ruleset,
    SkillLevel,
    SourceSnapshot,
    TalentVariant,
    utcnow,
)
from arknights_agent.storage import SnapshotStore

MODULES = {"operator", "recruitment", "garrison", "roguelike"}


def snapshot_document(content: bytes, media_type: str):
    """Deterministic evidence projection; always keep and hash original bytes."""
    if media_type.split(";", 1)[0] == "application/json":
        try:
            return json.loads(content)
        except (ValueError, UnicodeDecodeError) as exc:
            raise InvalidData("source is not valid JSON") from exc
    if media_type.split(";", 1)[0] == "text/html":
        from arknights_agent.ingestion.garrison import html_document

        try:
            return html_document(content)
        except (ValueError, UnicodeDecodeError) as exc:
            raise InvalidData("source is not valid UTF-8 HTML") from exc
    raise InvalidData("source media type has no supported evidence projection")


def canonical_json(value) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise InvalidData("value is not canonical JSON") from exc


def content_digest(value) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def same_json_value(left, right) -> bool:
    """Compare evidence values using the exact canonical representation.

    Python equates booleans with integers, and integers with equal floats,
    including inside nested containers. Those are different quoted JSON fields
    with different field hashes and must not certify each other's evidence.
    Invalid or non-finite JSON is rejected by the existing canonical encoder.
    """
    return canonical_json(left) == canonical_json(right)


def json_pointer(document, pointer: str):
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise InvalidData("evidence selector must be an RFC6901 JSON Pointer")
    try:
        current = document
        for token in pointer[1:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            if isinstance(current, list):
                if not token.isdigit() or (len(token) > 1 and token[0] == "0"):
                    raise KeyError(token)
                current = current[int(token)]
            else:
                current = current[token]
        return current
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise InvalidData("evidence pointer is absent in its source snapshot") from exc


def create_snapshot(session, source, store: SnapshotStore) -> SourceSnapshot:
    digest = store.put(source.content)
    found = session.scalar(
        select(SourceSnapshot).where(
            SourceSnapshot.url == source.url,
            SourceSnapshot.revision == source.revision,
            SourceSnapshot.content_hash == digest,
        )
    )
    if found:
        return found
    snapshot = SourceSnapshot(
        source_key=source.source_key,
        source_type=source.source_type,
        url=source.url,
        revision=source.revision,
        content_hash=digest,
        storage_key=digest,
        media_type=source.media_type,
        byte_size=len(source.content),
        source_metadata=source.metadata,
        published_at=source.published_at,
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def resolve_release(session, release_id=None) -> Release:
    if release_id is None:
        pointer = session.get(ActiveRelease, "default")
        if pointer is None:
            raise NotFound("no active published release")
        release_id = pointer.release_id
    release = session.get(Release, release_id)
    if release is None:
        raise NotFound("published release does not exist")
    if content_digest(release.manifest) != release.manifest_hash:
        raise Conflict("release manifest failed integrity verification")
    return release


def release_view(release) -> dict:
    return {
        "id": release.id,
        "label": release.label,
        "manifest_hash": release.manifest_hash,
        "created_at": release.created_at.isoformat(),
        "manifest": release.manifest,
    }


def _dataset_ids(release, module=None) -> list[str]:
    datasets = release.manifest["datasets"]
    if module is not None:
        if module not in datasets:
            raise NotFound("module is not included in this release")
        return [datasets[module]["dataset_id"]]
    return [item["dataset_id"] for item in datasets.values()]


def _record_view(session, record, evidence_ids=None) -> dict:
    if evidence_ids is None:
        evidence_ids = list(
            session.scalars(
                select(RecordEvidence.evidence_id)
                .where(
                    RecordEvidence.record_id == record.id,
                )
                .order_by(RecordEvidence.evidence_id)
            )
        )
    return {
        "id": record.id,
        "dataset_id": record.dataset_id,
        "entity_id": record.entity_id,
        "module": record.module,
        "kind": record.kind,
        "native_id": record.native_id,
        "name": record.name,
        "parent_native_id": record.parent_native_id,
        "data": record.payload,
        "evidence_ids": evidence_ids,
    }


def list_records(session, release, module=None, kind=None, name=None, limit=50, offset=0,
                 relevance=False):
    if not 1 <= limit <= 200 or offset < 0:
        raise InvalidData("invalid pagination")
    query = select(KnowledgeRecord).where(
        KnowledgeRecord.dataset_id.in_(_dataset_ids(release, module))
    )
    if kind:
        query = query.where(KnowledgeRecord.kind == kind)
    if name:
        # Literal substring search: '%' and '_' are ordinary player input, not wildcards.
        query = query.where(KnowledgeRecord.name.contains(name, autoescape=True))
    order = [KnowledgeRecord.kind, KnowledgeRecord.native_id, KnowledgeRecord.id]
    if relevance and name:
        order = [case((KnowledgeRecord.name == name, 0), else_=1),
                 case((KnowledgeRecord.kind == "operator", 0), else_=1), *order]
    records = list(
        session.scalars(
            query.order_by(*order)
            .limit(limit)
            .offset(offset)
        )
    )
    evidence_by_record = {record.id: [] for record in records}
    for record_id, evidence_id in session.execute(
        select(RecordEvidence.record_id, RecordEvidence.evidence_id)
        .where(RecordEvidence.record_id.in_(evidence_by_record))
        .order_by(RecordEvidence.evidence_id)
    ):
        evidence_by_record[record_id].append(evidence_id)
    return [_record_view(session, record, evidence_by_record[record.id]) for record in records]


def get_record(session, release, record_id):
    record = session.scalar(
        select(KnowledgeRecord).where(
            KnowledgeRecord.id == record_id,
            KnowledgeRecord.dataset_id.in_(_dataset_ids(release)),
        )
    )
    if record is None:
        raise NotFound("record is not included in this release")
    return _record_view(session, record)


def get_evidence(session, release, evidence_id):
    evidence = session.scalar(
        select(EvidenceSpan).where(
            EvidenceSpan.id == evidence_id,
            EvidenceSpan.dataset_id.in_(_dataset_ids(release)),
        )
    )
    if evidence is None:
        raise NotFound("evidence is not included in this release")
    snapshot = session.get(SourceSnapshot, evidence.snapshot_id)
    return {
        "id": evidence.id,
        "dataset_id": evidence.dataset_id,
        "snapshot_id": evidence.snapshot_id,
        "pointer": evidence.pointer,
        "excerpt": evidence.excerpt,
        "excerpt_hash": evidence.excerpt_hash,
        "source_url": snapshot.url,
        "source_revision": snapshot.revision,
    }


def get_snapshot(session, release, snapshot_id):
    included = session.scalar(
        select(DatasetSource)
        .where(
            DatasetSource.dataset_id.in_(_dataset_ids(release)),
            DatasetSource.snapshot_id == snapshot_id,
        )
        .limit(1)
    )
    if included is None:
        raise NotFound("snapshot is not included in this release")
    snapshot = session.get(SourceSnapshot, snapshot_id)
    return {
        "id": snapshot.id,
        "source_key": snapshot.source_key,
        "source_type": snapshot.source_type,
        "url": snapshot.url,
        "revision": snapshot.revision,
        "content_hash": snapshot.content_hash,
        "storage_key": snapshot.storage_key,
        "media_type": snapshot.media_type,
        "byte_size": snapshot.byte_size,
        "metadata": snapshot.source_metadata,
        "fetched_at": snapshot.fetched_at.isoformat(),
        "published_at": snapshot.published_at.isoformat() if snapshot.published_at else None,
    }


def _normalized_fields(record, source_key, pointer):
    return {
        "kind": record.kind,
        "native_id": record.native_id,
        "name": record.name,
        "payload": record.payload,
        "parent_native_id": record.parent_native_id,
        "source_key": source_key,
        "pointer": pointer,
    }


def _fingerprint(module, revision, parser_version, scope, coverage, snapshots, records):
    return content_digest(
        {
            "module": module,
            "revision": revision,
            "parser_version": parser_version,
            "scope": scope,
            "coverage": coverage,
            "sources": sorted((s.source_key, s.content_hash) for s in snapshots),
            "records": sorted(records, key=lambda r: (r["kind"], r["native_id"])),
        }
    )


def _projection(session, record, scope):
    raw = record.payload
    if record.kind == "operator":
        rarity = raw["rarity"]
        if not isinstance(rarity, str) or not rarity.startswith("TIER_"):
            raise InvalidData("unrecognized rarity encoding")
        session.add(
            Operator(
                record_id=record.id,
                rarity=int(rarity[5:]),
                profession=raw["profession"],
                sub_profession=raw.get("subProfessionId"),
                position=raw["position"],
                phases=raw["phases"],
            )
        )
    elif record.kind == "skill":
        for rank, level in enumerate(raw["levels"], start=1):
            sp = level["spData"]
            session.add(
                SkillLevel(
                    record_id=record.id,
                    rank=rank,
                    skill_type=level["skillType"],
                    sp_type=sp["spType"],
                    sp_cost=sp.get("spCost"),
                    initial_sp=sp.get("initSp"),
                    duration=level.get("duration"),
                    description=level.get("description"),
                    blackboard=level["blackboard"],
                )
            )
    elif record.kind == "talent":
        session.add(
            TalentVariant(
                record_id=record.id,
                unlock_condition=raw["unlockCondition"],
                required_potential_rank=raw.get("requiredPotentialRank"),
                description=raw.get("description"),
                blackboard=raw["blackboard"],
            )
        )
    elif record.kind == "module_phase":
        session.add(
            ModulePhase(
                record_id=record.id,
                equip_level=raw["equipLevel"],
                attribute_blackboard=raw["attributeBlackboard"],
                parts=raw["parts"],
            )
        )
    elif record.kind == "recruitment_member":
        session.add(
            RecruitPoolMember(
                record_id=record.id, rarity=raw["rarity_stars"], tags=raw.get("tag_ids")
            )
        )  # Unverified tags are null, never an invented empty set.
    elif record.kind == "chess":
        session.add(
            GarrisonUnit(
                record_id=record.id,
                activity_id=scope["activity_id"],
                chess_id=record.native_id,
                data=raw,
            )
        )


def ingest_batch(session, batch, store: SnapshotStore, parser_version="cn-sample-v1"):
    """Persist one all-or-nothing staging batch; repeat identical content reuses datasets."""
    if not batch.records or not batch.snapshots:
        raise InvalidData("batch must contain source snapshots and records")
    sources = {}
    documents = {}
    for source in batch.snapshots:
        if source.source_key in sources:
            raise InvalidData("duplicate source key")
        if source.source_type == "community_client_data" and source.revision != batch.revision:
            raise InvalidData("batch/source revision mismatch")
        sources[source.source_key] = create_snapshot(session, source, store)
        documents[source.source_key] = snapshot_document(source.content, source.media_type)
    grouped = {}
    seen = set()
    for row in batch.records:
        if (
            row.module not in MODULES
            or row.module not in batch.scopes
            or row.module not in batch.coverage
        ):
            raise InvalidData("record has unsupported or undeclared module scope")
        key = (row.module, row.kind, row.native_id)
        if key in seen:
            raise InvalidData("duplicate normalized record identity")
        seen.add(key)
        if row.source_key not in sources:
            raise InvalidData("record refers to an undeclared source")
        json_pointer(documents[row.source_key], row.pointer)
        grouped.setdefault(row.module, []).append(row)
    datasets = {}
    for module, rows in sorted(grouped.items()):
        scope, coverage = batch.scopes[module], batch.coverage[module]
        fingerprint = _fingerprint(
            module,
            batch.revision,
            parser_version,
            scope,
            coverage,
            sources.values(),
            [_normalized_fields(r, r.source_key, r.pointer) for r in rows],
        )
        dataset = session.scalar(select(Dataset).where(Dataset.fingerprint == fingerprint))
        if dataset is not None:
            datasets[module] = dataset
            continue
        scope_hash = content_digest({"module": module, "revision": batch.revision, "scope": scope})
        ruleset = session.scalar(select(Ruleset).where(Ruleset.scope_hash == scope_hash))
        if ruleset is None:
            ruleset = Ruleset(
                module=module,
                region=scope.get("region", "CN"),
                source_revision=batch.revision,
                scope=scope,
                scope_hash=scope_hash,
            )
            session.add(ruleset)
            session.flush()
        dataset = Dataset(
            module=module,
            ruleset_id=ruleset.id,
            source_revision=batch.revision,
            parser_version=parser_version,
            fingerprint=fingerprint,
            coverage=coverage,
            status="staging",
        )
        session.add(dataset)
        session.flush()
        for key, snapshot in sources.items():
            session.add(
                DatasetSource(dataset_id=dataset.id, snapshot_id=snapshot.id, source_key=key)
            )
        session.flush()
        for row in rows:
            # Scope local mode identities by activity to avoid same-id cross-activity merges.
            entity_native = row.native_id
            if module == "garrison":
                entity_native = f"{scope.get('activity_id', 'unknown')}:{entity_native}"
            entity_id = uuid5(NAMESPACE_URL, f"arknights:{module}:{row.kind}:{entity_native}").hex
            if session.get(Entity, entity_id) is None:
                session.add(
                    Entity(
                        id=entity_id,
                        game="arknights",
                        kind=f"{module}:{row.kind}",
                        native_id=entity_native,
                    )
                )
                session.flush()
            alias = session.scalar(
                select(EntityAlias).where(
                    EntityAlias.entity_id == entity_id,
                    EntityAlias.alias == row.name,
                )
            )
            if alias is None:
                session.add(EntityAlias(entity_id=entity_id, alias=row.name))
            record = KnowledgeRecord(
                dataset_id=dataset.id,
                entity_id=entity_id,
                module=module,
                kind=row.kind,
                native_id=row.native_id,
                name=row.name,
                parent_native_id=row.parent_native_id,
                payload=row.payload,
            )
            session.add(record)
            session.flush()
            excerpt = json_pointer(documents[row.source_key], row.pointer)
            evidence = EvidenceSpan(
                dataset_id=dataset.id,
                snapshot_id=sources[row.source_key].id,
                pointer=row.pointer,
                excerpt=excerpt,
                excerpt_hash=content_digest(excerpt),
            )
            session.add(evidence)
            session.flush()
            session.add(
                RecordEvidence(record_id=record.id, evidence_id=evidence.id, dataset_id=dataset.id)
            )
            session.add(
                FactAssertion(
                    dataset_id=dataset.id,
                    record_id=record.id,
                    predicate="source_payload",
                    value=row.payload,
                    evidence_id=evidence.id,
                    review_status="source_extracted",
                )
            )
            _projection(session, record, scope)
            session.flush()
        datasets[module] = dataset
    return datasets


def seal_dataset(session, dataset_id, store: SnapshotStore):
    dataset = session.scalar(
        select(Dataset)
        .where(Dataset.id == dataset_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if dataset is None:
        raise NotFound("dataset does not exist")
    already_sealed = dataset.status == "sealed"
    ruleset = session.get(Ruleset, dataset.ruleset_id)
    snapshots = list(
        session.scalars(
            select(SourceSnapshot)
            .join(
                DatasetSource,
                DatasetSource.snapshot_id == SourceSnapshot.id,
            )
            .where(DatasetSource.dataset_id == dataset.id)
        )
    )
    if not snapshots:
        raise InvalidData("dataset has no source snapshots")
    documents = {}
    for snapshot in snapshots:
        raw = store.read_verified(snapshot.content_hash)
        if len(raw) != snapshot.byte_size:
            raise Conflict("snapshot size mismatch")
        documents[snapshot.id] = snapshot_document(raw, snapshot.media_type)
    records = list(
        session.scalars(select(KnowledgeRecord).where(KnowledgeRecord.dataset_id == dataset.id))
    )
    if not records:
        raise InvalidData("cannot seal an empty dataset")
    assertions = {}
    for assertion in session.scalars(select(FactAssertion).where(
        FactAssertion.dataset_id == dataset.id, FactAssertion.predicate == "source_payload",
    )):
        if assertion.record_id in assertions:
            raise InvalidData("record has duplicate source extraction assertions")
        assertions[assertion.record_id] = assertion
    evidence_rows = {row.id: row for row in session.scalars(select(EvidenceSpan).where(
        EvidenceSpan.dataset_id == dataset.id,
    ))}
    source_rows = {row.id: row for row in snapshots}
    normalized = []
    for record in records:
        if record.module != dataset.module:
            raise InvalidData("dataset/record module mismatch")
        assertion = assertions.get(record.id)
        if assertion is None or not same_json_value(assertion.value, record.payload):
            raise InvalidData("normalized payload does not match its extraction assertion")
        evidence = evidence_rows.get(assertion.evidence_id)
        if evidence is None or evidence.snapshot_id not in documents:
            raise InvalidData("evidence refers to a source outside its dataset")
        actual = json_pointer(documents[evidence.snapshot_id], evidence.pointer)
        if not same_json_value(evidence.excerpt, actual) or evidence.excerpt_hash != content_digest(actual):
            raise InvalidData("evidence does not match original snapshot")
        source = source_rows[evidence.snapshot_id]
        normalized.append(_normalized_fields(record, source.source_key, evidence.pointer))
    expected = _fingerprint(
        dataset.module,
        dataset.source_revision,
        dataset.parser_version,
        ruleset.scope,
        dataset.coverage,
        snapshots,
        normalized,
    )
    if expected != dataset.fingerprint:
        raise InvalidData("dataset content changed after extraction")
    if not already_sealed:
        dataset.status = "sealed"
        session.flush()
    return dataset


def implementation_hash():
    root = Path(__file__).parent
    paths = sorted([*root.rglob("*.py"), *(root / "data").glob("*.json")])
    digest = hashlib.sha256()
    for path in paths:
        if path.exists():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def publish_release(session, dataset_ids, label="sample", store=None):
    if not dataset_ids or not set(dataset_ids).issubset(MODULES):
        raise InvalidData("release has invalid or empty module mapping")
    if store is None:
        from arknights_agent.config import get_settings

        store = SnapshotStore(get_settings().snapshot_root)
    entries = {}
    for module, dataset_id in sorted(dataset_ids.items()):
        dataset = seal_dataset(session, dataset_id, store)
        if dataset.module != module:
            raise InvalidData("release/dataset module mismatch")
        ruleset = session.get(Ruleset, dataset.ruleset_id)
        sources = list(
            session.scalars(
                select(DatasetSource.snapshot_id)
                .where(DatasetSource.dataset_id == dataset_id)
                .order_by(DatasetSource.snapshot_id)
            )
        )
        entries[module] = {
            "dataset_id": dataset.id,
            "ruleset_id": dataset.ruleset_id,
            "scope": ruleset.scope,
            "coverage": dataset.coverage,
            "source_revision": dataset.source_revision,
            "source_snapshot_ids": sources,
            "parser_version": dataset.parser_version,
            "fingerprint": dataset.fingerprint,
        }
    manifest = {
        "schema_version": 1,
        "label": label,
        "application_version": __version__,
        "implementation_hash": implementation_hash(),
        "datasets": entries,
        "index": {"kind": "postgresql_exact_and_substring", "vector_index": None},
        "capabilities": {
            "field_query": True,
            "operator_question_answering": "operator" in entries,
            "recruitment_calculator": entries.get("recruitment", {})
            .get("coverage", {})
            .get("calculator_enabled", False),
            "garrison_metadata_qa": entries.get("garrison", {})
            .get("coverage", {})
            .get("metadata_qa_enabled", False),
            "garrison_verified_field_qa": entries.get("garrison", {})
            .get("coverage", {})
            .get("verified_field_qa_enabled", False),
            "garrison_cross_checked_field_qa": entries.get("garrison", {})
            .get("coverage", {})
            .get("cross_checked_field_qa_enabled", False),
            "garrison_documented_mechanics_qa": entries.get("garrison", {})
            .get("coverage", {})
            .get("documented_mechanics_qa_enabled", False),
            "garrison_mechanism_reviews": entries.get("garrison", {})
            .get("coverage", {})
            .get("mechanics_review_available", False),
            "garrison_rules_engine": False,
        },
    }
    digest = content_digest(manifest)
    existing = session.scalar(select(Release).where(Release.manifest_hash == digest))
    if existing:
        return existing
    release = Release(
        label=label,
        manifest=manifest,
        manifest_hash=digest,
        manifest_canonical=canonical_json(manifest),
    )
    session.add(release)
    session.flush()
    for module, dataset_id in dataset_ids.items():
        session.add(ReleaseDataset(release_id=release.id, module=module, dataset_id=dataset_id))
    session.flush()
    return release


def activate_release(session, release_id):
    release = resolve_release(session, release_id)
    pointer = session.scalar(
        select(ActiveRelease).where(ActiveRelease.channel == "default").with_for_update()
    )
    if pointer is None:
        pointer = ActiveRelease(channel="default", release_id=release.id)
        session.add(pointer)
    else:
        pointer.release_id = release.id
        pointer.changed_at = utcnow()
    session.flush()
    return release


def diff_releases(session, before_id, after_id):
    before, after = resolve_release(session, before_id), resolve_release(session, after_id)

    def mapping(release):
        rows = session.scalars(
            select(KnowledgeRecord).where(KnowledgeRecord.dataset_id.in_(_dataset_ids(release)))
        )
        return {(r.module, r.kind, r.native_id): r for r in rows}

    old, new = mapping(before), mapping(after)
    changes = []
    for key in sorted(set(old) | set(new)):
        left, right = old.get(key), new.get(key)
        if (
            left is None
            or right is None
            or left.payload != right.payload
            or left.name != right.name
        ):
            changes.append(
                {
                    "module": key[0],
                    "kind": key[1],
                    "native_id": key[2],
                    "change": "added"
                    if left is None
                    else "removed"
                    if right is None
                    else "modified",
                    "before_record_id": left.id if left else None,
                    "after_record_id": right.id if right else None,
                }
            )
    return {"before_release_id": before.id, "after_release_id": after.id, "changes": changes}
