"""Strict on-disk contracts shared by Agent, baselines and evaluation reports."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from arknights_agent.errors import InvalidData


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Anchor(StrictModel):
    source_key: str
    source_revision: str
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    pointer: str
    value_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExpectedFact(StrictModel):
    key: str = Field(min_length=1)
    value: JsonValue
    anchors: list[Anchor] = Field(default_factory=list)


class Combination(StrictModel):
    tags: list[str]
    candidate_names: list[str]
    minimum_rarity: int | None = Field(ge=1, le=6)


class Expectation(StrictModel):
    statuses: list[str] = Field(min_length=1)
    conditions: dict[str, JsonValue] = Field(default_factory=dict)
    facts: list[ExpectedFact] = Field(default_factory=list)
    combinations: list[Combination] = Field(default_factory=list)
    required_anchors: list[Anchor] = Field(default_factory=list)
    answer_contains: list[str] = Field(default_factory=list)
    answer_not_contains: list[str] = Field(default_factory=list)


class PublicTask(StrictModel):
    fact_keys: list[str] = Field(default_factory=list)


class CaseRequest(StrictModel):
    module: Literal["operator", "recruitment", "garrison"]
    # A bounded eval request may intentionally violate the production API's
    # five-tag limit to test HTTP 422. Production schemas remain unchanged.
    tags: list[str] | None = Field(default=None, max_length=32)
    duration_minutes: int | None = None


class Case(StrictModel):
    id: str = Field(min_length=1)
    split: Literal["dev", "holdout"]
    module: Literal["operator", "recruitment", "garrison"]
    category: str = Field(min_length=1)
    question: str = Field(min_length=1, max_length=2000)
    request: CaseRequest
    release_ref: Literal["current", "previous"]
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    public: PublicTask = Field(default_factory=PublicTask)
    expected: Expectation
    review_status: Literal["source_checked_agent_authored", "human_reviewed"]


class PredictedFact(StrictModel):
    key: str = Field(min_length=1)
    value: JsonValue
    citation_indices: list[int] = Field(default_factory=list)


class Prediction(StrictModel):
    schema_version: Literal[1] = 1
    case_id: str = Field(min_length=1)
    release_id: str = Field(min_length=1)
    source_revision: str | None
    status: Literal[
        "answered",
        "computed",
        "needs_clarification",
        "unsupported",
        "needs_verification",
        "out_of_scope",
        "invalid_input",
        "error",
        "not_run",
        "pending_manual",
    ]
    conditions: dict[str, JsonValue] = Field(default_factory=dict)
    facts: list[PredictedFact] = Field(default_factory=list)
    combinations: list[Combination] = Field(default_factory=list)
    citations: list[dict[str, JsonValue]] = Field(default_factory=list)
    answer: str = ""
    trace: dict[str, JsonValue] = Field(default_factory=dict)
    latency_ms: float | None = Field(default=None, ge=0)


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def validate_prediction(value: dict) -> dict:
    if type(value.get("schema_version", 1)) is not int:
        raise InvalidData("prediction schema_version must be an integer")
    result = Prediction.model_validate(value).model_dump()
    if len({fact["key"] for fact in result["facts"]}) != len(result["facts"]):
        raise InvalidData("prediction contains duplicate fact keys")
    for fact in result["facts"]:
        indices = fact["citation_indices"]
        if any(index < 0 or index >= len(result["citations"]) for index in indices):
            raise InvalidData("prediction fact references an absent citation")
        if len(set(indices)) != len(indices):
            raise InvalidData("prediction contains duplicate fact citation references")
    combinations = result["combinations"]
    if len({tuple(sorted(row["tags"])) for row in combinations}) != len(combinations):
        raise InvalidData("prediction contains duplicate tag combinations")
    for row in combinations:
        if len(row["tags"]) != len(set(row["tags"])) or len(row["candidate_names"]) != len(
            set(row["candidate_names"])
        ):
            raise InvalidData("prediction contains duplicate tags or candidates")
    digest(result)
    return result


def load_suite(directory: str | Path) -> tuple[dict, list[dict], dict]:
    root = Path(directory)
    manifest = json.loads((root / "manifest.json").read_text())
    source_content = (root / "sources.json").read_bytes()
    sources = json.loads(source_content)
    if manifest.get("sources_sha256") != digest(sources):
        raise InvalidData("evaluation suite source inventory hash mismatch")
    content = (root / "cases.jsonl").read_bytes()
    if manifest.get("cases_sha256") != hashlib.sha256(content).hexdigest():
        raise InvalidData("evaluation suite cases hash mismatch")
    cases = [Case.model_validate_json(line).model_dump() for line in content.splitlines() if line]
    if not cases or len({case["id"] for case in cases}) != len(cases):
        raise InvalidData("evaluation suite is empty or contains duplicate IDs")
    if manifest.get("case_count") != len(cases):
        raise InvalidData("evaluation suite case count mismatch")
    for case in cases:
        tags = case["request"].get("tags")
        if tags and len(tags) > 5 and case["expected"]["statuses"] != ["invalid_input"]:
            raise InvalidData("over-limit eval tags require an explicit invalid_input expectation")
        if case["module"] != case["request"]["module"]:
            raise InvalidData("evaluation request module differs from case module")
        keys = [fact["key"] for fact in case["expected"]["facts"]]
        if len(set(keys)) != len(keys) or set(keys) != set(case["public"]["fact_keys"]):
            raise InvalidData("public fact keys do not match the requested gold field names")
    return manifest, cases, sources


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def write_jsonl(path: Path, values: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n" for value in values)
    )
