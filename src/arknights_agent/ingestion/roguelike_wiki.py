"""Append reviewed immutable PRTS revisions to a client-data release."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

from arknights_agent.errors import InvalidData
from arknights_agent.ingestion.contracts import NormalizedRecord, SampleBatch, SnapshotInput

POLICY_PATH = Path(__file__).parents[1] / "data" / "roguelike_wiki.json"
CONTENT_POINTER = "/query/pages/0/revisions/0/slots/main/content"
KIND = "roguelike_wiki"


def policy():
    return json.loads(POLICY_PATH.read_text())


def checked_page(raw, pin):
    if hashlib.sha256(raw).hexdigest() != pin["sha256"] or len(raw) != pin["byte_size"]:
        raise InvalidData("wiki source bytes differ from reviewed revision")
    doc = json.loads(raw)
    pages = doc["query"]["pages"]
    if len(pages) != 1:
        raise InvalidData("wiki revision page is ambiguous")
    page = pages[0]
    rows = page["revisions"]
    if (
        len(rows) != 1
        or page["pageid"] != pin["page_id"]
        or page["title"] != pin["title"]
        or rows[0]["revid"] != pin["revision"]
        or rows[0]["timestamp"] != pin["timestamp"]
        or rows[0]["slots"]["main"]["contentmodel"] != "wikitext"
    ):
        raise InvalidData("wiki revision identity differs")
    return rows[0]["slots"]["main"]["content"]


def append_wiki(base, raw_by_revision, *, guide_policy=None):
    spec = policy()
    snapshots, records = list(base.snapshots), list(base.records)
    for pin in spec["pins"]:
        raw = raw_by_revision[pin["revision"]]
        text = checked_page(raw, pin)
        snapshots.append(
            SnapshotInput(
                pin["source_key"],
                "community_wiki_revision",
                pin["url"],
                str(pin["revision"]),
                raw,
                "application/json",
                {
                    "authority": "community_documented_not_engine_verified",
                    "theme_id": pin["theme_id"],
                    "title": pin["title"],
                },
            )
        )
        records.append(
            NormalizedRecord(
                "roguelike",
                KIND,
                str(pin["revision"]),
                pin["title"],
                text,
                pin["source_key"],
                CONTENT_POINTER,
                pin["theme_id"],
            )
        )
    scopes, coverage = deepcopy(base.scopes), deepcopy(base.coverage)
    scopes["roguelike"]["wiki_revisions"] = [p["revision"] for p in spec["pins"]]
    if guide_policy is not None:
        scopes["roguelike"]["guide_policy_sha256"] = hashlib.sha256(guide_policy).hexdigest()
    coverage["roguelike"]["rag"] = {
        "source_pages": len(spec["pins"]),
        "reviewed_ending_routes": len(json.loads(guide_policy)["ending_routes"] if guide_policy else spec["ending_routes"]),
        "probability_engine": False,
        "source_kind": "fixed_prts_revision",
    }
    return SampleBatch(base.revision, snapshots, records, scopes, coverage)
