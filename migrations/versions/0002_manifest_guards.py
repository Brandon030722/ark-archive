"""Validate all dataset manifest fields and block TRUNCATE of versioned data.

Revision ID: 0002
Revises: 0001
"""

from alembic import op
from sqlalchemy import text

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

VALIDATE_RELEASE = "CREATE OR REPLACE FUNCTION ark_validate_release() RETURNS trigger LANGUAGE plpgsql AS $$\n    DECLARE item record; ds dataset%ROWTYPE; rs ruleset%ROWTYPE; sources jsonb;\n    BEGIN\n      IF NEW.manifest IS DISTINCT FROM NEW.manifest_canonical::jsonb\n        OR NEW.manifest_hash <> encode(sha256(convert_to(NEW.manifest_canonical,'UTF8')),'hex')\n        OR jsonb_typeof(NEW.manifest->'datasets') IS DISTINCT FROM 'object'\n        OR NOT EXISTS(SELECT 1 FROM jsonb_each(NEW.manifest->'datasets')) THEN\n        RAISE EXCEPTION 'invalid release manifest/hash' USING ERRCODE='23514';\n      END IF;\n      IF (SELECT count(*) FROM release_dataset WHERE release_id=NEW.id)\n        <> (SELECT count(*) FROM jsonb_each(NEW.manifest->'datasets')) THEN\n        RAISE EXCEPTION 'release membership incomplete' USING ERRCODE='23514';\n      END IF;\n      FOR item IN SELECT key,value FROM jsonb_each(NEW.manifest->'datasets') LOOP\n        SELECT * INTO ds FROM dataset WHERE id=item.value->>'dataset_id';\n        SELECT * INTO rs FROM ruleset WHERE id=ds.ruleset_id;\n        SELECT coalesce(jsonb_agg(snapshot_id ORDER BY snapshot_id),'[]'::jsonb)\n          INTO sources FROM dataset_source WHERE dataset_id=ds.id;\n        IF ds.id IS NULL OR ds.status<>'sealed' OR ds.module<>item.key\n          OR item.value->>'ruleset_id' IS DISTINCT FROM ds.ruleset_id\n          OR item.value->>'source_revision' IS DISTINCT FROM ds.source_revision\n          OR item.value->>'parser_version' IS DISTINCT FROM ds.parser_version\n          OR item.value->>'fingerprint' IS DISTINCT FROM ds.fingerprint\n          OR item.value->'coverage' IS DISTINCT FROM ds.coverage\n          OR item.value->'scope' IS DISTINCT FROM rs.scope\n          OR item.value->'source_snapshot_ids' IS DISTINCT FROM sources\n          OR NOT EXISTS(SELECT 1 FROM release_dataset\n            WHERE release_id=NEW.id AND module=item.key AND dataset_id=ds.id) THEN\n          RAISE EXCEPTION 'release manifest does not match sealed datasets' USING ERRCODE='23514';\n        END IF;\n      END LOOP;\n      RETURN NEW;\n    END $$"

PREVIOUS_VALIDATE_RELEASE = "CREATE OR REPLACE FUNCTION ark_validate_release() RETURNS trigger LANGUAGE plpgsql AS $$\n    DECLARE item record; ds dataset%ROWTYPE; rs ruleset%ROWTYPE; sources jsonb;\n    BEGIN\n      IF NEW.manifest IS DISTINCT FROM NEW.manifest_canonical::jsonb\n        OR NEW.manifest_hash <> encode(sha256(convert_to(NEW.manifest_canonical,'UTF8')),'hex')\n        OR jsonb_typeof(NEW.manifest->'datasets') IS DISTINCT FROM 'object'\n        OR NOT EXISTS(SELECT 1 FROM jsonb_each(NEW.manifest->'datasets')) THEN\n        RAISE EXCEPTION 'invalid release manifest/hash' USING ERRCODE='23514';\n      END IF;\n      IF (SELECT count(*) FROM release_dataset WHERE release_id=NEW.id)\n        <> (SELECT count(*) FROM jsonb_each(NEW.manifest->'datasets')) THEN\n        RAISE EXCEPTION 'release membership incomplete' USING ERRCODE='23514';\n      END IF;\n      FOR item IN SELECT key,value FROM jsonb_each(NEW.manifest->'datasets') LOOP\n        SELECT * INTO ds FROM dataset WHERE id=item.value->>'dataset_id';\n        SELECT * INTO rs FROM ruleset WHERE id=ds.ruleset_id;\n        SELECT coalesce(jsonb_agg(snapshot_id ORDER BY snapshot_id),'[]'::jsonb)\n          INTO sources FROM dataset_source WHERE dataset_id=ds.id;\n        IF ds.id IS NULL OR ds.status<>'sealed' OR ds.module<>item.key\n          OR item.value->>'ruleset_id' IS DISTINCT FROM ds.ruleset_id\n          OR item.value->'coverage' IS DISTINCT FROM ds.coverage\n          OR item.value->'scope' IS DISTINCT FROM rs.scope\n          OR item.value->'source_snapshot_ids' IS DISTINCT FROM sources\n          OR NOT EXISTS(SELECT 1 FROM release_dataset\n            WHERE release_id=NEW.id AND module=item.key AND dataset_id=ds.id) THEN\n          RAISE EXCEPTION 'release manifest does not match sealed datasets' USING ERRCODE='23514';\n        END IF;\n      END LOOP;\n      RETURN NEW;\n    END $$"

PROTECTED_TABLES = (
    "source_snapshot",
    "ruleset",
    "entity",
    "release_manifest",
    "dataset",
    "release_dataset",
    "dataset_source",
    "knowledge_record",
    "evidence_span",
    "record_evidence",
    "fact_assertion",
    "effect_rule",
    "operator",
    "skill_level",
    "talent_variant",
    "module_phase",
    "recruit_pool_member",
    "garrison_unit",
)


def upgrade():
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return
    connection.execute(text(VALIDATE_RELEASE))
    for table in PROTECTED_TABLES:
        connection.execute(
            text(
                f"CREATE TRIGGER no_truncate_{table} BEFORE TRUNCATE ON {table} FOR EACH STATEMENT EXECUTE FUNCTION ark_immutable()"
            )
        )


def downgrade():
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return
    for table in PROTECTED_TABLES:
        connection.execute(text(f"DROP TRIGGER no_truncate_{table} ON {table}"))
    connection.execute(text(PREVIOUS_VALIDATE_RELEASE))
