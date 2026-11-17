"""Exercise a newly seeded release in a disposable database, without a model."""
import json
import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from arknights_agent.api import create_app
from arknights_agent.config import Settings
from arknights_agent.runtime.budget import BudgetLedger
from scripts.first_run import ensure_book
from scripts.knowledge_bundle import MANIFEST, install


def main():
    # This smoke test writes a QA run. Require an explicitly designated test DB.
    if not os.environ.get("ARK_TEST_DATABASE_URL") or os.environ.get("ARK_DATABASE_URL") != os.environ["ARK_TEST_DATABASE_URL"]:
        raise SystemExit("Set ARK_DATABASE_URL and ARK_TEST_DATABASE_URL to the same disposable database.")
    manifest = json.loads(MANIFEST.read_text())
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        cfg = Settings(_env_file=None, model_live_enabled=False, model_budget_path=root / "ledger.sqlite3",
                       credential_store_path=root / "vault/key.enc", credential_master_key_path=root / "master.key")
        ensure_book(cfg.model_budget_path)
        book = BudgetLedger(cfg.model_budget_path)
        before = cfg.model_budget_path.read_bytes()
        with TestClient(create_app(cfg)) as client:
            assert client.get("/").is_success
            assert client.get("/v1/releases/active").json()["id"] == manifest["release"]
            result = client.post("/v1/recruitment/calculate", json={
                "tags": ["高级资深干员", "位移"], "duration_minutes": 540}).raise_for_status().json()
            selected = next(row for row in result["combinations"] if len(row["selected_tags"]) == 2)
            assert [c["name"] for c in selected["candidates"]] == ["温蒂"]
            item = selected["candidates"][0]
            citation = next(c for c in result["citations"] if c["record_id"] == item["record_id"])
            assert client.get("/v1/evidence/" + citation["evidence_id"]).is_success
            assert client.get("/v1/snapshots/" + citation["snapshot_id"]).is_success
        install(cfg)
        ensure_book(cfg.model_budget_path)
        with TestClient(create_app(cfg)) as restarted:
            assert restarted.get("/v1/qa/runs/" + result["run_id"], params={"release_id": result["release_id"]}).is_success
        assert cfg.model_budget_path.read_bytes() == before and book.status()["reservation_count"] == 0
        with create_engine(cfg.database_url).connect() as db:
            for name, count in manifest["tables"].items():
                assert db.execute(text(f"SELECT count(*) FROM {name}")).scalar_one() == count, name
        print("Validated release rows, recruitment, citation, saved run after restart and zero model usage.")


if __name__ == "__main__":
    main()
