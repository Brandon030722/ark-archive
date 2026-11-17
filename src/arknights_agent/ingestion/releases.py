"""Rebuild a module input from a sealed release, without fetching mutable upstream data."""

from copy import deepcopy

from sqlalchemy import select

from arknights_agent import services
from arknights_agent.errors import InvalidData, NotFound
from arknights_agent.models import (
    DatasetSource,
    EvidenceSpan,
    FactAssertion,
    KnowledgeRecord,
    SourceSnapshot,
)

from .contracts import NormalizedRecord, SampleBatch, SnapshotInput


def export_module_batch(session, release, module, store):
    """Copy exact source bytes and extraction selectors after verifying the sealed graph.

    This is a read operation. A caller can close its transaction before downloading
    additional sources, then publish the new module alongside the original other modules.
    """
    entry = release.manifest["datasets"].get(module)
    if entry is None:
        raise NotFound("base release does not contain the requested module")
    services.seal_dataset(session, entry["dataset_id"], store)
    sources = {}
    snapshots = []
    for link, source in session.execute(
        select(DatasetSource, SourceSnapshot)
        .join(SourceSnapshot, SourceSnapshot.id == DatasetSource.snapshot_id)
        .where(DatasetSource.dataset_id == entry["dataset_id"])
        .order_by(DatasetSource.source_key)
    ):
        sources[source.id] = link.source_key
        snapshots.append(
            SnapshotInput(
                source_key=link.source_key,
                source_type=source.source_type,
                url=source.url,
                revision=source.revision,
                content=store.read_verified(source.storage_key),
                media_type=source.media_type,
                metadata=deepcopy(source.source_metadata),
                published_at=source.published_at,
            )
        )
    assertions = {}
    for assertion in session.scalars(select(FactAssertion).where(
        FactAssertion.dataset_id == entry["dataset_id"], FactAssertion.predicate == "source_payload",
    )):
        if assertion.record_id in assertions:
            raise InvalidData("base extraction has duplicate assertions")
        assertions[assertion.record_id] = assertion
    evidence_rows = {row.id: row for row in session.scalars(select(EvidenceSpan).where(
        EvidenceSpan.dataset_id == entry["dataset_id"],
    ))}
    records = []
    for record in session.scalars(
        select(KnowledgeRecord)
        .where(KnowledgeRecord.dataset_id == entry["dataset_id"])
        .order_by(KnowledgeRecord.kind, KnowledgeRecord.native_id)
    ):
        assertion = assertions.get(record.id)
        evidence = evidence_rows.get(assertion.evidence_id) if assertion else None
        if evidence is None or evidence.snapshot_id not in sources:
            raise InvalidData("base extraction is missing its source selector")
        records.append(
            NormalizedRecord(
                module=module,
                kind=record.kind,
                native_id=record.native_id,
                name=record.name,
                payload=deepcopy(record.payload),
                source_key=sources[evidence.snapshot_id],
                pointer=evidence.pointer,
                parent_native_id=record.parent_native_id,
            )
        )
    return SampleBatch(
        revision=entry["source_revision"],
        snapshots=snapshots,
        records=records,
        scopes={module: deepcopy(entry["scope"])},
        coverage={module: deepcopy(entry["coverage"])},
    )
