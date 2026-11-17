"""Durable node-call receipts. Checkpoint replay never repeats a paid request.

This database is separate from the financial ledger. A started call without a
saved outcome is deliberately NOT retried, even if the process died before
sending it. Only the original ledger can settle or freeze a reservation.
"""
import json
import sqlite3

from arknights_agent import services


class GraphRecoveryRequired(RuntimeError):
    pass


class GraphCallJournal:
    def __init__(self, path):
        self.path = path
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS graph_model_call ('
                       'call_key TEXT PRIMARY KEY, request_hash TEXT NOT NULL, outcome TEXT)')

    def claim(self, key, request):
        digest = services.content_digest(request)
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT request_hash,outcome FROM graph_model_call WHERE call_key=?',
                             (key,)).fetchone()
            if row:
                if row[0] != digest or row[1] is None:
                    raise GraphRecoveryRequired('graph_call_recovery_required')
                return json.loads(row[1])
            db.execute('INSERT INTO graph_model_call VALUES (?,?,NULL)', (key, digest))
        return None

    def complete(self, key, outcome):
        with sqlite3.connect(self.path) as db:
            changed = db.execute('UPDATE graph_model_call SET outcome=? WHERE call_key=? AND outcome IS NULL',
                                 (services.canonical_json(outcome), key)).rowcount
            if changed != 1:
                raise GraphRecoveryRequired('graph_call_receipt_conflict')
