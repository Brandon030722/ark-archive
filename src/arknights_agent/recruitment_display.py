"""Source-identity projections of a complete recruitment calculation.

The model selects a typed display scope. This module never reads a question or
changes the calculated candidates, minimum rarities, warnings or provenance.
"""

from copy import deepcopy
from itertools import combinations as subsets

from arknights_agent import services
from arknights_agent.errors import Conflict, InvalidData


def project_recruitment_display(result, scope="all_combinations"):
    if not isinstance(scope, str) or scope not in {"all_combinations", "exact_selected_tags"}:
        raise InvalidData("unknown recruitment display scope")
    combinations = result["combinations"]
    names = result["input"]["normalized_tags"]
    if (
        not isinstance(names, list) or any(not isinstance(name, str) for name in names)
        or len(set(names)) != len(names)
        or not isinstance(combinations, list) or not combinations
        or type(result["combination_count"]) is not int
        or result["combination_count"] != len(combinations)
    ):
        raise Conflict("recruitment display differs from complete calculation")
    singletons = {}
    for combination in combinations:
        tags, ids = combination["selected_tags"], combination["selected_tag_ids"]
        if (
            not isinstance(tags, list) or not isinstance(ids, list)
            or any(not isinstance(tag, str) for tag in tags)
            or any(type(tag_id) is not int for tag_id in ids)
            or len(tags) != len(ids) or len(set(ids)) != len(ids)
        ):
            raise Conflict("invalid calculated recruitment combination identity")
        if len(tags) == 1:
            if tags[0] in singletons:
                raise Conflict("duplicate calculated recruitment tag identity")
            singletons[tags[0]] = ids[0]
    if set(singletons) != set(names) or len(set(singletons.values())) != len(names):
        raise Conflict("calculated recruitment tags do not match normalized input")
    selected_ids = sorted(singletons[name] for name in names)
    seen = set()
    for combination in combinations:
        tags, ids = combination["selected_tags"], combination["selected_tag_ids"]
        if (
            any(tag not in singletons for tag in tags)
            or sorted(singletons[tag] for tag in tags) != sorted(ids)
            or tuple(sorted(ids)) in seen
        ):
            raise Conflict("calculated recruitment combination selector differs")
        seen.add(tuple(sorted(ids)))
    complete_ids = {
        tuple(row) for size in range(1, min(3, len(selected_ids)) + 1)
        for row in subsets(selected_ids, size)
    } if selected_ids else {()}
    if seen != complete_ids:
        raise Conflict("recruitment display requires the complete calculated combinations")
    if scope == "exact_selected_tags":
        if len(selected_ids) > 3:
            raise InvalidData("exact recruitment selection permits at most three tags")
        shown = [row for row in combinations if sorted(row["selected_tag_ids"]) == selected_ids]
        if len(shown) != 1:
            raise Conflict("exact recruitment selection has no unique calculated combination")
    else:
        shown = combinations
    return {
        "scope": scope,
        "selected_tag_ids": selected_ids,
        "displayed_combination_count": len(shown),
        "total_combination_count": len(combinations),
        "combinations_preserved": True,
        "combinations": deepcopy(shown),
        "proof": {
            "complete_combinations_sha256": services.content_digest(combinations),
            "displayed_combinations_sha256": services.content_digest(shown),
        },
    }
