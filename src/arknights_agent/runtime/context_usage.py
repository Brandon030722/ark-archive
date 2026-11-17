"""Display the latest confirmed per-call context; never sum paid attempts."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


def _read_connection(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def context_usage(ledger_path, tasks_path, *, limit_tokens, task_id=None, conversation_id=None, legacy_limit_tokens=None):
    result = {'state': 'empty', 'task_id': task_id, 'conversation_id': conversation_id,
              'limit_tokens': limit_tokens, 'used_tokens': None, 'input_tokens': None,
              'output_tokens': None, 'latest_call_state': None,
              'basis': 'latest_confirmed_call', 'checked_at': datetime.now(UTC).isoformat()}
    if task_id is None:
        return result
    try:
        with closing(_read_connection(tasks_path)) as db:
            task = db.execute('SELECT conversation_id FROM http_chat_task WHERE task_id=?', (task_id,)).fetchone()
        if task is None or task['conversation_id'] != conversation_id:
            raise ValueError('scope unavailable')
        with closing(_read_connection(ledger_path)) as db:
            db.execute('BEGIN')
            rows = db.execute(
                'SELECT r.reservation_id,r.state,r.prompt_tokens,r.completion_tokens,r.total_tokens '
                'FROM stage40_request_attempt a LEFT JOIN reservation r '
                'ON a.reservation_id=r.reservation_id WHERE a.task_id=? ORDER BY a.rowid DESC',
                (task_id,),
            ).fetchall()
            # Each receipt belongs to the model active when it was submitted.
            confirmed = next((row for row in rows if row['state'] == 'settled'), None)
            if confirmed and legacy_limit_tokens is not None:
                result['limit_tokens'] = legacy_limit_tokens
            if confirmed and db.execute("SELECT 1 FROM sqlite_master WHERE name='provider_attempt'").fetchone():
                meta = db.execute('SELECT connection_json FROM provider_attempt WHERE reservation_id=?',
                                  (confirmed['reservation_id'],)).fetchone()
                if meta:
                    config = json.loads(meta[0])
                    recorded_limit = config['context_tokens']
                    if type(recorded_limit) is not int or not 4096 <= recorded_limit <= 1048576:
                        raise ValueError('invalid context limit')
                    result['limit_tokens'] = recorded_limit
                    result['provider'] = config['protocol']
                    result['model'] = config['model']
        if not rows:
            return result
        latest = rows[0]['state']
        if latest not in {'reserved', 'settled', 'unknown'}:
            raise ValueError('invalid call state')
        result['latest_call_state'] = latest
        result['state'] = {'reserved': 'waiting', 'settled': 'ready', 'unknown': 'unknown'}[latest]
        confirmed = next((row for row in rows if row['state'] == 'settled'), None)
        if confirmed is not None:
            prompt, completion, total = (confirmed[key] for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'))
            if (any(type(value) is not int or value < 0 or value > 2**53 - 1 for value in (prompt, completion, total))
                    or total != prompt + completion):
                raise ValueError('invalid usage')
            result.update(used_tokens=total, input_tokens=prompt, output_tokens=completion)
        return result
    except (OSError, sqlite3.Error, TypeError, ValueError, OverflowError):
        # Missing/corrupt receipts never become zero or another conversation's usage.
        return {**result, 'state': 'unavailable', 'used_tokens': None,
                'input_tokens': None, 'output_tokens': None, 'latest_call_state': None}
