"""Parser outputs; persistence and release promotion belong to the service layer."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class SnapshotInput:
    source_key: str
    source_type: str
    url: str
    revision: str
    content: bytes
    media_type: str
    metadata: dict[str, Any] = field(default_factory=dict)
    published_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class NormalizedRecord:
    module: str
    kind: str
    native_id: str
    name: str
    payload: dict[str, Any]
    source_key: str
    pointer: str
    parent_native_id: str | None = None


@dataclass(frozen=True, slots=True)
class SampleBatch:
    revision: str
    snapshots: list[SnapshotInput]
    records: list[NormalizedRecord]
    scopes: dict[str, Any]
    coverage: dict[str, Any]
