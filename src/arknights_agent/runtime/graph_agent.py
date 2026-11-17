"""Model-first LangGraph orchestration using the existing evidence and fee contracts.

Only plain JSON state is checkpointed. Providers, credentials, SQL sessions and
ledger capabilities remain in the runtime instance. All model nodes go through
the same durable call journal; no LangGraph/SDK retry policy is installed.
"""
from __future__ import annotations

import copy
import sqlite3
import time
from dataclasses import asdict
from typing import TypedDict
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context
from pydantic import ValidationError

from arknights_agent import services
from arknights_agent.db import readonly_session
from arknights_agent.runtime import graph_synthesis
from arknights_agent.runtime.budget import RESERVATION_INPUT_TOKENS, BudgetError
from arknights_agent.runtime.contracts import ModelTurn, ProviderError
from arknights_agent.runtime.execution_progress import ExecutionProgress
from arknights_agent.runtime.graph_journal import GraphCallJournal, GraphRecoveryRequired
from arknights_agent.runtime.observations import (
    fit_retrieval_observations,
    prompt_bytes,
    replayable_observation,
    retrieval_execution_counts,
    review_retrieval_audit,
)
from arknights_agent.runtime.providers import valid_failure_receipt
from arknights_agent.runtime.security import contains_secret, redact
from arknights_agent.runtime.shared_observation import MODEL_GUIDANCE, share_observation
from arknights_agent.runtime.source_projection import observation_view
from arknights_agent.runtime.task_progress import TaskProgress
from arknights_agent.runtime.tools import ToolContext, execute_tool


class AgentState(TypedDict, total=False):
    messages: list
    evidence: dict
    trace: dict
    progress: dict
    turn: dict | None
    specs: list
    staged: dict | None
    decision: dict | None
    review_messages: list
    route: str
    reason: str | None
    selected: dict | None
    identity: dict
    retrieval_audit: list
    budget_feedback: dict
    typed_sources: dict
    retrieval_observations: dict
    rejected_proposal: dict


def _dump_progress(value):
    result = asdict(value)
    result['repeats'] = [[action, generation, count]
                         for (action, generation), count in value.repeats.items()]
    for key in ('evidence', 'plans', 'proposals'):
        result[key] = sorted(result[key])
    return result


def _load_progress(value):
    value = copy.deepcopy(value)
    value['repeats'] = {(a, g): n for a, g, n in value['repeats']}
    for key in ('evidence', 'plans', 'proposals'):
        value[key] = set(value[key])
    return ExecutionProgress(**value)


class GraphAgent:
    def __init__(self, *, engine, store, settings, provider, ledger, budget_profile, release,
                 question, module, transport, previous_answer, previous_run_id, messages,
                 specs, public_input, previous_context, trace, deadline, remaining_settings,
                 finish, execution_id=None, progress_observer=None):
        self.engine, self.store, self.settings = engine, store, settings
        self.provider, self.ledger, self.profile = provider, ledger, budget_profile
        self.release, self.question, self.module = release, question, module
        self.transport, self.previous_answer = transport, previous_answer
        self.progress = TaskProgress(progress_observer, question)
        self.previous_run_id = previous_run_id
        self.public_input, self.previous_context = public_input, previous_context
        self.specs = [s for s in specs if s['function']['name'] not in
                      {'knowledge_answer', 'submit_answer', 'operator_talents', 'garrison_document'}]
        self.specs = graph_synthesis.specifications(self.specs)
        self.analysis_enabled = any(s['function']['name'] == 'evidence_analysis' for s in self.specs)
        if not self.analysis_enabled:
            self.specs = [s for s in self.specs if s['function']['name'] != 'organize_evidence']
        messages = copy.deepcopy(messages)
        from arknights_agent.runtime.runner import _LEGACY_PLAN_SEQUENCE
        messages[0]['content'] = messages[0]['content'].replace(_LEGACY_PLAN_SEQUENCE, '')
        messages[0]['content'] += (
            '\n当前工具能力：source_query是单个最终提案；如需先查询、比较多个等级或条件，'
            '使用read_source_query（可批量），再用compose_source_answer按source_ref整理正文。'
            '明确的单对象事实优先一次source_query。给玩法建议时先检索最能决定操作的对象和规则，'
            '已知且互不依赖的必要查询放在同一批。只问某条目时优先读该条目，'
            '仅在全表或赛季信息会改变答案时才查这些资料。证据足以给有边界的建议就直接提交，'
            '提交后只处理独立复核指出的实质问题，不为补背景再次查询。'
            '首批必须有且只先提交一个plan_task。对象和范围已明确时，允许在同一批把它放在'
            '一个source_query或roguelike_query前，或放在必要的read_source_query前；'
            '需要探索时，也可在计划后同批提交已确定检索词的analysis_search、query_records或rag_search。'
            '同批查询必须互不依赖，不能引用本批尚未返回的候选或证据编号。'
            '计划通过才执行后续查询。需要根据计划反馈调整时只提交plan_task。'
            '最终提案不可与其它查询混批，每批最多一个最终提案；首批可在它前面提交计划。'
            'compose_source_answer会保留完整查询原件供独立复核，不能把未知或错误查询当成事实。'
            '简单事实查询目标是三次模型调用内完成：第一批plan_task与已确定的read_source_query，'
            '第二次compose_source_answer，第三次独立复核；可直接交付的source_query也可与计划同批。'
            '这不是硬性终止条件，证据不足或复核发现实质问题时继续补查。'
            'simple任务且首个查询参数已明确时，首轮直接一起提交plan_task与read_source_query，'
            '计划确认不需要额外模型往返；仅在查询确实依赖计划的工具反馈时才拆开。'
            '先确定最小证据范围，不为简单事实泛查全库，不虚构用户未给的标签组合或其他条件。'
            '公招单标签保底总览用recruitment_tag_overview；时长未给可明确以9小时为条件，'
            '不能把单标签总览说成覆盖全部多标签组合。'
            '工具的authenticated-source-view-v1是经来源校验的查询视图，完整原件留在服务器。'
            'calculation_proof只证明固定来源计算，不能证明用户意图、概率或当前线上版本。'
            'source_view及其摘要不是可用于正文的新来源编号。'
            + MODEL_GUIDANCE +
            'source_query提案也返回可复用的source_ref，复核仅要求改正文时直接引用它修订，'
            '无需重查相同资料。任务计划中的条件和待证问题必须逐项回应，未证实的明确缺口。'
            'evidence_analysis直接在analysis.evidence_refs选择本运行真实引用，服务器会组装对应原文；'
            'observations可省略，不必重复填引用，更不要重新抄写Wiki。需要短片段才用start/end。'
            '简称未定位时先用query_records查实际目录：module=roguelike的kind可取'
            'roguelike_theme、roguelike_squad、roguelike_relic或roguelike_item；name省略可浏览目录，'
            '再由你按上下文选候选并检索效果；不要只对口语原词重复词面检索。'
            '正文先回答所问关系，不用无关主题或无关道具的条文填充答案。'
            '用户否定旧类别时，先在plan_task的requested_object_type与rejected_object_types记录更正；'
            '后续检索按新类别选择工具，上一轮错误答案不是新的事实依据。'
            + ('' if self.analysis_enabled else
               '本模块没有analysis_search/analysis_read/organize_evidence/evidence_analysis，'
               '前述分析流程在此使用read_source_query→compose_source_answer；不要调用未提供的工具。'))
        garrison_scope = release.manifest.get('datasets', {}).get('garrison', {}).get('scope', {})
        current_season = garrison_scope.get('latest_documented_season')
        if module in {'auto', 'garrison'} and current_season:
            messages[0]['content'] += (
                '\n卫戍固定发布的已收录赛季定位数据（仅供本轮绑定范围）：'
                + services.canonical_json({key: current_season[key] for key in ('title', 'activity_id', 'phase_id')
                                           if key in current_season})
                + '。只问本期具体盟约机制或玩法时，首选garrison_regulation_record，regulation_kind=regulation_bond，'
                'entity_names填所问名称。已能精确定位时同批提交plan_task及该read_source_query。'
                '只有用户问赛季名称、整体解锁或时期政策，或这些政策直接改变操作时，才用garrison_metadata；'
                '已确认赛季的追问沿用其范围，明确问历史盟约时才指定act2autochess。'
                '具体模式、等级、解锁条件或联机门槛查regulation_mode，先按已知名称或词段查，'
                '比较单人和联机时按modeType及各自unlockText核对，勿把等级、难度标记和匹配算法混为一谈。'
                '装备普通与金色分别查regulation_equipment，用各自isGolden及effectId绑定regulation_effect原文。'
                '关键目标和兑换各查regulation_mission和regulation_shop_good。已定位名称时不要逐类泛查目录。'
                '如需列本赛季盟约名称和效果，用单次source_query intent=garrison_bond_catalog，'
                '服务端返回全部盟约、各自条件原文和引用，无需分页取证或把目录重写成大段文字。'
                '开局策略与盟约是不同目录；开局策略的客户端类型为regulation_band。'
                '列这类开局选项用garrison_regulation_catalog并指定regulation_kind=regulation_band，'
                '一次读取同类目录及来源，按catalog.has_more判断是否需要下一页；不要逐个查询整类条目。'
                '无法确认简称时先查对应类别目录，不能把简称的词面缺失说成对象不存在。'
                '原问泛问本期盟约有哪些且未给出可核验的子集条件时，完整目录可作答，'
                '目录范围明示为本期全部收录盟约（含特殊条目），不声称它们全属于某个未核实分类。'
                '如用户只要指定子集、比较、参数或实战解释，按该范围另选查询和输出，不能用全目录代答。'
                '用read_source_query的garrison_regulation_record按实际kind与名称或ID取证，'
                '若涉及跨表关系，引用关系所在的记录及其所指效果。'
                '预载或计划时间不证明实时开放。公告未给出具体日期时明确说未公布。'
            )
        self.trace, self.deadline = trace, deadline
        self.remaining_settings, self.finish = remaining_settings, finish
        self.execution_id = execution_id or uuid4().hex
        self.path = settings.model_budget_path.resolve().parent / 'langgraph-state.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.journal = GraphCallJournal(self.path)
        self.path.chmod(0o600)
        progress = ExecutionProgress(settings.agent_max_model_calls,
            min(settings.agent_progress_initial_calls, settings.agent_max_model_calls),
            settings.agent_progress_extension_calls, settings.agent_revision_reserve_calls,
            settings.agent_idle_repeat_limit)
        trace.update(execution_engine='langgraph', graph_protocol='source-agent-graph-v1',
                     graph_execution_id=self.execution_id, model_first_enabled=True,
                     semantic_review_enabled=True, graph_nodes=[])
        self.initial = AgentState(messages=copy.deepcopy(messages), evidence={}, trace=copy.deepcopy(trace),
            progress=_dump_progress(progress), staged=None, decision=None, turn=None,
            review_messages=[], specs=[], route='plan', reason=None, selected=None,
            retrieval_audit=[], budget_feedback={}, typed_sources={}, retrieval_observations={},
            rejected_proposal={},
            identity={'release_id': release.id, 'manifest_hash': release.manifest_hash,
                      'question': question, 'previous_run_id': previous_run_id,
                      'implementation_hash': services.implementation_hash(),
                      'initial_context_hash': services.content_digest(messages),
                      'runtime_contract_hash': services.content_digest({
                          'model': provider.model, 'simulation': provider.simulation,
                          'ledger_path': str(settings.model_budget_path.resolve()),
                          'agent_settings': {k: v for k, v in settings.model_dump(mode='json').items()
                                             if k.startswith('agent_')},
                          'tools': specs})})

    @staticmethod
    def stop(state, reason):
        state.update(reason=reason, route='end')
        return state

    def mark(self, state, name):
        state['trace']['graph_nodes'].append(name)
        return state

    def prepare(self, state):
        from arknights_agent.runtime.runner import (
            _execution_budget_feedback,
            _registered_correction_refs,
        )
        self.mark(state, 'prepare')
        cfg, trace = self.settings, state['trace']
        if time.monotonic() + cfg.deepseek_timeout_seconds > self.deadline:
            return self.stop(state, 'time_limit')
        p = _load_progress(state['progress'])
        left = p.allocate(trace['model_calls'])
        state['progress'] = _dump_progress(p)
        trace.setdefault('progress_allocations', []).append({'used_model_calls': trace['model_calls'], **p.audit()})
        if left < 2 or trace['model_calls'] >= cfg.agent_max_model_calls:
            return self.stop(state, 'model_call_limit')
        tools_left = cfg.agent_max_tool_calls - trace['tool_calls']
        if tools_left < 2:
            return self.stop(state, 'tool_limit')
        if not state['evidence'].get('task_plan') and tools_left < 3:
            return self.stop(state, 'tool_limit')
        identity = {k: state['identity'][k] for k in
                    ('release_id', 'manifest_hash', 'question', 'previous_run_id')}
        registered = _registered_correction_refs(state['evidence'], identity,
                                                 state['evidence'].get('items', {}), max_refs=None)
        feedback = _execution_budget_feedback(state['messages'], model_remaining=left,
            tool_remaining=tools_left, review_phase=False, max_output_tokens=cfg.agent_max_output_tokens,
            registered_read_refs=registered,
            revision_reserve=cfg.agent_revision_reserve_calls - 2 if not trace.get('staged_source_answers') else 0,
            tail=True)
        trace.setdefault('execution_budget_feedback', []).append(feedback)
        state['budget_feedback'] = feedback
        specs = copy.deepcopy(self.specs)
        if not state['evidence'].get('task_plan'):
            specs = [s for s in specs if s['function']['name'] in
                     {'plan_task', 'source_query', 'roguelike_query', 'read_source_query',
                      'analysis_search', 'query_records', 'rag_search'}]
            state['route'] = 'plan'
        else:
            state['route'] = 'reason'
            if state.get('typed_sources') and not state['evidence'].get('items'):
                specs = [s for s in specs if s['function']['name'] not in {'organize_evidence', 'evidence_analysis'}]
            elif not state['evidence'].get('organized_evidence'):
                specs = [s for s in specs if s['function']['name'] != 'evidence_analysis']
            if feedback['discovery_closed']:
                specs = [s for s in specs if s['function']['name'] in
                         {'plan_task', 'organize_evidence', 'evidence_analysis', 'source_query', 'roguelike_query',
                          'compose_source_answer'}
                         or (s['function']['name'] == 'analysis_read' and 'registered_source_read' in feedback)]
        state['specs'] = specs
        return state

    def fit_messages(self, state, messages, specs, phase):
        """Shrink only authenticated discovery display, never questions or proof."""
        def measure(value):
            return self.provider.request_size(
                value, specs, max_output_tokens=self.settings.agent_max_output_tokens)
        before = measure(messages)
        if before <= self.settings.agent_max_prompt_bytes or phase == 'review':
            return messages
        envelope = before - prompt_bytes(messages, specs)
        cache = {int(index): item for index, item in state.get('retrieval_observations', {}).items()}
        fitted, audit = fit_retrieval_observations(
            messages, specs, self.settings.agent_max_prompt_bytes - envelope, cache)
        if audit:
            state['trace'].setdefault('prompt_compactions', []).append({**audit,
                'phase': phase, 'request_envelope_bytes': envelope,
                'before_complete_wire_bytes': before,
                'after_complete_wire_bytes': measure(fitted) if fitted is not None else None})
        # The ordinary preflight still rejects an irreducible request before
        # reserving money. A projection is not evidence or a new source read.
        return fitted if fitted is not None else messages

    def billed_model(self, state, messages, specs, phase):
        """Preflight → once-only claim → reserve → provider → settle → durable receipt."""
        cfg, trace = self.settings, copy.deepcopy(state['trace'])
        if trace['model_calls'] >= cfg.agent_max_model_calls:
            return {'error': 'model_call_limit', 'trace': trace}
        if time.monotonic() + cfg.deepseek_timeout_seconds > self.deadline:
            return {'error': 'time_limit', 'trace': trace}
        try:
            self.provider.validate_request(messages, specs, max_output_tokens=cfg.agent_max_output_tokens)
            if self.provider.request_size(messages, specs, max_output_tokens=cfg.agent_max_output_tokens) > cfg.agent_max_prompt_bytes:
                return {'error': 'prompt_limit', 'trace': trace}
            kwargs = {}
            input_tokens = RESERVATION_INPUT_TOKENS
            if self.profile:
                bound = self.profile.bind_request(self.provider, messages, specs,
                                                 max_output_tokens=cfg.agent_max_output_tokens)
                trace.setdefault('bounded_budget_requests', []).append(bound.audit())
                input_tokens = bound.input_tokens
                kwargs = {'bounded_request': bound, 'continuation_id': self.profile.continuation_id}
        except (ProviderError, BudgetError) as exc:
            return {'error': exc.code, 'trace': trace}
        key = f'{self.execution_id}:{trace["model_calls"] + 1}:{phase}'
        request = {'messages': messages, 'tools': specs, 'model': self.provider.model,
                   'provider': cfg.api_protocol, 'api_base_url': cfg.api_base_url,
                   'max_output_tokens': cfg.agent_max_output_tokens, 'identity': state['identity']}
        try:
            cached = self.journal.claim(key, request)
        except GraphRecoveryRequired as exc:
            return {'error': str(exc), 'trace': trace}
        if cached is not None:
            return cached
        reservation = None
        outcome = {}
        try:
            reservation = self.ledger.reserve(input_tokens=input_tokens,
                output_tokens=cfg.agent_max_output_tokens, input_price=cfg.model_input_cny_per_million,
                output_price=cfg.model_output_cny_per_million, **kwargs)
            trace['model_calls'] += 1
            turn = self.provider.complete(messages, specs, max_output_tokens=cfg.agent_max_output_tokens)
            if not isinstance(turn, ModelTurn) or turn.simulation != self.provider.simulation:
                raise ProviderError('invalid_response')
            trace['budget'] = self.ledger.settle(reservation, turn.usage)
            trace['total_tokens'] += turn.usage['total_tokens']
            trace['model_turns'].append({'model': turn.model, 'simulation': turn.simulation,
                'usage': dict(turn.usage), 'reservation_id': reservation,
                'accounted_cny': trace['budget']['reservation_accounted_cny'],
                'response_shape': getattr(self.provider, 'last_response_diagnostic', {})})
            # Untrusted free prose cannot become a delivered answer or checkpoint data.
            if turn.finish_reason != 'tool_calls':
                outcome['error'] = 'uncertified_model_output'
            elif contains_secret([c.model_dump() for c in turn.tool_calls]):
                outcome['error'] = 'sensitive_model_arguments'
            else:
                outcome['turn'] = turn.model_copy(update={'content': None}).model_dump(mode='json')
        except ProviderError as exc:
            receipt = self.provider.failure_receipt(exc)
            trace['provider_response_shape'] = getattr(self.provider, 'last_response_diagnostic', {})
            if valid_failure_receipt(receipt, self.provider.model, self.provider.simulation):
                try:
                    trace['budget'] = self.ledger.settle(reservation, receipt['usage'])
                    trace['total_tokens'] += receipt['usage']['total_tokens']
                    trace['model_turns'].append({'model': receipt['model'], 'simulation': receipt['simulation'],
                        'usage': receipt['usage'], 'reservation_id': reservation,
                        'accounted_cny': trace['budget']['reservation_accounted_cny'], 'protocol_error': exc.code})
                    outcome['known_protocol_error'] = exc.code
                except BudgetError as billing_error:
                    trace['budget'] = self.ledger.fail_unknown(reservation, billing_error.code)
                    outcome['error'] = billing_error.code
            else:
                trace['budget'] = self.ledger.fail_unknown(reservation, exc.code)
                outcome['error'] = exc.code
        except BudgetError as exc:
            if reservation is not None:
                trace['budget'] = self.ledger.fail_unknown(reservation, exc.code)
            outcome['error'] = exc.code
        except Exception:
            if reservation is not None:
                trace['budget'] = self.ledger.fail_unknown(reservation, 'provider_error')
            outcome['error'] = 'provider_error'
        if trace.get('budget', {}).get('frozen') or trace.get('budget', {}).get('reservation_state') == 'unknown':
            outcome['error'] = outcome.get('error', 'usage_invalid')
            outcome.pop('turn', None)
        if trace['total_tokens'] > cfg.agent_max_total_tokens and not outcome.get('error'):
            outcome['error'] = 'token_limit'
        if trace['total_tokens'] > cfg.agent_max_total_tokens:
            outcome['error'] = 'token_limit'
        if time.monotonic() >= self.deadline:
            outcome['error'] = 'time_limit'
        outcome['trace'] = trace
        self.journal.complete(key, outcome)
        return outcome

    def model(self, state, phase):
        self.mark(state, phase)
        review = phase == 'review'
        plan = state['evidence'].get('task_plan', {})
        simple = phase == 'plan' or plan.get('complexity') == 'simple'
        if state['trace'].get('semantic_reviews'):
            simple = False  # A substantive correction deserves more reasoning.
        effort = (self.settings.agent_simple_reasoning_effort if simple
                  else self.settings.agent_complex_reasoning_effort)
        if hasattr(self.provider, 'with_reasoning_effort'):
            self.provider = self.provider.with_reasoning_effort(effort)
            state['trace'].setdefault('reasoning_allocations', []).append({
                'phase': phase, 'effort': effort, 'model_classified_complexity': plan.get('complexity'),
                'model_call': state['trace']['model_calls'] + 1})
        if phase == 'plan':
            self.progress.show("planning", module=self.module)
        elif review:
            answer = state['staged']['answer']
            self.progress.show("reviewing", module=answer.get('module'), count=len(answer.get('citations') or []))
        else:
            self.progress.show("revising" if state.get('decision', {}) and
                               state['decision'].get('decision') == 'revise' else "writing", module=self.module)
        from arknights_agent.runtime.runner import _semantic_review_specs
        messages = state['review_messages'] if review else state['messages']
        specs = _semantic_review_specs(model_first=True) if review else state['specs']
        try:
            messages = self.fit_messages(state, messages, specs, phase)
        except (ProviderError, BudgetError) as exc:
            return self.stop(state, exc.code)
        state['review_messages' if review else 'messages'] = messages
        result = self.billed_model(state, messages, specs, phase)
        if not review and messages and messages[-1].get('name') == 'runtime_execution_budget':
            # The journal retains the exact wire. Transient budget state must
            # not shift observation indexes or accumulate in dialogue history.
            messages.pop()
        state['trace'] = result['trace']
        if result.get('error'):
            return self.stop(state, result['error'])
        if result.get('known_protocol_error'):
            messages.append({'role': 'system', 'content':
                '上次响应格式错误，工具未执行。依据完整原问和实际证据重新生成合法参数，不补写未知内容。'})
            state['route'] = 'review' if review else 'prepare'
            return state
        turn = ModelTurn.model_validate(result['turn'])
        if turn.finish_reason != 'tool_calls':
            return self.stop(state, 'uncertified_model_output')
        if contains_secret([c.model_dump() for c in turn.tool_calls]):
            return self.stop(state, 'sensitive_model_arguments')
        state['turn'] = result['turn']
        state['route'] = 'review_decision' if review else 'tools'
        return state

    def observe(self, state, call, result, *, executed=None):
        step = {'tool': call.name,
            'arguments_hash': services.content_digest(call.arguments),
            'status': 'ok' if result.get('ok') else result.get('error', {}).get('code', 'tool_error')}
        if executed is not None:
            step['executed'] = executed
        state['trace']['steps'].append(step)
        observation = replayable_observation(call.name, result)
        if observation is not None:
            state.setdefault('retrieval_observations', {})[str(len(state['messages']))] = observation
        display, projection = observation_view(result, state.get('typed_sources', {}))
        if projection:
            state['trace'].setdefault('source_projections', []).append(projection)
        display = redact(display)
        if call.name in {'read_source_query', 'source_query', 'query_records', 'read_evidence'}:
            display, audit = share_observation(display)
            if audit:
                state['trace'].setdefault('lossless_observations', []).append({
                    **audit, 'tool': call.name, 'tool_call_id': call.id})
        data = services.canonical_json(display)
        if len(data.encode()) > self.settings.tool_result_max_bytes:
            if observation is not None:
                data = services.canonical_json(redact(observation))
            if len(data.encode()) > self.settings.tool_result_max_bytes:
                data = services.canonical_json({'ok': False, 'error': {'code': 'tool_result_too_large'}})
        state['messages'].append({'role': 'tool', 'tool_call_id': call.id, 'content': data})

    def tools(self, state):
        self.mark(state, 'tools')
        turn = ModelTurn.model_validate(state['turn'])
        trace = state['trace']
        state['messages'].append(turn.assistant_message())
        available = {s['function']['name'] for s in state['specs']}
        proposals = {'evidence_analysis', 'source_query', 'roguelike_query', 'compose_source_answer'}
        first_batch = not state['evidence'].get('task_plan')
        calls = turn.tool_calls
        starts_with_plan = first_batch and calls[0].name == 'plan_task' and sum(
            call.name == 'plan_task' for call in calls) == 1
        answer_calls = calls[1:] if starts_with_plan else calls
        mixed_proposal = len(answer_calls) > 1 and any(c.name in proposals for c in answer_calls)
        p = _load_progress(state['progress'])
        # Admission is atomic: a discovery batch cannot consume the slots
        # needed to construct and independently review the final answer.
        final_batch = len(answer_calls) == 1 and answer_calls[0].name in proposals
        reserved = 1 if final_batch else 2
        from arknights_agent.runtime.runner import _registered_source_read_batch
        registered_batch = _registered_source_read_batch(calls, state['budget_feedback'])
        if not first_batch and not final_batch and not registered_batch:
            reserved += state['budget_feedback'].get('reserved_revision_tool_calls', 0)
        capacity = max(0, self.settings.agent_max_tool_calls - trace['tool_calls'] - reserved)
        if len(calls) > capacity:
            fingerprint = services.content_digest([{'name': c.name, 'arguments': c.arguments} for c in calls])
            if p.repeat(fingerprint):
                return self.stop(state, 'no_progress_repeated_action')
            trace.setdefault('unexecuted_discovery_batches', []).append({
                'model_call': trace['model_calls'], 'attempted_tool_calls': len(calls),
                'executed_tool_calls': 0, 'available_tool_calls': capacity,
                'reserved_tool_calls': reserved, 'arguments_hashes': [
                    services.content_digest({'name': c.name, 'arguments': c.arguments}) for c in calls]})
            for call in calls:
                self.observe(state, call, {'ok': False, 'error': {
                    'code': 'discovery_budget_reserved_for_answer_and_review', 'executed': False,
                    'available_tool_calls': capacity, 'reserved_tool_calls': reserved,
                    'detail': '本批全部未执行。请缩小本批查询数量，保留完整回答与独立复核空间；'
                              '已有资料足够时直接提交，确实缺失的说明边界。'}}, executed=False)
            state['progress'] = _dump_progress(p)
            state['route'] = 'prepare'
            return state
        for call in turn.tool_calls:
            if trace['tool_calls'] >= self.settings.agent_max_tool_calls:
                return self.stop(state, 'tool_limit')
            trace['tool_calls'] += 1
            fingerprint = services.content_digest({'name': call.name, 'arguments': call.arguments})
            if p.repeat(fingerprint):
                return self.stop(state, 'no_progress_repeated_action')
            if first_batch and (not starts_with_plan or (
                    call.name != 'plan_task' and not state['evidence'].get('task_plan'))):
                self.observe(state, call, {'ok': False, 'error': {'code': 'task_plan_required',
                    'executed': False, 'detail': '先提交一个合法的plan_task，再提交对应的来源查询。'}})
                continue
            if mixed_proposal:
                self.observe(state, call, {'ok': False, 'error': {'code': 'multiple_final_proposals',
                    'executed': False, 'detail': '本批未执行。source_query是最终提案，须单独提交一个。'
                    '需要比较多组条件时，请把这些查询改为read_source_query读取证据（可批量），'
                    '收到source_ref后用compose_source_answer综合成一个完整回答，再独立复核。'}})
                continue
            if call.name not in available:
                self.observe(state, call, {'ok': False, 'error': {'code': 'tool_not_available_in_phase',
                    'detail': '依据当前工具schema提交；回答提案单独提交，其余有效条件保持。'}})
                continue
            rejected = state.get('rejected_proposal', {})
            if (call.name in proposals and rejected.get('fingerprint') == fingerprint
                    and rejected.get('generation') == p.generation):
                self.observe(state, call, {'ok': False, 'error': {
                    'code': 'unchanged_review_rejected_proposal', 'executed': False,
                    'detail': '这份提案已被本轮复核退回，尚无新的来源或计划。请根据复核指出的实际问题修订；'
                              '只改正文可用已有source_ref提交compose_source_answer。复核意见不是事实，'
                              '有原文依据时说明反证，不要原样重交。'}}, executed=False)
                continue
            if state['budget_feedback'].get('discovery_closed') and call.name == 'analysis_read':
                if not _registered_source_read_batch(turn.tool_calls, state['budget_feedback']):
                    self.observe(state, call, {'ok': False, 'error': {'code': 'registered_source_read_not_authorized'}})
                    continue
            dispatch = {'roguelike_query': 'roguelike_document', 'read_source_query': 'source_query'}.get(call.name, call.name)
            if call.name in proposals or call.name == 'read_source_query':
                target = (call.arguments.get('module') if call.name in
                          {'source_query', 'read_source_query', 'compose_source_answer'} else 'roguelike')
                if self.module != 'auto' and target != self.module:
                    self.observe(state, call, {'ok': False, 'error': {'code': 'conditions_fixed'}})
                    continue
            bounded = self.remaining_settings()
            progress_module = call.arguments.get('module') or (
                'roguelike' if call.name.startswith('analysis_') or call.name == 'roguelike_query' else self.module)
            self.progress.read(call.name, progress_module, call.arguments)
            reused = None
            if call.name == 'read_source_query':
                try:
                    reused = graph_synthesis.reusable_query(state, call.arguments,
                        question=self.question, release=self.release)
                except ValueError:
                    return self.stop(state, 'query_commit_integrity_failure')
            if call.name == 'compose_source_answer':
                try:
                    result = {'ok': True, 'data': graph_synthesis.compose(state, call.arguments,
                        module=self.module, question=self.question, release=self.release)}
                except ValidationError as exc:
                    result = {'ok': False, 'error': {'code': 'invalid_source_composition',
                        'fields': [{'loc': list(item['loc']), 'type': item['type']}
                                   for item in exc.errors(include_input=False, include_url=False)[:12]],
                        'detail': '按字段位置修正结构，保留已核验的来源和有效条件。'}}
                except graph_synthesis.CompositionSourceError as exc:
                    result = {'ok': False, 'error': {'code': 'invalid_source_composition',
                        'reason': str(exc), 'fields': exc.fields,
                        **graph_synthesis.composition_repair_sources(state, call.arguments.get('module')),
                        'detail': '只修正列出段落的来源引用。编号须逐字使用本运行真实source_ref；'
                                  '未找到支撑来源的论断请删除或明确未知。不得猜测、编造引用或重查无关资料。'}}
                except (ValueError, KeyError, TypeError) as exc:
                    result = {'ok': False, 'error': {'code': 'invalid_source_composition',
                        'reason': str(exc) if isinstance(exc, ValueError) else 'invalid_source_structure',
                        'detail': '仅引用本运行实际返回的source_ref，保持模块与真实来源。'}}
            elif reused:
                result = {'ok': True, 'data': reused[1]}
                trace.setdefault('reused_source_queries', []).append({
                    'source_ref': reused[0], 'model_call': trace['model_calls'],
                    'arguments_sha256': fingerprint, 'new_database_query': False})
            else:
                with readonly_session(self.engine, timeout_ms=bounded.database_tool_timeout_ms) as session:
                    context = ToolContext(session, self.release, self.store, bounded, self.question,
                        self.transport, self.previous_answer, self.previous_run_id, analysis_evidence=state['evidence'])
                    result = execute_tool(dispatch, call.arguments, context)
            self.progress.read(call.name, progress_module, call.arguments, result)
            if result.get('ok'):
                if call.name in {'analysis_search', 'analysis_read'}:
                    data = result['data']
                    entry = {'tool': call.name, 'tool_call_id': call.id, 'model_call': trace['model_calls'],
                             'arguments_sha256': fingerprint, 'result_sha256': services.content_digest(result)}
                    if call.name == 'analysis_search':
                        entry.update(query=data['query'], theme_id=data['theme_id'], **retrieval_execution_counts(data))
                    else:
                        entry.update(evidence_ref=data['evidence_ref'], source_evidence_ref=data['source_evidence_ref'],
                                     start=data['start'], end=data['end'], quote_char_count=len(data['quote']))
                    state['retrieval_audit'].append(entry)
                if call.name in {'plan_task', 'organize_evidence'}:
                    p.observe('plan', services.content_digest(result['data']))
                    trace.setdefault('task_plans' if call.name == 'plan_task' else 'evidence_organizations', []).append(copy.deepcopy(result['data']))
                for item in state['evidence'].get('items', {}).values():
                    p.observe('evidence', services.content_digest({k: item.get(k) for k in
                        ('record_id', 'selector', 'start', 'end', 'quote', 'source_kind')}))
                if call.name in proposals or call.name == 'read_source_query':
                    answer = result['data']
                    origin = (answer.get('query_interpretation', {}).get('origin') if call.name in
                              {'source_query', 'read_source_query', 'compose_source_answer'}
                              else answer.get('dialogue', {}).get('interpretation_origin'))
                    if (answer.get('question') != self.question or answer.get('release_id') != self.release.id
                            or answer.get('manifest_hash') != self.release.manifest_hash
                            or answer.get('module') != target or origin != 'model_query_proposal'
                            or answer.get('dialogue', {}).get('proposal_repair_request')):
                        return self.stop(state, 'query_commit_integrity_failure')
                    source_ref = None
                    if call.name in {'source_query', 'read_source_query'}:
                        try:
                            source_ref = graph_synthesis.register(state, answer, call.arguments)
                        except ValueError:
                            return self.stop(state, 'typed_source_registry_limit')
                        p.observe('evidence', source_ref)
                    if call.name == 'read_source_query':
                        self.observe(state, call, {'ok': True, 'data': {'source_ref': source_ref,
                            'source_query': call.arguments, 'verified_source_result': answer,
                            'next_step': '证据能回答所问时，立即用compose_source_answer提交；只有会改变结论的证据仍缺失才继续查。'}})
                        continue
                    digest = services.content_digest(answer)
                    p.observe('proposal', digest)
                    records = trace.setdefault('staged_source_answers', [])
                    ref = f'staged_answer_{len(records) + 1}'
                    state['staged'] = {'ref': ref, 'sha256': digest, 'answer': copy.deepcopy(answer),
                        'tool': call.name, 'arguments': copy.deepcopy(call.arguments), 'model_first': True,
                        'task_contract': {
                            'initial_plan': copy.deepcopy(trace.get('task_plans', [None])[0]),
                            'active_plan': copy.deepcopy(state['evidence'].get('task_plan')),
                            'hypotheses_are_verified_facts': False},
                        'prior_reviews': copy.deepcopy(trace.get('semantic_reviews', [])[-8:]),
                        'retrieval_audit': review_retrieval_audit(state['retrieval_audit'], state['messages'])}
                    trace['retrieval_execution_audit'] = copy.deepcopy(state['staged']['retrieval_audit'])
                    records.append({'answer_ref': ref, 'answer_sha256': digest, 'committed_at_staging': False})
                    self.observe(state, call, {'ok': True, 'data': {'answer_ref': ref, 'answer_sha256': digest,
                        **({'source_ref': source_ref, 'verified_source_result': answer} if source_ref else {}),
                        'status': 'source_verified_pending_semantic_review'}})
                    state['progress'] = _dump_progress(p)
                    state['route'] = 'prepare_review'
                    return state
            self.observe(state, call, result)
        state['progress'] = _dump_progress(p)
        state['route'] = 'prepare'
        return state

    def prepare_review(self, state):
        from arknights_agent.runtime.runner import (
            _execution_budget_feedback,
            _semantic_review_messages,
        )
        self.mark(state, 'prepare_review')
        state['review_messages'] = _semantic_review_messages(self.public_input, self.previous_context, state['staged'])
        _execution_budget_feedback(state['review_messages'],
            model_remaining=self.settings.agent_max_model_calls - state['trace']['model_calls'],
            tool_remaining=self.settings.agent_max_tool_calls - state['trace']['tool_calls'],
            review_phase=True, max_output_tokens=self.settings.agent_max_output_tokens)
        state['route'] = 'review'
        return state

    def review_decision(self, state):
        self.mark(state, 'review_decision')
        calls = ModelTurn.model_validate(state['turn']).tool_calls
        staged, trace = state['staged'], state['trace']
        if len(calls) != 1 or trace['tool_calls'] >= self.settings.agent_max_tool_calls:
            return self.stop(state, 'semantic_review_requires_single_decision')
        call = calls[0]
        args = call.arguments
        trace['tool_calls'] += 1
        allowed = {'answer_ref', 'answer_sha256', 'decision', 'reason', 'blocking_issues',
                   'resolved_previous_issues', 'source_correction_evidence_refs'}
        valid = (call.name == 'review_source_answer' and not set(args) - allowed
            and args.get('answer_ref') == staged['ref'] and args.get('answer_sha256') == staged['sha256']
            and services.content_digest(staged['answer']) == staged['sha256']
            and args.get('decision') in ('accept', 'revise')
            and isinstance(args.get('reason'), str) and 1 <= len(args['reason']) <= 1200
            and all(isinstance(args.get(k, []), list) and len(args.get(k, [])) <= 8
                and all(isinstance(v, str) and 1 <= len(v) <= 1000 for v in args.get(k, []))
                for k in ('blocking_issues', 'resolved_previous_issues')))
        valid = valid and not (args.get('decision') == 'accept' and args.get('blocking_issues'))
        if not valid:
            state['review_messages'].extend([
                ModelTurn.model_validate(state['turn']).assistant_message(),
                {'role': 'tool', 'tool_call_id': call.id, 'content': services.canonical_json(
                    {'ok': False, 'error': {'code': 'invalid_semantic_review_reference'}})}])
            state['route'] = 'review'
            return state
        state['decision'] = copy.deepcopy(args)
        trace.setdefault('semantic_reviews', []).append({**copy.deepcopy(args), 'model_call': trace['model_calls']})
        trace['steps'].append({'tool': call.name, 'status': 'semantic_accepted_and_delivered'
                              if args['decision'] == 'accept' else 'semantic_revision_requested'})
        state['route'] = 'commit' if args['decision'] == 'accept' else 'revise'
        return state

    def revise(self, state):
        from arknights_agent.runtime.runner import _review_recovery_guidance
        self.mark(state, 'revise')
        state['rejected_proposal'] = {
            'fingerprint': services.content_digest({'name': state['staged']['tool'],
                                                    'arguments': state['staged']['arguments']}),
            'generation': _load_progress(state['progress']).generation}
        state['messages'].extend([
            {'role': 'assistant', 'content': services.canonical_json({
                'actual_model_semantic_review_feedback_data_only': state['decision'],
                'rejected_typed_query': state['staged']['arguments'], 'answer_not_delivered': True})},
            {'role': 'system', 'content': _review_recovery_guidance(
                analysis_enabled=self.analysis_enabled, source_query_available=True) +
                '需要综合多个已核验查询时用read_source_query取证，再用compose_source_answer引用结果改写正文。'}])
        state['staged'] = None
        state['route'] = 'prepare'
        return state

    def commit(self, state):
        self.mark(state, 'commit')
        staged = state['staged']
        if state['decision']['decision'] != 'accept' or services.content_digest(staged['answer']) != staged['sha256']:
            return self.stop(state, 'query_commit_integrity_failure')
        self.progress.show("saving", module=staged['answer'].get('module'))
        state['trace'].update(model_interpreted=True, server_committed=True,
            submission_mode='verified_query_semantic_review',
            used='deepseek_simulation' if self.provider.simulation else 'deepseek',
            committed_answer_hash=staged['sha256'], selected_answer_tool=staged['tool'])
        state['selected'] = staged['answer']
        return self.stop(state, 'source_verified_query_semantically_reviewed')

    def build(self, saver):
        graph = StateGraph(AgentState)
        nodes = {'prepare': self.prepare, 'plan': lambda s: self.model(s, 'plan'),
                 'reason': lambda s: self.model(s, 'reason'), 'tools': self.tools,
                 'prepare_review': self.prepare_review, 'review': lambda s: self.model(s, 'review'),
                 'review_decision': self.review_decision, 'revise': self.revise, 'commit': self.commit}
        for name, node in nodes.items():
            graph.add_node(name, node)
        graph.add_edge(START, 'prepare')
        for name in nodes:
            graph.add_conditional_edges(name, lambda s: s['route'], {**{n: n for n in nodes}, 'end': END})
        return graph.compile(checkpointer=saver)

    def run(self):
        config = {'configurable': {'thread_id': self.execution_id},
                  'recursion_limit': self.settings.agent_max_model_calls * 8 + 16}
        # Local persistence only. Never implicitly export prompts or game sources.
        with tracing_context(enabled=False), sqlite3.connect(self.path, check_same_thread=False) as conn:
            graph = self.build(SqliteSaver(conn))
            saved = graph.get_state(config)
            if saved.values and saved.values.get('identity') != self.initial['identity']:
                return self.finish('graph_checkpoint_identity_mismatch')
            if saved.values and not saved.next:
                result = saved.values
            else:
                result = graph.invoke(None if saved.values else self.initial, config, durability='sync')
        self.trace.clear()
        self.trace.update(result['trace'])
        return self.finish(result['reason'], selected=result.get('selected'))


def run_graph_agent(**kwargs):
    return GraphAgent(**kwargs).run()
