"""Publication invariants on actual PostgreSQL, using explicitly synthetic facts.

These are not Arknights correctness fixtures. The separate live-source workflow
exercises real client-table revisions without editing their values.
"""

import hashlib
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from arknights_agent import models, services
from arknights_agent.db import make_engine, session_factory
from arknights_agent.errors import Conflict, InvalidData, NotFound
from arknights_agent.ingestion.contracts import NormalizedRecord, SampleBatch, SnapshotInput
from arknights_agent.storage import SnapshotStore


def synthetic_batch(revision="synthetic-revision-a", value=1):
    """Three tiny module slices of one synthetic JSON source."""
    modules = ("operator", "recruitment", "garrison")
    document = {module: {"synthetic_fixture": True, "value": value} for module in modules}
    source = SnapshotInput(
        source_key="synthetic-fixture:source.json",
        source_type="synthetic_program_test",
        url="https://example.invalid/synthetic/source.json",
        revision=revision,
        content=json.dumps(document, sort_keys=True).encode(),
        media_type="application/json",
        metadata={"synthetic_fixture": True},
    )
    records = [
        NormalizedRecord(
            module=module,
            kind="synthetic_fixture",
            native_id=f"synthetic-{module}",
            name=f"Synthetic {module}",
            payload=document[module],
            source_key=source.source_key,
            pointer=f"/{module}",
        )
        for module in modules
    ]
    return SampleBatch(
        revision=revision,
        snapshots=[source],
        records=records,
        scopes={
            module: {
                "game": "arknights",
                "region": "CN",
                "mode": f"synthetic_{module}",
                "source_revision": revision,
                "effective_at": None,
                "mapping_status": "synthetic_fixture",
            }
            for module in modules
        },
        coverage={module: {"status": "synthetic_fixture"} for module in modules},
    )


@pytest.fixture
def pg_context(tmp_path):
    database_url = os.environ.get("ARK_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set ARK_TEST_DATABASE_URL to run PostgreSQL publication invariants")
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        pytest.fail("publication invariants require PostgreSQL, not SQLite")
    # Tests own a random schema and never truncate the user's demonstration DB.
    schema = f"quality_test_{uuid4().hex}"
    admin = make_engine(database_url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    options = f"{url.query.get('options', '')} -csearch_path={schema}".strip()
    test_url = url.update_query_dict({"options": options})
    engine = make_engine(test_url)
    try:
        config = Config("alembic.ini")
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        with session_factory(engine)() as session:
            yield SimpleNamespace(
                session=session,
                engine=engine,
                store=SnapshotStore(tmp_path / "snapshots"),
                schema=schema,
            )
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def ingest(context, batch):
    datasets = services.ingest_batch(context.session, batch, context.store)
    context.session.commit()
    return datasets


def publish(context, datasets, label="synthetic release"):
    release = services.publish_release(
        context.session,
        {module: dataset.id for module, dataset in datasets.items()},
        label=label,
        store=context.store,
    )
    context.session.commit()
    return release


def assert_sql_rejected(session, statement, parameters=None, message=None):
    with pytest.raises(DBAPIError, match=message):
        with session.begin_nested():
            session.execute(text(statement), parameters or {})


@pytest.mark.postgres
def test_staging_is_never_a_public_release(pg_context):
    datasets = ingest(pg_context, synthetic_batch())
    with pytest.raises(NotFound):
        services.resolve_release(pg_context.session)
    with pytest.raises(NotFound):
        services.resolve_release(pg_context.session, datasets["operator"].id)
    assert set(dataset.status for dataset in datasets.values()) == {"staging"}


@pytest.mark.postgres
def test_ingestion_is_idempotent_and_evidence_matches_raw_json(pg_context):
    batch = synthetic_batch()
    first = ingest(pg_context, batch)
    counts = {
        model.__tablename__: pg_context.session.scalar(select(func.count()).select_from(model))
        for model in (
            models.SourceSnapshot,
            models.Dataset,
            models.KnowledgeRecord,
            models.EvidenceSpan,
            models.RecordEvidence,
        )
    }
    second = ingest(pg_context, batch)
    assert {module: row.id for module, row in first.items()} == {
        module: row.id for module, row in second.items()
    }
    for model in (
        models.SourceSnapshot,
        models.Dataset,
        models.KnowledgeRecord,
        models.EvidenceSpan,
        models.RecordEvidence,
    ):
        assert (
            pg_context.session.scalar(select(func.count()).select_from(model))
            == counts[model.__tablename__]
        )

    release = publish(pg_context, first)
    items = services.list_records(pg_context.session, release)
    assert len(items) == 3
    for item in items:
        assert len(item["evidence_ids"]) >= 1
        evidence = services.get_evidence(pg_context.session, release, item["evidence_ids"][0])
        assert evidence["dataset_id"] == item["dataset_id"]
        assert evidence["excerpt"] == item["data"]
        assert services.content_digest(evidence["excerpt"]) == evidence["excerpt_hash"]
        snapshot = services.get_snapshot(pg_context.session, release, evidence["snapshot_id"])
        raw = pg_context.store.read_verified(snapshot["content_hash"])
        assert len(raw) == snapshot["byte_size"]
        assert hashlib.sha256(raw).hexdigest() == snapshot["content_hash"]
        assert services.json_pointer(json.loads(raw), evidence["pointer"]) == evidence["excerpt"]
    repeated_after_publication = ingest(pg_context, batch)
    assert {module: row.id for module, row in repeated_after_publication.items()} == {
        module: row.id for module, row in first.items()
    }
    assert {row.status for row in repeated_after_publication.values()} == {"sealed"}
    assert services.list_records(pg_context.session, release) == items


@pytest.mark.postgres
def test_explicit_versions_evidence_and_active_rollback_are_isolated(pg_context):
    release_a = publish(pg_context, ingest(pg_context, synthetic_batch()), "synthetic A")
    release_b = publish(
        pg_context,
        ingest(pg_context, synthetic_batch("synthetic-revision-b", 2)),
        "synthetic B",
    )
    services.activate_release(pg_context.session, release_a.id)
    pg_context.session.commit()
    old_items = services.list_records(
        pg_context.session, services.resolve_release(pg_context.session)
    )
    assert {item["data"]["value"] for item in old_items} == {1}
    services.activate_release(pg_context.session, release_b.id)
    pg_context.session.commit()
    assert services.resolve_release(pg_context.session).id == release_b.id
    assert {
        item["data"]["value"] for item in services.list_records(pg_context.session, release_b)
    } == {2}
    assert services.list_records(pg_context.session, release_a) == old_items
    old = old_items[0]
    with pytest.raises(NotFound):
        services.get_record(pg_context.session, release_b, old["id"])
    with pytest.raises(NotFound):
        services.get_evidence(pg_context.session, release_b, old["evidence_ids"][0])
    old_evidence = services.get_evidence(pg_context.session, release_a, old["evidence_ids"][0])
    with pytest.raises(NotFound):
        services.get_snapshot(pg_context.session, release_b, old_evidence["snapshot_id"])
    services.activate_release(pg_context.session, release_a.id)
    pg_context.session.commit()
    assert services.resolve_release(pg_context.session).id == release_a.id
    assert services.list_records(pg_context.session, release_a) == old_items
    assert {
        item["data"]["value"] for item in services.list_records(pg_context.session, release_b)
    } == {2}


@pytest.mark.postgres
def test_snapshot_identity_cannot_be_rewritten_or_deleted(pg_context):
    source = synthetic_batch().snapshots[0]
    snapshot = services.create_snapshot(pg_context.session, source, pg_context.store)
    pg_context.session.commit()
    for statement in (
        "UPDATE source_snapshot SET content_hash=:hash WHERE id=:id",
        "UPDATE source_snapshot SET revision='forged revision' WHERE id=:id",
        "UPDATE source_snapshot SET storage_key=:hash WHERE id=:id",
        "DELETE FROM source_snapshot WHERE id=:id",
    ):
        assert_sql_rejected(
            pg_context.session, statement, {"id": snapshot.id, "hash": "f" * 64}, "immutable"
        )


@pytest.mark.postgres
def test_published_graph_rejects_direct_updates_and_deletes(pg_context):
    datasets = ingest(pg_context, synthetic_batch())
    release = publish(pg_context, datasets)
    dataset_id = datasets["operator"].id
    immutable_statements = [
        "UPDATE dataset SET coverage='{}'::jsonb WHERE id=:dataset_id",
        "DELETE FROM dataset WHERE id=:dataset_id",
        "UPDATE dataset_source SET source_key='forged' WHERE dataset_id=:dataset_id",
        "DELETE FROM dataset_source WHERE dataset_id=:dataset_id",
        "UPDATE knowledge_record SET payload='{}'::jsonb WHERE dataset_id=:dataset_id",
        "DELETE FROM knowledge_record WHERE dataset_id=:dataset_id",
        "UPDATE evidence_span SET excerpt='{}'::jsonb WHERE dataset_id=:dataset_id",
        "DELETE FROM evidence_span WHERE dataset_id=:dataset_id",
        "UPDATE record_evidence SET dataset_id=dataset_id WHERE dataset_id=:dataset_id",
        "DELETE FROM record_evidence WHERE dataset_id=:dataset_id",
        "UPDATE fact_assertion SET value='{}'::jsonb WHERE dataset_id=:dataset_id",
        "DELETE FROM fact_assertion WHERE dataset_id=:dataset_id",
        "UPDATE ruleset SET scope='{}'::jsonb WHERE id=(SELECT ruleset_id FROM dataset WHERE id=:dataset_id)",
        "DELETE FROM ruleset WHERE id=(SELECT ruleset_id FROM dataset WHERE id=:dataset_id)",
        "UPDATE entity SET native_id=native_id WHERE id IN (SELECT entity_id FROM knowledge_record WHERE dataset_id=:dataset_id)",
        "DELETE FROM entity WHERE id IN (SELECT entity_id FROM knowledge_record WHERE dataset_id=:dataset_id)",
        "UPDATE release_manifest SET label='changed' WHERE id=:release_id",
        "DELETE FROM release_manifest WHERE id=:release_id",
        "UPDATE release_dataset SET dataset_id=dataset_id WHERE release_id=:release_id",
        "DELETE FROM release_dataset WHERE release_id=:release_id",
    ]
    for statement in immutable_statements:
        assert_sql_rejected(
            pg_context.session,
            statement,
            {"dataset_id": dataset_id, "release_id": release.id},
            "immutable",
        )


@pytest.mark.postgres
def test_published_graph_rejects_new_records_sources_and_evidence(pg_context):
    datasets = ingest(pg_context, synthetic_batch())
    release = publish(pg_context, datasets)
    dataset_id = datasets["operator"].id
    statements = [
        """INSERT INTO knowledge_record
           (id,dataset_id,entity_id,module,kind,native_id,name,payload)
           SELECT :new_id,dataset_id,entity_id,module,kind,'new synthetic id',name,payload
           FROM knowledge_record WHERE dataset_id=:dataset_id LIMIT 1""",
        """INSERT INTO evidence_span (id,dataset_id,snapshot_id,pointer,excerpt,excerpt_hash)
           SELECT :new_id,dataset_id,snapshot_id,pointer,excerpt,excerpt_hash
           FROM evidence_span WHERE dataset_id=:dataset_id LIMIT 1""",
        """INSERT INTO dataset_source (dataset_id,snapshot_id,source_key)
           SELECT dataset_id,snapshot_id,'new synthetic key' FROM dataset_source
           WHERE dataset_id=:dataset_id LIMIT 1""",
        """INSERT INTO record_evidence(record_id,evidence_id,dataset_id)
           SELECT record_id,evidence_id,dataset_id FROM record_evidence
           WHERE dataset_id=:dataset_id LIMIT 1""",
        """INSERT INTO effect_rule(id,dataset_id,rule_key,expression,review_status)
           VALUES (:new_id,:dataset_id,'synthetic rule','{}'::jsonb,'unverified')""",
        """INSERT INTO fact_assertion
           (id,dataset_id,record_id,predicate,value,evidence_id,review_status,adopted_at)
           SELECT :new_id,dataset_id,record_id,'synthetic','{}'::jsonb,evidence_id,
           'source_extracted',now() FROM record_evidence WHERE dataset_id=:dataset_id LIMIT 1""",
    ]
    for statement in statements:
        assert_sql_rejected(
            pg_context.session,
            statement,
            {"dataset_id": dataset_id, "new_id": uuid4().hex},
            "immutable",
        )
    assert_sql_rejected(
        pg_context.session,
        "INSERT INTO release_dataset(release_id,module,dataset_id) VALUES(:release_id,'extra',:dataset_id)",
        {"release_id": release.id, "dataset_id": dataset_id},
        "not declared",
    )


@pytest.mark.postgres
def test_truncate_cascade_cannot_erase_published_graph(pg_context):
    datasets = ingest(pg_context, synthetic_batch())
    release = publish(pg_context, datasets)
    before = services.list_records(pg_context.session, release)
    assert_sql_rejected(pg_context.session, "TRUNCATE dataset CASCADE")
    assert services.list_records(pg_context.session, release) == before
    assert pg_context.session.scalar(select(func.count()).select_from(models.Release)) == 1
    assert pg_context.session.scalar(select(func.count()).select_from(models.EvidenceSpan)) == 3


@pytest.mark.postgres
def test_published_projections_reject_new_rows(pg_context):
    datasets = ingest(pg_context, synthetic_batch())
    publish(pg_context, datasets)
    record = pg_context.session.scalar(
        select(models.KnowledgeRecord).where(
            models.KnowledgeRecord.dataset_id == datasets["operator"].id,
        )
    )
    # Each statement supplies schema-valid synthetic projection fields. Dataset
    # publication must reject it before any projection can alter public facts.
    statements = [
        """INSERT INTO operator(record_id,rarity,profession,position,phases)
           VALUES(:record,1,'SYNTHETIC','SYNTHETIC','[]'::jsonb)""",
        """INSERT INTO skill_level(record_id,rank,skill_type,sp_type,blackboard)
           VALUES(:record,1,'SYNTHETIC','"SYNTHETIC"'::jsonb,'[]'::jsonb)""",
        """INSERT INTO talent_variant(record_id,unlock_condition,blackboard)
           VALUES(:record,'{}'::jsonb,'[]'::jsonb)""",
        """INSERT INTO module_phase(record_id,equip_level,attribute_blackboard,parts)
           VALUES(:record,1,'[]'::jsonb,'[]'::jsonb)""",
        """INSERT INTO recruit_pool_member(record_id,rarity,tags)
           VALUES(:record,1,'[]'::jsonb)""",
        """INSERT INTO garrison_unit(record_id,activity_id,chess_id,data)
           VALUES(:record,'synthetic','synthetic','{}'::jsonb)""",
    ]
    for statement in statements:
        assert_sql_rejected(pg_context.session, statement, {"record": record.id}, "immutable")


@pytest.mark.postgres
def test_published_projections_reject_updates_and_deletes(pg_context):
    datasets = ingest(pg_context, synthetic_batch())
    record = pg_context.session.scalar(
        select(models.KnowledgeRecord).where(
            models.KnowledgeRecord.dataset_id == datasets["operator"].id,
        )
    )
    # These deliberately synthetic projection rows test table guards only. They
    # are not claims about a valid game entity or executable domain rules.
    pg_context.session.add_all(
        [
            models.Operator(
                record_id=record.id,
                rarity=1,
                profession="SYNTHETIC",
                position="SYNTHETIC",
                phases=[],
            ),
            models.SkillLevel(
                record_id=record.id,
                rank=1,
                skill_type="SYNTHETIC",
                sp_type="SYNTHETIC",
                blackboard=[],
            ),
            models.TalentVariant(record_id=record.id, unlock_condition={}, blackboard=[]),
            models.ModulePhase(
                record_id=record.id, equip_level=1, attribute_blackboard=[], parts=[]
            ),
            models.RecruitPoolMember(record_id=record.id, rarity=1, tags=[]),
            models.GarrisonUnit(
                record_id=record.id, activity_id="synthetic", chess_id="synthetic", data={}
            ),
        ]
    )
    pg_context.session.commit()
    publish(pg_context, datasets)
    for table in (
        "operator",
        "skill_level",
        "talent_variant",
        "module_phase",
        "recruit_pool_member",
        "garrison_unit",
    ):
        for statement in (
            f"UPDATE {table} SET record_id=record_id WHERE record_id=:record",
            f"DELETE FROM {table} WHERE record_id=:record",
        ):
            assert_sql_rejected(pg_context.session, statement, {"record": record.id}, "immutable")


@pytest.mark.postgres
def test_cross_dataset_evidence_and_ruleset_are_rejected(pg_context):
    datasets = ingest(pg_context, synthetic_batch())
    session = pg_context.session
    operator_id = datasets["operator"].id
    recruitment_id = datasets["recruitment"].id
    record = session.scalar(
        select(models.KnowledgeRecord).where(models.KnowledgeRecord.dataset_id == operator_id)
    )
    foreign_evidence = session.scalar(
        select(models.EvidenceSpan).where(models.EvidenceSpan.dataset_id == recruitment_id)
    )
    assert_sql_rejected(
        session,
        "INSERT INTO record_evidence(record_id,evidence_id,dataset_id) VALUES(:record,:evidence,:dataset)",
        {"record": record.id, "evidence": foreign_evidence.id, "dataset": operator_id},
    )
    assert_sql_rejected(
        session,
        "UPDATE dataset SET ruleset_id=:ruleset WHERE id=:dataset",
        {"ruleset": datasets["recruitment"].ruleset_id, "dataset": operator_id},
        "module mismatch",
    )


@pytest.mark.postgres
@pytest.mark.parametrize(
    ("field", "false_value"),
    [
        ("source_snapshot_ids", []),
        ("source_revision", "synthetic forged source revision"),
        ("parser_version", "synthetic forged parser version"),
        ("fingerprint", "f" * 64),
        ("coverage", {"status": "synthetic forged coverage"}),
        ("scope", {"mode": "synthetic forged scope"}),
        ("ruleset_id", "0" * 32),
    ],
)
def test_manifest_dataset_mismatch_is_rejected_at_commit(pg_context, field, false_value):
    datasets = ingest(pg_context, synthetic_batch())
    release = services.publish_release(
        pg_context.session,
        {module: dataset.id for module, dataset in datasets.items()},
        store=pg_context.store,
    )
    # Insert a separate false manifest with valid canonical hash. A field
    # mismatch must fail independently of JSON/hash syntax and FK membership.
    manifest = json.loads(services.canonical_json(release.manifest))
    manifest["datasets"]["operator"][field] = false_value
    canonical = services.canonical_json(manifest)
    forged = models.Release(
        label="synthetic invalid source mapping",
        manifest=manifest,
        manifest_canonical=canonical,
        manifest_hash=hashlib.sha256(canonical.encode()).hexdigest(),
    )
    pg_context.session.add(forged)
    pg_context.session.flush()
    pg_context.session.add_all(
        [
            models.ReleaseDataset(release_id=forged.id, module=module, dataset_id=dataset.id)
            for module, dataset in datasets.items()
        ]
    )
    with pytest.raises(DBAPIError, match="does not match"):
        pg_context.session.commit()
    pg_context.session.rollback()
    assert pg_context.session.scalar(select(func.count()).select_from(models.Release)) == 0
    assert {row.status for row in pg_context.session.scalars(select(models.Dataset))} == {"staging"}


@pytest.mark.postgres
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE knowledge_record SET payload='{}'::jsonb WHERE dataset_id=:dataset",
        "UPDATE evidence_span SET excerpt='{}'::jsonb WHERE dataset_id=:dataset",
        "UPDATE dataset SET coverage='{}'::jsonb WHERE id=:dataset",
    ],
)
def test_staging_content_tampering_cannot_be_published(pg_context, statement):
    datasets = ingest(pg_context, synthetic_batch())
    pg_context.session.execute(text(statement), {"dataset": datasets["operator"].id})
    pg_context.session.commit()
    with pytest.raises((InvalidData, DBAPIError)):
        services.publish_release(
            pg_context.session,
            {module: dataset.id for module, dataset in datasets.items()},
            store=pg_context.store,
        )
        # Publication is transaction-bound: deferred PostgreSQL constraints are
        # part of validation and may reject the candidate at caller commit.
        pg_context.session.commit()
    pg_context.session.rollback()
    assert pg_context.session.scalar(select(func.count()).select_from(models.Release)) == 0
    assert {row.status for row in pg_context.session.scalars(select(models.Dataset))} == {"staging"}


@pytest.mark.postgres
def test_corrupt_snapshot_blocks_publication_atomically(pg_context):
    datasets = ingest(pg_context, synthetic_batch())
    snapshot = pg_context.session.scalar(select(models.SourceSnapshot))
    pg_context.store.path_for(snapshot.content_hash).write_bytes(b"synthetic damaged bytes")
    with pytest.raises((Conflict, InvalidData)):
        services.publish_release(
            pg_context.session,
            {module: dataset.id for module, dataset in datasets.items()},
            store=pg_context.store,
        )
    pg_context.session.rollback()
    assert pg_context.session.scalar(select(func.count()).select_from(models.Release)) == 0
    assert {row.status for row in pg_context.session.scalars(select(models.Dataset))} == {"staging"}


@pytest.mark.postgres
def test_corrupted_sealed_source_blocks_a_new_release(pg_context):
    datasets = ingest(pg_context, synthetic_batch())
    old_release = publish(pg_context, datasets, "synthetic before corruption")
    snapshot = pg_context.session.scalar(select(models.SourceSnapshot))
    pg_context.store.path_for(snapshot.content_hash).write_bytes(b"damaged after publication")
    with pytest.raises((Conflict, InvalidData)):
        services.publish_release(
            pg_context.session,
            {module: dataset.id for module, dataset in datasets.items()},
            label="synthetic after corruption",
            store=pg_context.store,
        )
    pg_context.session.rollback()
    assert pg_context.session.scalar(select(func.count()).select_from(models.Release)) == 1
    assert services.resolve_release(pg_context.session, old_release.id).id == old_release.id
    assert {row.status for row in pg_context.session.scalars(select(models.Dataset))} == {"sealed"}
