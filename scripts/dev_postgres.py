"""Manage only this checkout's private, loopback-only PostgreSQL cluster."""

import argparse
import os
import shutil
import subprocess
from pathlib import Path

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local" / "postgres"
DATA = LOCAL / "data"
SOCKET = LOCAL / "socket"
PORT = 55432


def binary(name: str) -> str:
    bundled = ROOT / ".local/tools/Postgres.app/Contents/Versions/16/bin" / name
    if bundled.is_file():
        return str(bundled)
    found = shutil.which(name)
    if found:
        return found
    candidate = Path("/Applications/Postgres.app/Contents/Versions/16/bin") / name
    if candidate.exists():
        return str(candidate)
    raise SystemExit("PostgreSQL binaries unavailable; use compose.yaml or set PATH")


def run(*args: str, check=True):
    return subprocess.run(args, check=check, cwd=ROOT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "status"])
    action = parser.parse_args().action
    if action != "start":
        run(binary("pg_ctl"), "-D", str(DATA), action, check=False)
        return
    LOCAL.mkdir(parents=True, exist_ok=True)
    SOCKET.mkdir(exist_ok=True)
    os.chmod(SOCKET, 0o700)
    if not (DATA / "PG_VERSION").exists():
        run(binary("initdb"), "-D", str(DATA), "-A", "trust", "--encoding=UTF8", "--locale=C")
        escaped_socket = str(SOCKET).replace("'", "''")
        listen = "" if os.environ.get("ARK_PRIVATE_CLUSTER") == "1" else "127.0.0.1"
        with (DATA / "postgresql.conf").open("a") as handle:
            handle.write(
                f"\nlisten_addresses = '{listen}'\nport = {PORT}\n"
                f"unix_socket_directories = '{escaped_socket}'\n"
                "shared_buffers = '32MB'\nmax_connections = 40\n"
            )
    status = subprocess.run([binary("pg_ctl"), "-D", str(DATA), "status"], capture_output=True)
    if status.returncode:
        run(binary("pg_ctl"), "-D", str(DATA), "-l", str(LOCAL / "server.log"), "-w", "start")
    # Use this cluster's Unix socket, never an unrelated server on the chosen TCP port.
    with psycopg.connect(host=str(SOCKET), port=PORT, dbname="postgres", autocommit=True) as conn:
        for name in ("arknights", "arknights_test"):
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
            ).fetchone()
            if not exists:
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    print(f"PostgreSQL ready: {DATA}")


if __name__ == "__main__":
    main()
