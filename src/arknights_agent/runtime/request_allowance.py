"""Append an exact public question plan without increasing the money limit."""
import copy
import json
from datetime import UTC

from arknights_agent.runtime.budget import CAMPAIGN_ID, BudgetError
from arknights_agent.runtime.budget_extension import TABLES, digest, effective_limits

AUTHORIZATION = ('继续扩大真实测试，把七个肉鸽更多藏品、事件和结局的问题都测一遍，'
                 '发现遗漏和引用错误就修，此外你对deepseek的token开销计算有误，'
                 '现在还有12块钱，这是你的用量')
BASE_TABLES = (*TABLES, 'budget_extension')


def snapshot(db):
    return {name: [dict(r) for r in db.execute(f'SELECT * FROM {name} ORDER BY 1')]
            for name in BASE_TABLES}


def public_identity(case):
    return digest({key: case[key] for key in ('question', 'module', 'release_id')})


def build_receipt(baseline, plan, *, authorization=AUTHORIZATION):
    receipt = {'authorization_text': authorization, 'campaign_id': CAMPAIGN_ID,
               'baseline': copy.deepcopy(baseline), 'baseline_sha256': digest(baseline),
               'plan': copy.deepcopy(plan), 'plan_sha256': digest(plan),
               'baseline_count': 273, 'max_requests': len(plan) * 3,
               'additional_money_cny': '0', 'budget_cny': '28', 'cap_cny': '22.40000000'}
    receipt['id'] = digest(receipt)
    return validate(receipt)


def validate(receipt):
    try:
        b, plan = receipt['baseline'], receipt['plan']
        valid = (set(receipt) == {'authorization_text', 'campaign_id', 'baseline', 'baseline_sha256',
                                 'plan', 'plan_sha256', 'baseline_count', 'max_requests',
                                 'additional_money_cny', 'budget_cny', 'cap_cny', 'id'}
                 and receipt['authorization_text'] == AUTHORIZATION
                 and receipt['campaign_id'] == CAMPAIGN_ID
                 and set(b) == set(BASE_TABLES) and len(b['campaign']) == 1
                 and len(b['reservation']) == receipt['baseline_count'] == 273
                 and len({r['reservation_id'] for r in b['reservation']}) == 273
                 and sum(r['state'] == 'settled' for r in b['reservation']) == 271
                 and sum(r['state'] == 'unknown' for r in b['reservation']) == 2
                 and b['campaign'][0]['frozen'] == 0
                 and len(b['bounded_continuation']) == 2 and len(b['budget_extension']) == 1
                 and receipt['additional_money_cny'] == '0' and receipt['budget_cny'] == '28'
                 and receipt['cap_cny'] == '22.40000000'
                 and len(plan) == 226 and receipt['max_requests'] == 678
                 and all(set(c) == {'case_id', 'question', 'module', 'release_id'}
                         and all(isinstance(v, str) and v for v in c.values())
                         and c['module'] == 'auto' for c in plan)
                 and len({c['case_id'] for c in plan}) == len(plan)
                 and len({public_identity(c) for c in plan}) == len(plan)
                 and digest(b) == receipt['baseline_sha256'] and digest(plan) == receipt['plan_sha256']
                 and digest({k: v for k, v in receipt.items() if k != 'id'}) == receipt['id'])
    except (KeyError, TypeError, ValueError, IndexError):
        valid = False
    if not valid:
        raise BudgetError('request_allowance_invalid')
    return receipt


def verify_baseline(db, receipt):
    current = snapshot(db)
    ids = {r['reservation_id'] for r in receipt['baseline']['reservation']}
    current['reservation'] = [r for r in current['reservation'] if r['reservation_id'] in ids]
    # A later unknown can freeze the campaign, but cannot alter the prior rows.
    for key in ('frozen', 'freeze_code'):
        current['campaign'][0][key] = receipt['baseline']['campaign'][0][key]
    if digest(current) != receipt['baseline_sha256']:
        raise BudgetError('request_allowance_history_changed')


def register(book, receipt):
    validate(receipt)
    with book._transaction() as db:
        status = book._summary(db)
        if status['frozen'] or status['inflight_count'] or status['price_verification_status'] != 'ready':
            raise BudgetError('request_allowance_campaign_not_ready')
        verify_baseline(db, receipt)
        budget, cap = effective_limits(db, db.execute('SELECT * FROM campaign').fetchone())
        if str(budget) != receipt['budget_cny'] or str(cap) != receipt['cap_cny']:
            raise BudgetError('request_allowance_cap_changed')
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='request_allowance'").fetchone()
        if exists:
            rows = db.execute('SELECT * FROM request_allowance').fetchall()
            if len(rows) == 1 and rows[0]['id'] == receipt['id'] and json.loads(rows[0]['receipt_json']) == receipt:
                return {'id': receipt['id'], 'reused': True}
            raise BudgetError('request_allowance_already_registered')
        if status['reservation_count'] != receipt['baseline_count']:
            raise BudgetError('request_allowance_baseline_count_changed')
        db.execute('CREATE TABLE request_allowance (id TEXT PRIMARY KEY, receipt_json TEXT NOT NULL, authorized_at TEXT NOT NULL)')
        db.execute('INSERT INTO request_allowance VALUES (?,?,?)',
                   (receipt['id'], json.dumps(receipt, ensure_ascii=False, sort_keys=True),
                    book.clock().astimezone(UTC).isoformat()))
        return {'id': receipt['id'], 'reused': False}


def request_limit(db, bound, original_limit):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='request_allowance'").fetchone():
        return original_limit
    rows = db.execute('SELECT * FROM request_allowance').fetchall()
    if len(rows) != 1:
        raise BudgetError('request_allowance_registration_invalid')
    receipt = validate(json.loads(rows[0]['receipt_json']))
    if rows[0]['id'] != receipt['id']:
        raise BudgetError('request_allowance_registration_invalid')
    verify_baseline(db, receipt)
    from arknights_agent.runtime.rag_expansion_allowance import request_limit as expansion_limit
    from arknights_agent.runtime.rag_recheck_allowance import request_limit as rag_limit
    from arknights_agent.runtime.stage21_public_allowance import guard as stage21_guard
    from arknights_agent.runtime.stage22_public_allowance import guard as stage22_guard

    stage22_limit = stage22_guard(db, bound)
    if stage22_limit is not None:
        return stage22_limit
    stage21_limit = stage21_guard(db, bound)
    if stage21_limit is not None:
        return stage21_limit
    expanded_limit = expansion_limit(db, bound)
    if expanded_limit is not None:
        return expanded_limit
    recheck_limit = rag_limit(db, bound)
    if recheck_limit is not None:
        return recheck_limit
    if bound.public_input_sha256 not in {public_identity(c) for c in receipt['plan']}:
        raise BudgetError('request_outside_authorized_plan')
    return receipt['baseline_count'] + receipt['max_requests']
