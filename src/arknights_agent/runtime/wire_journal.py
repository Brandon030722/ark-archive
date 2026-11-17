"""Record public provider payloads and usage against the actual reserved task.

This observer never changes a ledger or retries a provider. Authentication
headers and configured secrets are never part of the public request payload.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from arknights_agent.runtime.budget import BudgetError
from arknights_agent.runtime.openai_responses import public_wire
from arknights_agent.runtime.provider_argument_failure import valid_argument_failure_evidence
from arknights_agent.runtime.providers import ProviderError
from arknights_agent.runtime.security import redact

_COUNTERS = {}
_COUNTER_LOCK = threading.Lock()

def recording_provider(base, ledger, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    path = output / "model-turns.jsonl"
    with _COUNTER_LOCK:
        counter = _COUNTERS.get(str(path.resolve()))
        if counter is None:
            sequence = 0
            if path.exists():
                with path.open() as stream:
                    sequence = max((json.loads(line).get("sequence", 0) for line in stream if line.strip()), default=0)
            counter = {"sequence":sequence, "lock":threading.Lock()}
            _COUNTERS[str(path.resolve())] = counter
    lock = counter["lock"]

    def append(value):
        value = redact(value)
        with path.open("a") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")

    class RecordingProvider(base):
        def complete(self, messages, tools, *, max_output_tokens, response_format=None):
            try:
                return self._complete_recorded(
                    messages,
                    tools,
                    max_output_tokens=max_output_tokens,
                    response_format=response_format,
                )
            except (ProviderError, BudgetError):
                raise
            except Exception:
                # The runner must apply its original full unknown hold/freeze
                # even if the observer loses its disk or reserved-task link.
                raise ProviderError("provider_error") from None

        def _complete_recorded(self, messages, tools, *, max_output_tokens, response_format=None):
            payload = self._request(messages, tools, max_output_tokens, response_format)
            with lock:
                with ledger._readonly() as db:
                    reserved = db.execute(
                        "SELECT r.reservation_id, a.task_id, a.public_input_sha256, a.rowid AS attempt_sequence "
                        "FROM reservation r JOIN stage40_request_attempt a "
                        "ON a.reservation_id=r.reservation_id WHERE r.state='reserved'"
                    ).fetchall()
                if len(reserved) != 1:
                    raise RuntimeError("recorded_request_needs_unique_reserved_task")
                row = dict(reserved[0])
                # The durable shared ledger also orders independent server /
                # acceptance processes. A stale process-local counter must not
                # reuse a sequence after another process made a paid call.
                counter["sequence"] = max(counter["sequence"] + 1, row.pop("attempt_sequence"))
                base_event = {
                    **row,
                    "case_id": row["task_id"],
                    "sequence": counter["sequence"],
                    "protocol": "stage40_real_web_tasks",
                    "provider": getattr(getattr(self, "_settings", None), "api_protocol", "deepseek"),
                    "model": payload.get("model"),
                }
                timestamp = datetime.now(UTC).isoformat()
                append(
                    {
                        **base_event,
                        "timestamp": timestamp,
                        "observed_at": timestamp,
                        "phase": "started",
                        "public_request": public_wire(payload),
                        "wire_sha256": hashlib.sha256(
                            json.dumps(
                                payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                            ).encode()
                        ).hexdigest(),
                    }
                )
            try:
                turn = super().complete(
                    messages,
                    tools,
                    max_output_tokens=max_output_tokens,
                    response_format=response_format,
                )
            except Exception as exc:
                with lock:
                    timestamp = datetime.now(UTC).isoformat()
                    try:
                        receipt = self.failure_receipt(exc) if hasattr(self, "failure_receipt") else None
                        evidence = None
                        try:
                            reader = getattr(self, "argument_failure_evidence", None)
                            candidate = reader(exc) if callable(reader) else None
                            if valid_argument_failure_evidence(
                                candidate, receipt, getattr(self, "model", None), self.simulation
                            ):
                                evidence = candidate
                        except Exception:
                            pass  # Optional raw evidence must not weaken billing handling.
                        append(
                            {
                                **base_event,
                                "timestamp": timestamp,
                                "observed_at": timestamp,
                                "phase": "failed",
                                "error_code": getattr(exc, "code", "provider_exception"),
                                "response_id": self.last_response_id,
                                "diagnostic": getattr(self, "last_response_diagnostic", {}),
                                "failure_receipt": receipt,
                                "argument_failure_evidence": evidence,
                            }
                        )
                    except Exception:
                        # Keep known billing evidence, but never offer another
                        # paid correction when its full raw evidence was lost.
                        self._last_argument_failure_evidence = None
                        pass  # Recording must not replace a provider's failure code.
                raise
            with lock:
                timestamp = datetime.now(UTC).isoformat()
                append(
                    {
                        **base_event,
                        "timestamp": timestamp,
                        "observed_at": timestamp,
                        "phase": "returned",
                        "raw_parsed_model_turn": public_wire(turn.model_dump()),
                        "response_id": self.last_response_id,
                        "diagnostic": getattr(self, "last_response_diagnostic", {}),
                    }
                )
            return turn

    return RecordingProvider
