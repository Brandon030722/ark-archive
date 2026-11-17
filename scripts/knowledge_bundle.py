"""Portable PostgreSQL seed containing only the active, evidence-backed release."""
import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from sqlalchemy import create_engine, text

from arknights_agent.config import Settings
from arknights_agent.models import Base
from arknights_agent.storage import SnapshotStore

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "distribution" / "knowledge.zip"
MANIFEST = BUNDLE.with_name("knowledge.json")
ACTIVE = "SELECT release_id FROM active_release WHERE channel='default'"
DATASETS = f"SELECT dataset_id FROM release_dataset WHERE release_id IN ({ACTIVE})"
RECORDS = f"SELECT id FROM knowledge_record WHERE dataset_id IN ({DATASETS})"
ENTITIES = f"SELECT entity_id FROM knowledge_record WHERE dataset_id IN ({DATASETS})"
SOURCES = f"SELECT snapshot_id FROM dataset_source WHERE dataset_id IN ({DATASETS})"
# Explicit allowlist. QA runs, observed game states, credentials and account data are private.
TABLES = {
    "ruleset": f"id IN (SELECT ruleset_id FROM dataset WHERE id IN ({DATASETS}))",
    "entity": f"id IN ({ENTITIES})", "entity_alias": f"entity_id IN ({ENTITIES})",
    "source_snapshot": f"id IN ({SOURCES})", "dataset": f"id IN ({DATASETS})",
    **{t: f"dataset_id IN ({DATASETS})" for t in (
        "dataset_source", "knowledge_record", "evidence_span", "record_evidence", "fact_assertion", "effect_rule")},
    **{t: f"record_id IN ({RECORDS})" for t in (
        "operator", "skill_level", "talent_variant", "module_phase", "recruit_pool_member", "garrison_unit")},
    "release_manifest": f"id IN ({ACTIVE})", "release_dataset": f"release_id IN ({ACTIVE})",
    "active_release": "channel='default'",
}


def columns(table):
    return [c.name for c in Base.metadata.tables[table].columns]


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def export_bundle(cfg):
    store = SnapshotStore(cfg.snapshot_root)
    engine = create_engine(cfg.database_url, isolation_level="REPEATABLE READ")
    with tempfile.TemporaryDirectory(dir=BUNDLE.parent) as scratch, engine.begin() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        ident = db.execute(text(ACTIVE)).scalar_one()
        archive = Path(scratch) / BUNDLE.name
        counts = {}
        with ZipFile(archive, "w", compression=ZIP_DEFLATED, compresslevel=6) as bundle:
            for table, predicate in TABLES.items():
                fields = [("'staging'::text" if table == "dataset" and col == "status" else col)
                          for col in columns(table)]
                pk = ", ".join(col.name for col in Base.metadata.tables[table].primary_key)
                query = f"SELECT {', '.join(fields)} FROM {table} WHERE {predicate} ORDER BY {pk}"
                with db.connection.driver_connection.cursor() as cur, bundle.open(table + ".csv", "w") as out:
                    with cur.copy(f"COPY ({query}) TO STDOUT WITH (FORMAT CSV)") as stream:
                        for chunk in stream:
                            out.write(chunk)
                counts[table] = db.execute(text(f"SELECT count(*) FROM {table} WHERE {predicate}")).scalar_one()
            sources = [dict(row) for row in db.execute(text(
                f"SELECT source_key, url, revision, content_hash, byte_size FROM source_snapshot WHERE id IN ({SOURCES}) ORDER BY source_key, revision"
            )).mappings()]
            for sha in sorted({s["content_hash"] for s in sources}):
                bundle.writestr("snapshots/" + sha, store.read_verified(sha))
        meta = {"format": 1, "release": ident, "sha256": digest(archive), "tables": counts, "sources": sources}
        manifest = Path(scratch) / MANIFEST.name
        manifest.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
        os.replace(archive, BUNDLE)
        os.replace(manifest, MANIFEST)


def install(cfg):
    engine = create_engine(cfg.database_url)
    with engine.begin() as db:
        db.execute(text("SELECT pg_advisory_xact_lock(194507331)"))
        current = db.execute(text(ACTIVE)).scalar_one_or_none()
        if current:
            print("沿用当前资料版本", current)
            return
        if any(db.execute(text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar_one() for table in TABLES):
            raise ValueError("资料库已有数据但尚未激活。请先核对已有版本，再继续安装。")
        meta = json.loads(MANIFEST.read_text())
        if meta["format"] != 1 or set(meta["tables"]) != set(TABLES) or digest(BUNDLE) != meta["sha256"]:
            raise ValueError("资料包校验失败，请重新下载完整的项目压缩包。")
        store = SnapshotStore(cfg.snapshot_root)
        with ZipFile(BUNDLE) as bundle:
            for sha in {s["content_hash"] for s in meta["sources"]}:
                if store.put(bundle.read("snapshots/" + sha)) != sha:
                    raise ValueError("来源快照校验失败")
            for table in TABLES:
                if table == "release_manifest":
                    db.execute(text("UPDATE dataset SET status='sealed'"))
                with db.connection.driver_connection.cursor() as cur, bundle.open(table + ".csv") as src:
                    with cur.copy(f"COPY {table} ({', '.join(columns(table))}) FROM STDIN WITH (FORMAT CSV)") as stream:
                        while chunk := src.read(1024 * 1024):
                            stream.write(chunk)
                    if cur.rowcount != meta["tables"][table]:
                        raise ValueError(f"{table} 导入数量不一致")
            if db.execute(text(ACTIVE)).scalar_one() != meta["release"]:
                raise ValueError("发布版本不一致")
    print("资料已安装", meta["release"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["export", "install"])
    (export_bundle if parser.parse_args().action == "export" else install)(Settings())
