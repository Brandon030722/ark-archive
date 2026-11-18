"""Prepare an independent installation without seeding any personal history."""
import os
import tempfile
from pathlib import Path
from urllib.parse import urlencode

from arknights_agent.cli import migrate
from arknights_agent.config import Settings
from arknights_agent.runtime.budget import BudgetLedger
from scripts.dev_postgres import PORT, SOCKET
from scripts.knowledge_bundle import install


def configure_local_database():
    # Use the private cluster even if another checkout listens on TCP 55432.
    os.environ["ARK_DATABASE_URL"] = "postgresql+psycopg:///arknights?" + urlencode(
        {"host": str(SOCKET), "port": PORT}
    )


def ensure_book(path: Path):
    if path.exists():
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=path.parent) as tmp:
        new = Path(tmp) / "ledger.sqlite3"
        BudgetLedger(new)
        new.chmod(0o600)
        try:
            os.link(new, path)
        except FileExistsError:
            pass


def prepare():
    migrate()
    cfg = Settings()
    install(cfg)
    ensure_book(cfg.model_budget_path)


if __name__ == "__main__":
    configure_local_database()
    prepare()
