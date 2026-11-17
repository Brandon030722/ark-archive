"""Four-turn public regression with a synthetic evidence release and no paid transport."""
import json
import sqlite3

import pytest
from test_runtime_runner import call, response
from test_stage52_semantic_review import review
from test_stage65_analysis_runtime import QUESTION, args_for, last_hits, search_call

from arknights_agent import agent
from arknights_agent.runtime.long_context import CONTEXT_TOKENS, MAX_WIRE_BYTES


def plan(question=QUESTION, index=0):
    return {"task": "分析完整前文的高难构筑机制和证据边界", "question_indices": [index],
            "context_resolution": "主题补充延续原来的机制问题，未确认简称保持假设",
            "conditions": ["原问的高难范围"], "evidence_questions": ["什么条件影响该构筑"],
            "search_queries": ["共享藏品 完整效果"], "uncertainties": ["尚未确认循环的具体机制"]}


def exercise_dialogue(knowledge, tmp_path, monkeypatch, invoke):
    monkeypatch.setattr(agent, 'dispatch_question', lambda *_a, **_k: pytest.fail('no oral parser'))
    parent = None
    for turn, question in enumerate([QUESTION, '界园', '不是看原文，我问原因', '不到12会变吗？']):
        chosen = {}
        def organize(wire):
            hit = next(h for h in last_hits(wire) if '完整效果' in h['quote'])
            chosen.update(hit)
            return response([call('organize_evidence', {'groups': [{
                'question': '机制与范围', 'observations': [{'evidence_ref': hit['evidence_ref']}],
                'interpretation': '模型判断为效果原文，不能单凭该文证明高难循环',
                'missing_evidence': ['高难适用条件']} ]}, 'organize')])
        def submit(wire, revised=False):
            args = args_for(chosen, question=question, current_index=turn)
            args['observations'] = [{'evidence_ref': chosen['evidence_ref']}]
            if revised:
                args['limitations'].append('修订后进一步明确：该资料不能支持循环绝对失效结论。')
                args['review_response'] = '已明确资料不能支持绝对失效结论。'
            return response([call('evidence_analysis', args, 'revision' if revised else 'proposal')])
        answer, wires, _ = invoke(knowledge, tmp_path / str(turn), [
            response([call('plan_task', plan(question, turn), 'plan')]), search_call(), organize, submit,
            lambda w: review(w, 'revise', reason='需要明确资料不足以支持循环绝对失效',
                             blocking_issues=['缺少对绝对失效结论的限制'], resolved_previous_issues=[]),
            lambda w: submit(w, True), review], question=question,
            previous_run_id=parent['run_id'] if parent else None,
            agent_model_first=True, agent_large_output_enabled=True, agent_max_model_calls=32,
            agent_max_tool_calls=96, agent_max_prompt_bytes=MAX_WIRE_BYTES,
            agent_max_total_tokens=CONTEXT_TOKENS * 32, agent_max_total_seconds=180,
            tool_result_max_bytes=262144)
        assert answer['agent_runtime']['server_committed'], answer['agent_runtime']['reason']
        assert answer['agent_runtime']['model_calls'] == 7
        first_tools = {s['function']['name'] for s in wires[0]['tools']}
        assert 'plan_task' in first_tools
        allowed_first = {'plan_task', 'source_query', 'roguelike_query', 'read_source_query'}
        if answer['agent_runtime'].get('execution_engine') == 'langgraph':
            allowed_first |= {'analysis_search', 'query_records', 'rag_search'}
        assert first_tools <= allowed_first
        assert '正文允许有据概括和推理' in wires[0]['messages'][0]['content']
        assert '只为明日方舟公开百科选只读工具，不写事实' not in wires[0]['messages'][0]['content']
        assert len(answer['analysis_request']['original_user_questions']) == turn + 1
        assert answer['agent_runtime']['task_plans'] and answer['agent_runtime']['evidence_organizations']
        if turn:
            prefix = '\n'.join(m.get('content', '') for m in wires[0]['messages'])
            assert 'authentic_visible_dialogue_data_only' in prefix
            assert '已明确资料不能支持绝对失效结论' not in prefix  # Private revision feedback is not visible history.
            assert '修订后进一步明确' in prefix
        assert chosen['quote'] not in answer['answer']
        assert answer['source_observations'][0]['quote'] == chosen['quote']
        assert '资料依据：' not in answer['answer']
        feedback = json.loads(wires[-1]['messages'][-1]['content'])
        assert '缺少对绝对失效结论的限制' in json.dumps(feedback, ensure_ascii=False)
        assert '已明确资料不能支持绝对失效结论' in json.dumps(feedback, ensure_ascii=False)
        for wire in wires:
            for spec in wire['tools']:
                if spec['function']['name'] in {'organize_evidence', 'evidence_analysis'}:
                    assert 'quote' not in spec['function']['parameters']['$defs']['AnalysisObservation']['properties']
        parent = agent.record_run(knowledge.session, knowledge.release, question, answer)
        knowledge.session.commit()


def check_graph_checkpoints(tmp_path):
    paths = list(tmp_path.rglob('langgraph-state.sqlite3'))
    assert len(paths) == 4
    for path in paths:
        with sqlite3.connect(path) as db:
            assert db.execute('select count(*) from checkpoints').fetchone()[0] > 7
            rows = db.execute('select outcome from graph_model_call').fetchall()
            assert len(rows) == 7 and all(row[0] for row in rows)
            final = json.loads(rows[-1][0])['trace']
            assert final['execution_engine'] == 'langgraph'
            assert 'revise' in final['graph_nodes']
