"""Model-facing views of authenticated queries; full originals stay in the run.

No question parsing, model summarisation, arbitrary truncation or new facts.
Calculation results are trusted only after the ordinary full-pool byte replay.
"""
from __future__ import annotations

import copy

from arknights_agent import services


def _pick(value, keys):
    return {key: copy.deepcopy(value[key]) for key in keys if key in value}


def _combination(row, *, members=True):
    value = _pick(row, (
        'selected_tags', 'selected_tag_ids', 'candidate_count',
        'minimum_rarity_if_tags_retained', 'status', 'condition',
        'ordinary_tags_may_drop', 'special_tags_protected_at_this_duration',
    ))
    if members:
        value['candidates'] = [_pick(c, ('name', 'native_id', 'rarity_stars', 'tag_names'))
                               for c in row['candidates']]
    return value


def source_view(answer):
    """Project executable result fields, never the user's semantic conditions."""
    if answer.get('module') == 'recruitment' and 'calculation_inputs' in answer:
        value = _pick(answer, ('question', 'module', 'status', 'release_id', 'manifest_hash',
                              'version', 'input', 'normal_rarity_range_stars', 'warnings',
                              'probabilities', 'answer_scope', 'tag_overview'))
        overview = 'tag_overview' in answer
        display = answer.get('recruitment_display')
        rows = display['combinations'] if display else answer['combinations']
        if not overview:
            value['combinations'] = [_combination(row) for row in rows]
        if display:
            value['recruitment_display'] = _pick(display, (
                'scope', 'selected_tag_ids', 'displayed_combination_count',
                'total_combination_count', 'combinations_preserved', 'proof'))
        focus = answer.get('requested_focus')
        if focus:
            # Candidate lists above already carry every rarity, so retain the
            # requested filter and its semantics without copying them again.
            value['requested_focus'] = _pick(focus, ('rarity_stars', 'operation',
                                                   'combinations_preserved', 'guarantee_or_probability'))
        # Full-pool absence/minimum claims require every member's provenance,
        # not just positive matches. Retain the entire proof server-side and
        # expose its digest and source identities, not hundreds of copies.
        unique = {}
        for citation in answer.get('citations', []):
            source = _pick(citation, ('snapshot_id', 'source_url', 'source_revision', 'authority'))
            unique[services.canonical_json(source)] = source
        value['citations'] = list(unique.values())
        value['calculation_proof'] = {
            'validation': 'complete_published_pool_and_rules_replayed_before_calculation',
            'input_count': len(answer['calculation_inputs']['member_record_ids']),
            'complete_calculation_sha256': services.content_digest(answer['combinations']),
            'complete_citations_sha256': services.content_digest(answer['citations']),
            'complete_citation_count': len(answer['citations']),
            'probabilities_verified': False,
        }
    else:
        # Keep raw field values, conditions and quoted evidence. Remove only
        # server audit/history copies; source text is never shortened here.
        value = {key: copy.deepcopy(item) for key, item in answer.items() if key not in {
            'tool_trace', 'trace', 'dialogue_state', 'query_interpretation', 'sources',
            'agent_runtime', 'run_id', 'verified_query_sources',
        }}
    value['source_view'] = {'format': 'authenticated-source-view-v1',
                          'original_sha256': services.content_digest(answer),
                          'complete_original_retained': True}
    return value


def query_views(sources):
    return {ref: {'query': copy.deepcopy(row['query']), 'sha256': row['sha256'],
                  'answer': source_view(row['answer'])} for ref, row in sources.items()}


def observation_view(result, registry):
    data = result.get('data', {})
    ref = data.get('source_ref')
    row = registry.get(ref)
    if not row or data.get('verified_source_result') != row['answer']:
        return result, None
    if services.content_digest({'answer': row['answer'], 'query': row['query']}) != row['sha256']:
        raise ValueError('source_identity_mismatch')
    projected = copy.deepcopy(result)
    projected['data']['verified_source_result'] = source_view(row['answer'])
    return projected, {'tool_source_ref': ref, 'original_sha256': services.content_digest(result),
                       'before_bytes': len(services.canonical_json(result).encode()),
                       'after_bytes': len(services.canonical_json(projected).encode())}
