"""Compose model prose from run-owned, source-verified structured queries.

No user-language interpretation happens here. The model chooses queries and
references; the server preserves their complete source results for review.
"""
from __future__ import annotations

import copy
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from arknights_agent import services


class CompositionSourceError(ValueError):
    def __init__(self, reason, args, ref):
        super().__init__(reason)
        self.fields = [{'loc': ['paragraphs', i, 'source_refs', j], 'source_ref': ref}
                       for i, p in enumerate(args.paragraphs)
                       for j, value in enumerate(p.source_refs) if value == ref]


class Paragraph(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    text: Annotated[str, Field(min_length=1, max_length=1200)]
    source_refs: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(min_length=1, max_length=8)


class Composition(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    module: Literal['operator', 'recruitment', 'garrison']
    paragraphs: list[Paragraph] = Field(min_length=1, max_length=8)
    catalog_source_ref: Annotated[str, Field(min_length=1, max_length=80)] | None = Field(
        default=None, description='用户需要完整目录时，选择本轮已在paragraphs引用的目录source_ref。服务器保留全部条目和引用，无需逐项重抄。非目录问题省略。')
    limitations: list[Annotated[str, Field(min_length=1, max_length=600)]] = Field(default_factory=list, max_length=8)
    review_response: Annotated[str, Field(min_length=1, max_length=1000)] | None = None


def specifications(specs):
    result = copy.deepcopy(specs)
    from arknights_agent.runtime.evidence_analysis import ModelFirstAnalysisAnswerArguments
    for spec in result:
        if spec['function']['name'] == 'evidence_analysis':
            schema = ModelFirstAnalysisAnswerArguments.model_json_schema()
            # Preserve the model-first protocol: models select refs/spans,
            # while explicit legacy quotes remain accepted only for replay.
            schema['$defs']['AnalysisObservation']['properties'].pop('quote', None)
            spec['function']['parameters'] = schema
    query = next((s for s in result if s['function']['name'] == 'source_query'), None)
    if query is None:
        return result
    properties = query['function']['parameters']['properties']
    properties['phase_id']['description'] = (
        '客户端阶段ID。V3_1为规程序列I，V2_1/V2_2为盟约下半期的两个阶段。'
        '查历史全阶段可指定activity_id=act2autochess且省略phase_id。'
        '省略两者的garrison_metadata读取本发布最新收录赛季。跨赛季分别查询，不混用规则。')
    properties['intent']['description'] += (
        '点名一条盟约的效果、人数或打法时用garrison_regulation_record定位单条，'
        'garrison_bond_catalog只用于用户确实要求列整季盟约。'
        '问某位干员是否能公招、有哪些公招标签，或追问核实之前对此的回答，'
        '用recruitment的catalog并在entity_names传该干员。只有计算指定标签的候选时才用recruitment_calculation。'
        '查单标签保底星级总览用recruitment_tag_overview，rarity是最低星级门槛；不虚构屏幕五标签。'
    )
    reader = copy.deepcopy(query)
    reader['function'].update(name='read_source_query', description=(
        '只读取来源核验后的结构化查询结果，不交付最终答案；可批量查询不同等级或条件。'
        '参数与source_query一致。结果返回本运行source_ref及真实原件；'
        '比较或综合时用compose_source_answer引用这些结果，由模型写正文，再独立复核。'))
    result.extend([reader, {'type': 'function', 'function': {
        'name': 'compose_source_answer',
        'description': '根据read_source_query已返回的真实source_ref整理回答、比较条件或解释差异。'
            '服务器组装完整来源，模型不重抄引文；逐段引用真实结果，所有推理仍须独立复核。'
            '把面向当前问题的结论或行动建议放在第一段，后续只写必要条件和取舍。'
            '简短答案直接用自然段；需要讲步骤时，每步单独成行，可用短标题或列表。'
            '正文使用玩家能读懂的名称，内部字段和原始数据由来源记录提供。'
            '每段只引用实际支撑该段的查询，已有单条来源时勿用整个目录代替。'
            'limitations是单独显示的资料说明，只填实际的证据缺口；同一缺口不用在几个段落反复解释。'
            '局限不能代替已查得的答案，查不到的明确说明。此工具单独提交。',
        'parameters': Composition.model_json_schema()}}])
    return result


def register(state, answer, arguments):
    sources = state.setdefault('typed_sources', {})
    digest = services.content_digest({'answer': answer, 'query': arguments})
    ref = 'query_source_' + digest[:24]
    if ref not in sources and len(sources) >= 64:
        raise ValueError('typed_source_registry_limit')
    sources[ref] = {'answer': copy.deepcopy(answer), 'query': copy.deepcopy(arguments), 'sha256': digest}
    return ref


def reusable_query(state, arguments, *, question, release):
    """Reuse only exact queries under this run's fixed source and parent state."""
    for ref, row in state.get('typed_sources', {}).items():
        if row['query'] != arguments:
            continue
        digest = services.content_digest({'answer': row['answer'], 'query': row['query']})
        answer = row['answer']
        if (digest != row['sha256'] or ref != 'query_source_' + digest[:24]
                or answer.get('question') != question or answer.get('module') != arguments.get('module')
                or answer.get('release_id') != release.id or answer.get('manifest_hash') != release.manifest_hash):
            raise ValueError('source_identity_mismatch')
        return ref, copy.deepcopy(answer)
    return None


def composition_repair_sources(state, module):
    """Small, exact reference menu; never guess a replacement for a bad ref."""
    valid = []
    for ref, row in state.get('typed_sources', {}).items():
        digest = services.content_digest({'answer': row['answer'], 'query': row['query']})
        if (row['sha256'] != digest or ref != 'query_source_' + digest[:24]
                or row['answer'].get('module') != module):
            continue
        valid.append({'source_ref': ref, 'query': {key: row['query'][key] for key in
            ('intent', 'regulation_kind', 'entity_names', 'rank', 'offset') if key in row['query']}})
    return {'available_sources': valid[-8:], 'available_source_count': len(valid)}


def compose(state, arguments, *, module, question, release):
    args = Composition.model_validate(arguments)
    if module != 'auto' and args.module != module:
        raise ValueError('source_module_mismatch')
    refs = list(dict.fromkeys(ref for p in args.paragraphs for ref in p.source_refs))
    sources = state.get('typed_sources', {})
    selected = {}
    for ref in refs:
        row = sources.get(ref)
        if not row or services.content_digest({'answer': row['answer'], 'query': row['query']}) != row['sha256']:
            raise CompositionSourceError('unknown_or_changed_source_reference', args, ref)
        answer = row['answer']
        if (answer.get('question') != question or answer.get('module') != args.module
                or answer.get('release_id') != release.id or answer.get('manifest_hash') != release.manifest_hash):
            raise ValueError('source_identity_mismatch')
        selected[ref] = copy.deepcopy(row)
    result = {k: copy.deepcopy(next(iter(selected.values()))['answer'][k])
              for k in ('question', 'release_id', 'manifest_hash', 'module')}
    # Query results were produced under this same authenticated parent. Keep
    # their complete chain so a composed answer can itself be a real parent.
    base = next(iter(selected.values()))['answer']
    for key in ('dialogue_state', 'version'):
        if key in base:
            result[key] = copy.deepcopy(base[key])
    citations, claims, records, ref_citations = [], [], {}, {}
    citation_identity = {}
    catalog = None
    if args.catalog_source_ref and args.catalog_source_ref not in selected:
        raise ValueError('catalog_source_must_be_cited_in_paragraphs')
    for index, (ref, row) in enumerate(selected.items(), 1):
        original = row['answer']
        originals = original.get('citations', [])
        # Recruitment catalog claims refer to evidence IDs. Bind them to the
        # citation for the same record before assigning public display IDs.
        by_citation_id = {}
        by_record_evidence = {}
        ref_citations[ref] = []
        for n, citation in enumerate(originals, 1):
            identity = services.canonical_json({k: v for k, v in citation.items() if k != 'id'})
            display_id = citation_identity.get(identity)
            if display_id is None:
                display_id = f'q{index}_c{n}'
                citation_identity[identity] = display_id
                citations.append({**copy.deepcopy(citation), 'id': display_id})
            ref_citations[ref].append(display_id)
            if 'id' in citation:
                by_citation_id[citation['id']] = display_id
            by_record_evidence.setdefault(
                (citation.get('record_id'), citation.get('evidence_id')), [],
            ).append(display_id)
        if ref == args.catalog_source_ref:
            catalog = copy.deepcopy(original.get('catalog'))
            if not isinstance(catalog, dict) or not isinstance(catalog.get('items'), list):
                raise ValueError('selected_source_has_no_catalog')
            for item in catalog['items']:
                item['citation_id'] = by_citation_id[item['citation_id']]
        for claim in original.get('claims', []):
            if 'citation_ids' in claim:
                linked = [by_citation_id[cid] for cid in claim['citation_ids']]
            else:
                linked = [cid for evidence_id in claim['evidence_ids']
                          for cid in by_record_evidence[claim['record_id'], evidence_id]]
            claims.append({**copy.deepcopy(claim), 'id': f'q{index}_{claim["id"]}',
                           'citation_ids': list(dict.fromkeys(linked))})
        records.update({r['id']: copy.deepcopy(r) for r in original.get('records', [])})
    analysis = [{'text': p.text, 'evidence_refs': p.source_refs,
                 'citation_ids': list(dict.fromkeys(cid for ref in p.source_refs for cid in ref_citations[ref])),
                 'verification_status': 'model_inference_not_runtime_verified'} for p in args.paragraphs]
    result.update(status='answered' if citations else 'partial', citations=citations, claims=claims,
        records=list(records.values()), model_analysis=analysis, limitations=args.limitations,
        verified_query_sources=selected,
        analysis_request={'summary': question, 'current_question': question,
                          'original_user_questions': base.get('dialogue_state', {}).get('questions', [question]),
                          'interpretation_origin': 'model_query_proposal'},
        query_interpretation={'origin': 'model_query_proposal', 'proposal': args.model_dump()},
        source_authority={'source_queries_verified': True, 'model_inferences_source_verified': False,
                          'requires_independent_semantic_review': True},
        answer='\n\n'.join(p.text for p in args.paragraphs) +
               ('\n\n尚未核实：' + '；'.join(args.limitations) if args.limitations else ''))
    if args.review_response:
        result['model_revision_response'] = args.review_response
    if catalog is not None:
        result['catalog'] = catalog
    return result
