"""Run the Windows workbench and its private PostgreSQL installation."""
import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urlencode

import psycopg
import uvicorn

from scripts.first_run import prepare
from scripts.serve_web import build_app

LOCAL = Path(".local")
CLUSTER = LOCAL / "postgres"
DATA = CLUSTER / "data"
STOP = LOCAL / "windows.stop"
PG_USER = "ark_archive"


def pg(tool, *args):
    subprocess.run([Path(os.environ["ARK_WINDOWS_POSTGRES"]) / "bin" / f"{tool}.exe", *args],
                   stdin=subprocess.DEVNULL, check=True)


def initialize():
    CLUSTER.mkdir(parents=True, exist_ok=True)
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    connection = {"password": secrets.token_urlsafe(36), "port": port}
    # Inherit the Windows ACL already applied to .local. Python's Windows
    # mkdir(mode=0o700) replaces inherited rights when making a temp directory,
    # which PostgreSQL's restricted startup token cannot use.
    folder = CLUSTER / ("initialize-" + secrets.token_hex(12))
    folder.mkdir()
    try:
        password = folder / "password"
        password.write_text(connection["password"] + "\n", encoding="ascii")
        staging = folder / "data"
        pg("initdb", "-D", str(staging), "-U", PG_USER, "--pwfile", str(password),
           "-A", "scram-sha-256", "--locale=C", "--encoding=UTF8")
        with (staging / "postgresql.conf").open("a", encoding="ascii") as config:
            config.write(f"\nlisten_addresses = '127.0.0.1'\nport = {port}\n"
                         "shared_buffers = '32MB'\nmax_connections = 40\n")
        (staging / "ark-connection.json").write_text(json.dumps(connection), encoding="ascii")
        staging.rename(DATA)
    finally:
        shutil.rmtree(folder)
    return connection


def start_database():
    connection = (json.loads((DATA / "ark-connection.json").read_text(encoding="ascii"))
                  if DATA.exists() else initialize())
    started = subprocess.run(
        [Path(os.environ["ARK_WINDOWS_POSTGRES"]) / "bin/pg_ctl.exe", "-D", str(DATA), "status"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode
    if started:
        pg("pg_ctl", "-D", str(DATA), "-l", str(CLUSTER / "server.log"), "-w", "start")
    return connection


def prepare_database(connection):
    params = {"host": "127.0.0.1", "user": PG_USER, **connection}
    # The random password belongs to this checkout. An unrelated server on the
    # selected port cannot pass authentication.
    with psycopg.connect(**params, dbname="postgres", connect_timeout=5, autocommit=True) as db:
        if not db.execute("SELECT 1 FROM pg_database WHERE datname = 'arknights'").fetchone():
            db.execute("CREATE DATABASE arknights")
    os.environ["ARK_DATABASE_URL"] = "postgresql+psycopg:///arknights?" + urlencode(params)
    prepare()


def serve(port, open_browser):
    config = uvicorn.Config(build_app(argparse.Namespace(live=True, personal=True)),
                            host="127.0.0.1", port=port)
    server = uvicorn.Server(config)
    done = threading.Event()

    def controls():
        while not done.wait(0.2):
            if STOP.exists():
                server.should_exit = True
                return

    print(f"Ark Archive: http://127.0.0.1:{port}/", flush=True)
    print("Press Ctrl+C or run Stop-Ark-Archive.cmd to stop.", flush=True)
    with config.bind_socket() as listener:
        if open_browser:
            # Opening the browser is independent of polling for an explicit stop.
            def open_when_ready():
                while not done.wait(0.2):
                    if server.started:
                        try:
                            os.startfile(f"http://127.0.0.1:{port}/")
                        except OSError:
                            pass
                        return
            threading.Thread(target=open_when_ready, daemon=True).start()
        threading.Thread(target=controls, daemon=True).start()
        try:
            server.run(sockets=[listener])
        finally:
            done.set()
    if not server.started:
        raise SystemExit("Web server did not start.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000, choices=range(1, 65536), metavar="PORT")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    # The portable Python distribution supplies the Visual C++ runtime needed
    # by the adjacent PostgreSQL binaries on Windows installations.
    os.environ["PATH"] = os.pathsep.join((sys.base_prefix, os.environ["PATH"]))
    connection = start_database()
    try:
        prepare_database(connection)
        serve(args.port, not args.no_browser)
    except KeyboardInterrupt:
        pass
    finally:
        pg("pg_ctl", "-D", str(DATA), "-m", "fast", "-w", "stop")


if __name__ == "__main__":
    main()
