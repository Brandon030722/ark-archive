"""Database-level publication invariants, including direct SQL and concurrent writes."""

from sqlalchemy import text

IMMUTABLE_TABLES = ("source_snapshot", "ruleset", "entity", "release_manifest")
DATASET_TABLES = (
    "dataset_source",
    "knowledge_record",
    "evidence_span",
    "record_evidence",
    "fact_assertion",
    "effect_rule",
)
PROJECTION_TABLES = (
    "operator",
    "skill_level",
    "talent_variant",
    "module_phase",
    "recruit_pool_member",
    "garrison_unit",
)

FUNCTIONS = [
    """CREATE FUNCTION ark_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'immutable object: %', TG_TABLE_NAME USING ERRCODE='55000'; END $$""",
    """CREATE FUNCTION ark_guard_dataset_children() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE row_data jsonb; ds_id text; state text; rec_id text;
    BEGIN
      FOR row_data IN SELECT x FROM unnest(ARRAY[
        CASE WHEN TG_OP <> 'INSERT' THEN to_jsonb(OLD) END,
        CASE WHEN TG_OP <> 'DELETE' THEN to_jsonb(NEW) END]) x WHERE x IS NOT NULL
      LOOP
        IF TG_ARGV[0] = 'projection' THEN
          rec_id := row_data->>'record_id';
          SELECT dataset_id INTO ds_id FROM knowledge_record WHERE id=rec_id;
        ELSE ds_id := row_data->>'dataset_id'; END IF;
        SELECT status INTO state FROM dataset WHERE id=ds_id FOR SHARE;
        IF state = 'sealed' THEN
          RAISE EXCEPTION 'sealed dataset is immutable' USING ERRCODE='55000';
        END IF;
      END LOOP;
      IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END $$""",
    """CREATE FUNCTION ark_guard_dataset() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE rs_module text;
    BEGIN
      IF TG_OP <> 'INSERT' AND OLD.status='sealed' THEN
        RAISE EXCEPTION 'sealed dataset is immutable' USING ERRCODE='55000';
      END IF;
      IF TG_OP='DELETE' THEN RETURN OLD; END IF;
      SELECT module INTO rs_module FROM ruleset WHERE id=NEW.ruleset_id;
      IF rs_module IS DISTINCT FROM NEW.module THEN
        RAISE EXCEPTION 'dataset/ruleset module mismatch' USING ERRCODE='23514';
      END IF;
      IF NEW.status='sealed' THEN
        IF TG_OP='INSERT' THEN
          RAISE EXCEPTION 'dataset must be created in staging' USING ERRCODE='23514';
        END IF;
        IF NOT EXISTS(SELECT 1 FROM knowledge_record WHERE dataset_id=NEW.id)
           OR NOT EXISTS(SELECT 1 FROM dataset_source WHERE dataset_id=NEW.id)
           OR EXISTS(SELECT 1 FROM knowledge_record r WHERE r.dataset_id=NEW.id
             AND (r.module<>NEW.module OR NOT EXISTS(
               SELECT 1 FROM record_evidence e WHERE e.record_id=r.id))) THEN
          RAISE EXCEPTION 'dataset has missing or mismatched records/evidence' USING ERRCODE='23514';
        END IF;
      END IF;
      RETURN NEW;
    END $$""",
    """CREATE FUNCTION ark_validate_release() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE item record; ds dataset%ROWTYPE; rs ruleset%ROWTYPE; sources jsonb;
    BEGIN
      IF NEW.manifest IS DISTINCT FROM NEW.manifest_canonical::jsonb
        OR NEW.manifest_hash <> encode(sha256(convert_to(NEW.manifest_canonical,'UTF8')),'hex')
        OR jsonb_typeof(NEW.manifest->'datasets') IS DISTINCT FROM 'object'
        OR NOT EXISTS(SELECT 1 FROM jsonb_each(NEW.manifest->'datasets')) THEN
        RAISE EXCEPTION 'invalid release manifest/hash' USING ERRCODE='23514';
      END IF;
      IF (SELECT count(*) FROM release_dataset WHERE release_id=NEW.id)
        <> (SELECT count(*) FROM jsonb_each(NEW.manifest->'datasets')) THEN
        RAISE EXCEPTION 'release membership incomplete' USING ERRCODE='23514';
      END IF;
      FOR item IN SELECT key,value FROM jsonb_each(NEW.manifest->'datasets') LOOP
        SELECT * INTO ds FROM dataset WHERE id=item.value->>'dataset_id';
        SELECT * INTO rs FROM ruleset WHERE id=ds.ruleset_id;
        SELECT coalesce(jsonb_agg(snapshot_id ORDER BY snapshot_id),'[]'::jsonb)
          INTO sources FROM dataset_source WHERE dataset_id=ds.id;
        IF ds.id IS NULL OR ds.status<>'sealed' OR ds.module<>item.key
          OR item.value->>'ruleset_id' IS DISTINCT FROM ds.ruleset_id
          OR item.value->>'source_revision' IS DISTINCT FROM ds.source_revision
          OR item.value->>'parser_version' IS DISTINCT FROM ds.parser_version
          OR item.value->>'fingerprint' IS DISTINCT FROM ds.fingerprint
          OR item.value->'coverage' IS DISTINCT FROM ds.coverage
          OR item.value->'scope' IS DISTINCT FROM rs.scope
          OR item.value->'source_snapshot_ids' IS DISTINCT FROM sources
          OR NOT EXISTS(SELECT 1 FROM release_dataset
            WHERE release_id=NEW.id AND module=item.key AND dataset_id=ds.id) THEN
          RAISE EXCEPTION 'release manifest does not match sealed datasets' USING ERRCODE='23514';
        END IF;
      END LOOP;
      RETURN NEW;
    END $$""",
    """CREATE FUNCTION ark_guard_release_member() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE expected text;
    BEGIN
      IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'release membership is immutable' USING ERRCODE='55000';
      END IF;
      SELECT manifest->'datasets'->NEW.module->>'dataset_id' INTO expected
        FROM release_manifest WHERE id=NEW.release_id;
      IF expected IS DISTINCT FROM NEW.dataset_id THEN
        RAISE EXCEPTION 'release member not declared in manifest' USING ERRCODE='23514';
      END IF;
      RETURN NEW;
    END $$""",
]


def install_guards(connection):
    if connection.dialect.name != "postgresql":
        return  # SQLite supports API fixture tests only, not production publication guarantees.
    for statement in FUNCTIONS:
        connection.execute(text(statement))
    for table in IMMUTABLE_TABLES:
        connection.execute(
            text(
                f"CREATE TRIGGER immutable_{table} BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION ark_immutable()"
            )
        )
    connection.execute(
        text(
            "CREATE TRIGGER guard_dataset BEFORE INSERT OR UPDATE OR DELETE ON dataset FOR EACH ROW EXECUTE FUNCTION ark_guard_dataset()"
        )
    )
    for table in DATASET_TABLES:
        connection.execute(
            text(
                f"CREATE TRIGGER guard_{table} BEFORE INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION ark_guard_dataset_children('dataset')"
            )
        )
    for table in PROJECTION_TABLES:
        connection.execute(
            text(
                f"CREATE TRIGGER guard_{table} BEFORE INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION ark_guard_dataset_children('projection')"
            )
        )
    connection.execute(
        text(
            "CREATE TRIGGER guard_release_member BEFORE INSERT OR UPDATE OR DELETE ON release_dataset FOR EACH ROW EXECUTE FUNCTION ark_guard_release_member()"
        )
    )
    connection.execute(
        text(
            "CREATE CONSTRAINT TRIGGER validate_release AFTER INSERT ON release_manifest DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ark_validate_release()"
        )
    )
    for table in (
        *IMMUTABLE_TABLES,
        "dataset",
        "release_dataset",
        *DATASET_TABLES,
        *PROJECTION_TABLES,
    ):
        connection.execute(
            text(
                f"CREATE TRIGGER no_truncate_{table} BEFORE TRUNCATE ON {table} FOR EACH STATEMENT EXECUTE FUNCTION ark_immutable()"
            )
        )


def drop_guards(connection):
    if connection.dialect.name == "postgresql":
        for name in (
            "ark_immutable",
            "ark_guard_dataset_children",
            "ark_guard_dataset",
            "ark_validate_release",
            "ark_guard_release_member",
        ):
            connection.execute(text(f"DROP FUNCTION IF EXISTS {name}() CASCADE"))
