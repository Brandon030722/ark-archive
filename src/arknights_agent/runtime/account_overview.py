"""Read-only account display, isolated from paid requests and budget decisions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from arknights_agent.runtime.account import AccountError, read_balance

REFRESH_SECONDS = 60
MIN_REFRESH_SECONDS = 15
LOCAL_TIME = ZoneInfo("Asia/Shanghai")


class AccountBalanceCache:
    """Coalesce readers, throttle refresh clicks, and label previous values stale."""

    def __init__(self):
        self._lock = threading.Lock()
        self._identity = None
        self._attempted = None
        self._checked_at = None
        self._balance = None
        self._error = None

    def snapshot(self, key, *, refresh=False, reader=None, clock=time.monotonic):
        identity = hashlib.sha256(key.get_secret_value().encode()).digest() if key else None
        with self._lock:
            if identity != self._identity:
                self._identity = identity
                self._attempted = self._checked_at = self._balance = self._error = None
            now = clock()
            ttl = MIN_REFRESH_SECONDS if refresh else REFRESH_SECONDS
            if self._attempted is None or now - self._attempted >= ttl:
                try:
                    self._balance = (reader or read_balance)(key)
                    self._checked_at = datetime.now(UTC).isoformat()
                    self._error = None
                except AccountError as error:
                    self._error = error.code
                finally:
                    self._attempted = clock()
            return {
                "state": "stale" if self._error and self._balance else "unavailable" if self._error else "ready",
                "balance": dict(self._balance) if self._balance else None,
                "checked_at": self._checked_at,
                "error": self._error,
                "refresh_after_seconds": REFRESH_SECONDS,
            }


def local_usage(path: Path, *, now: datetime | None = None) -> dict:
    """Read existing records only; never instantiate a ledger or create a file.

    Accounted amounts are conservative local estimates, not provider invoices.
    Requests refer to the web-model attempt table; historical holds span the
    entire ledger and are kept separate from web request totals.
    """
    now = (now or datetime.now(UTC)).astimezone(LOCAL_TIME)
    days = {(now.date() - timedelta(days=i)).isoformat(): 0 for i in reversed(range(7))}
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            rows = db.execute(
                "SELECT a.started_at,r.state,r.accounted_cny,r.total_tokens "
                "FROM stage40_request_attempt a LEFT JOIN reservation r "
                "ON a.reservation_id=r.reservation_id"
            ).fetchall()
            by_provider = {}
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='provider_attempt'").fetchone():
                provider_rows = db.execute('SELECT p.connection_json,r.state,r.accounted_cny,r.reserved_cny '
                    'FROM provider_attempt p JOIN reservation r ON r.reservation_id=p.reservation_id').fetchall()
                for connection, state, cost, reserved in provider_rows:
                    name = json.loads(connection)['protocol']
                    item = by_provider.setdefault(name, {'requests':0,'accounted_cny':Decimal(0),'held_cny':Decimal(0)})
                    item['requests'] += 1
                    item['accounted_cny'] += Decimal(cost) if state == 'settled' else Decimal(0)
                    item['held_cny'] += Decimal(reserved) if state in {'unknown','reserved'} else Decimal(0)
            holds = db.execute("SELECT state,reserved_cny FROM reservation WHERE state IN ('unknown','reserved')").fetchall()
        daily_cost = Decimal(0)
        total_cost = Decimal(0)
        total_tokens = 0
        today = now.date().isoformat()
        for started, state, accounted, tokens in rows:
            timestamp = datetime.fromisoformat(started)
            if timestamp.tzinfo is None:
                raise ValueError("ambiguous timestamp")
            date = timestamp.astimezone(LOCAL_TIME).date().isoformat()
            if date in days:
                days[date] += 1
            if state == "settled":
                amount = Decimal(accounted)
                if not amount.is_finite() or amount < 0:
                    raise ValueError("invalid local amount")
                total_cost += amount
                if date == today:
                    daily_cost += amount
                if type(tokens) is not int or tokens < 0:
                    raise ValueError("invalid usage")
                total_tokens += tokens
        held = Decimal(0)
        for _, value in holds:
            amount = Decimal(value)
            if not amount.is_finite() or amount < 0:
                raise ValueError("invalid hold")
            held += amount
        return {
            "state": "ready", "timezone": "Asia/Shanghai", "checked_at": now.isoformat(),
            "web_requests_total": len(rows), "web_requests_today": days[today],
            "web_accounted_total_cny": format(total_cost, ".8f"),
            "web_accounted_today_cny": format(daily_cost, ".8f"),
            "settled_tokens_total": total_tokens,
            "configured_provider_usage": {k:{**v,"accounted_cny":str(v["accounted_cny"]),"held_cny":str(v["held_cny"])} for k,v in by_provider.items()},
            "unknown_count": sum(state == "unknown" for state, _ in holds),
            "inflight_count": sum(state == "reserved" for state, _ in holds),
            "held_cny": format(held, ".8f"),
            "days": [{"date": date, "requests": count} for date, count in days.items()],
        }
    except (OSError, sqlite3.Error, ValueError, TypeError, InvalidOperation, OverflowError):
        return {"state": "unavailable", "error": "local_usage_unavailable"}
