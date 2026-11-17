"""Compose reviewed module inputs; retain every source's own revision identity."""

from .garrison import build_garrison_batch
from .recruitment import build_recruitment_batch
from .sample import DEFAULT_COMMIT, fetch_sample

PARSER_VERSION = "cn-stage2-v1"


def fetch_stage2(commit=DEFAULT_COMMIT, *, cache_dir=".local/ingestion-cache", all_operators=True):
    batch = fetch_sample(commit, cache_dir=cache_dir, all_operators=all_operators)
    batch = build_recruitment_batch(batch)
    return build_garrison_batch(batch)
