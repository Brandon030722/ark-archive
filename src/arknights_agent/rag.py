"""Chinese lexical RAG over release-pinned source passages.

BM25 retrieves evidence, not truth. Character offsets use Python Unicode code
points in the untouched MediaWiki content field; citations always replay bytes.
"""

import re
from collections import Counter
from math import log

from sqlalchemy import select

from arknights_agent import services
from arknights_agent.errors import Conflict, InvalidData
from arknights_agent.ingestion.roguelike_wiki import CONTENT_POINTER, KIND, checked_page, policy
from arknights_agent.models import Dataset, KnowledgeRecord
from arknights_agent.wiki_text import plain


def tokens(text):
    out = re.findall(r"[a-z0-9_]+", text.lower())
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        out.extend(run[i : i + 2] for i in range(len(run) - 1))
        if len(run) == 1:
            out.append(run)
    return out


def passages(text, size=1000):
    # Each block has exact offsets and explicit adjacency. Overlap protects
    # ordinary retrieval; complete routes/menus use separately checked spans.
    for start in range(0, len(text), size - 180):
        end = min(start + size, len(text))
        yield start, end
        if end == len(text):
            break


def pages(session, release, store, theme_id=None):
    entry = release.manifest["datasets"].get("roguelike", {})
    if services.content_digest(release.manifest) != release.manifest_hash:
        raise Conflict("RAG manifest differs")
    dataset = session.get(Dataset, entry.get("dataset_id"))
    if (
        dataset is None
        or dataset.module != "roguelike"
        or dataset.status != "sealed"
        or dataset.source_revision != entry.get("source_revision")
        or dataset.fingerprint != entry.get("fingerprint")
    ):
        raise Conflict("RAG release/dataset identity differs")
    revisions = entry.get("scope", {}).get("wiki_revisions", [])
    pins = {str(p["revision"]): p for p in policy()["pins"] if p["revision"] in revisions}
    rows = session.scalars(
        select(KnowledgeRecord).where(
            KnowledgeRecord.dataset_id == entry.get("dataset_id"),
            KnowledgeRecord.kind == KIND,
        )
    ).all()
    if {r.native_id for r in rows} != set(pins) or len(rows) != len(pins):
        raise Conflict("RAG published source membership differs")
    for row in rows:
        pin = pins[row.native_id]
        if theme_id and theme_id != pin["theme_id"]:
            continue
        record = services.get_record(session, release, row.id)
        if len(record["evidence_ids"]) != 1:
            raise Conflict("RAG source evidence is ambiguous")
        evidence = services.get_evidence(session, release, record["evidence_ids"][0])
        snapshot = services.get_snapshot(session, release, evidence["snapshot_id"])
        expected = {
            "source_key": pin["source_key"],
            "source_type": "community_wiki_revision",
            "url": pin["url"],
            "revision": str(pin["revision"]),
            "content_hash": pin["sha256"],
            "byte_size": pin["byte_size"],
            "media_type": "application/json",
        }
        if (
            any(snapshot.get(k) != v for k, v in expected.items())
            or snapshot["id"] not in entry["source_snapshot_ids"]
            or evidence["pointer"] != CONTENT_POINTER
            or evidence["source_revision"] != str(pin["revision"])
            or evidence["source_url"] != pin["url"]
            or evidence["dataset_id"] != entry["dataset_id"]
            or record["parent_native_id"] != pin["theme_id"]
            or record["name"] != pin["title"]
        ):
            raise Conflict("RAG source identity differs")
        text = checked_page(store.read_verified(pin["sha256"]), pin)
        if (
            not services.same_json_value(text, record["data"])
            or not services.same_json_value(text, evidence["excerpt"])
            or services.content_digest(text) != evidence["excerpt_hash"]
        ):
            raise Conflict("RAG original content differs from stored evidence")
        yield {
            "pin": pin,
            "record": record,
            "evidence": evidence,
            "snapshot": snapshot,
            "text": text,
        }


def citation(page, start, end, ident="c1"):
    text, pin, snap = page["text"], page["pin"], page["snapshot"]
    if not 0 <= start < end <= len(text):
        raise InvalidData("invalid RAG source span")
    quote = text[start:end]
    return {
        "id": ident,
        "record_id": page["record"]["id"],
        "dataset_id": page["record"]["dataset_id"],
        "evidence_id": page["evidence"]["id"],
        "snapshot_id": snap["id"],
        "snapshot_hash": pin["sha256"],
        "source_url": pin["url"],
        "page_url": f"https://prts.wiki/index.php?oldid={pin['revision']}",
        "source_revision": str(pin["revision"]),
        "source_media_type": "application/json",
        "pointer": CONTENT_POINTER,
        "evidence_pointer": CONTENT_POINTER,
        "excerpt_hash": page["evidence"]["excerpt_hash"],
        "selector_semantics": "RFC6901_then_Unicode_codepoint_slice",
        "start": start,
        "end": end,
        "field_value": quote,
        "field_hash": services.content_digest(quote),
        "verification_status": "fixed_community_wiki_passage",
    }


def retrieve(session, release, store, query, theme_id=None, limit=5):
    if not isinstance(query, str) or not query.strip() or len(query) > 2000 or not 1 <= limit <= 10:
        raise InvalidData("invalid RAG query")
    from arknights_agent.roguelike_sources import THEME_NAMES

    if theme_id is not None and theme_id not in THEME_NAMES:
        raise InvalidData("unknown RAG theme")
    docs, df = [], Counter()
    for page in pages(session, release, store, theme_id):
        for start, end in passages(page["text"]):
            tf = Counter(tokens(plain(page["text"][start:end])))
            docs.append((page, start, end, tf, sum(tf.values())))
            df.update(tf.keys())
    query_terms = set(tokens(query))
    avg = sum(d[4] for d in docs) / max(len(docs), 1)
    scored = []
    for page, start, end, tf, length in docs:
        score = sum(
            log(1 + (len(docs) - df[t] + 0.5) / (df[t] + 0.5))
            * tf[t]
            * 2.2
            / (tf[t] + 1.2 * (0.25 + 0.75 * length / max(avg, 1)))
            for t in query_terms
            if tf[t]
        )
        if score > 0:
            scored.append((score, page, start, end))
    scored.sort(key=lambda x: (-x[0], x[1]["pin"]["revision"], x[2]))
    hits = []
    for score, page, start, end in scored[:limit]:
        hits.append(
            {
                "title": page["pin"]["title"],
                "theme_id": page["pin"]["theme_id"],
                "score": round(score, 4),
                "text": plain(page["text"][start:end])[:400],
                "preview_truncated": len(plain(page["text"][start:end])) > 400,
                "citation": citation(page, start, end, f"c{len(hits) + 1}"),
                "complete_document": False,
                "adjacent_context_may_be_required": True,
            }
        )
    return {
        "release_id": release.id,
        "manifest_hash": release.manifest_hash,
        "query": query,
        "theme_id": theme_id,
        "method": "BM25_Chinese_bigrams",
        "indexed_passages": len(docs),
        "hits": hits,
        "answer_permission": "evidence_candidates_only; no inferred probability or completeness",
    }
