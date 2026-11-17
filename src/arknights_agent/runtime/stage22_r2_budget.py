"""Review-only Stage22 R2 fixed-plan ledger draft.

No registration or HTTP happens on import. Existing reviewed source files remain
unchanged. This explicit successor owns future request selection; it validates
all frozen R1 facts and receipts instead of pretending R1 authorized new input.
Native R1 interfaces remain independently selectable under their original scope;
reviewed R2 operation requires quiescence and fails closed on any R1 append.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from arknights_agent import services
from arknights_agent.runtime import request_allowance as original_requests
from arknights_agent.runtime import stage22_money_grant as money
from arknights_agent.runtime import stage22_public_allowance as r1
from arknights_agent.runtime.bounded_profile import (
    BOUNDED_INPUT_TOKENS,
    STAGE16_PROFILE_ID,
    BoundedRequest,
)
from arknights_agent.runtime.bounded_profile import (
    validate_receipt as continuation_validate,
)
from arknights_agent.runtime.budget import (
    CAMPAIGN_ID,
    INPUT_PRICE_FLOOR,
    MAX_OUTPUT_TOKENS,
    OUTPUT_PRICE_FLOOR,
    BudgetError,
    BudgetLedger,
    _cost,
    _decimal,
    _money,
)
from arknights_agent.runtime.request_allowance import digest, public_identity

TABLE = 'stage22_r2_public_allowance'
ATTEMPT_TABLE = 'stage22_r2_request_attempt'
SCOPE = 'one_additional_fixed_103_development_37_fresh_holdout_repair_round_within_32_cny'
MAX_ATTEMPTS = 3
MAX_REQUESTS = 420
REVIEW_TTL = timedelta(minutes=10)
REVIEW_SAFEGUARDS = (
    'original_continued_repair_scope_verified', 'money_scope_unchanged',
    'r1_collectors_quiescent_and_round_complete', 'r1_final_artifacts_frozen',
    'new_heldout_unseen_until_implementation_freeze', 'new_heldout_source_oracle_verified',
    'new_exact_public_plan_verified', 'historical_rows_receipts_attempts_immutable',
    'original_bounded_source_and_continuation_gates_retained',
    'explicit_future_selector_migration_reviewed', 'no_counter_reset_or_budget_increase',
    'unknown_hold_freeze_and_no_automatic_retry_retained',
)
PROTECTIONS = {
    f'stage22_r2_{table}_{op.lower()}': (
        f'CREATE TRIGGER stage22_r2_{table}_{op.lower()} BEFORE {op} ON {table} '
        "BEGIN SELECT RAISE(ABORT, 'stage22_r2_append_only'); END"
    ) for table in (TABLE, ATTEMPT_TABLE) for op in ('UPDATE', 'DELETE')
}


def require(value, code='stage22_r2_receipt_invalid'):
    if not value:
        raise BudgetError(code)


def implementation_paths():
    module = Path(__file__).resolve()
    source = r1.prior.SOURCE_ROOT
    if module.parent == source / 'src/arknights_agent/runtime':
        test = source / 'tests/test_stage22_r2_budget.py'
    else:
        require(module.parent == source / '.local/stage22-r2-preparation/budget',
                'stage22_r2_implementation_location_invalid')
        test = module.parent / 'tests/test_stage22_r2_budget.py'
    return (*r1.REVIEWED_FILES, str(module.relative_to(source)), str(test.relative_to(source)))


def implementation_hashes():
    return {name: hashlib.sha256((r1.prior.SOURCE_ROOT / name).read_bytes()).hexdigest()
            for name in implementation_paths()}


def snapshot(db):
    value = money.snapshot(db)
    value[money.TABLE] = [dict(row) for row in db.execute(f'SELECT * FROM {money.TABLE} ORDER BY 1')]
    value['money_grant_protections'] = r1._triggers(db, money.TABLE)
    return value


def baseline_validate(baseline):
    """Every R1 response is finished; old native receipts stay native and exact."""
    require(len(baseline[r1.TABLE]) == len(baseline[money.TABLE]) == 1)
    r1_row = baseline[r1.TABLE][0]
    old = r1.validate(json.loads(r1_row['receipt_json']))
    require(r1_row['id'] == old['id'] and r1_row['review_sha256'] == old['review_sha256'])
    grant_row = baseline[money.TABLE][0]
    grant = money.validate(json.loads(grant_row['receipt_json']))
    require(grant_row['id'] == grant['id'] and grant_row['review_sha256'] == grant['review_sha256'])
    require(grant['allowance_id'] == old['id'])
    require(baseline['money_grant_protections'] == money.PROTECTIONS)
    require(baseline['stage22_attempt_protections'] == r1.PROTECTIONS)
    # The first 1459 reservations, all original registrations, and old attempts
    # match the exact native R1 receipt; no new semantic authorization is inferred.
    old_ids = {row['reservation_id'] for row in old['baseline']['reservation']}
    before = {key: copy.deepcopy(value) for key, value in baseline.items() if key not in {
        r1.TABLE, r1.ATTEMPT_TABLE, 'stage22_attempt_protections',
        money.TABLE, 'money_grant_protections',
    }}
    before['reservation'] = [row for row in before['reservation'] if row['reservation_id'] in old_ids]
    require(before == old['baseline'])
    rows = baseline['reservation']
    require(len(rows) == len({row['reservation_id'] for row in rows}))
    require(baseline['campaign'][0]['frozen'] == 0 and baseline['campaign'][0]['freeze_code'] is None)
    first_round = [row for row in rows if row['reservation_id'] not in old_ids]
    for row in first_round:
        r1._reservation_shape(row, 'settled')
    attempts = baseline[r1.ATTEMPT_TABLE]
    require(140 <= len(first_round) == len(attempts) <= MAX_REQUESTS)
    require({row['reservation_id'] for row in attempts} == {row['reservation_id'] for row in first_round})
    counts = Counter(row['public_input_sha256'] for row in attempts)
    identities = {public_identity(case) for case in old['plan']}
    require(set(counts) == identities and all(1 <= n <= MAX_ATTEMPTS for n in counts.values()))
    require(all(row['allowance_id'] == old['id'] for row in attempts))
    require(len([row for row in rows if row['state'] == 'unknown']) == 3)
    require(not any(row['state'] == 'reserved' for row in rows))
    return old, grant


def review_template(baseline, plan, *, split_evidence_sha256, r1_result_manifest_sha256):
    return {
        'reviewer': 'independent_agent', 'passed': False, 'checked_at': None,
        'authorization_text': r1.AUTHORIZATION, 'reviewed_scope_interpretation': SCOPE,
        'baseline_sha256': digest(baseline), 'plan_sha256': digest(plan),
        'reviewed_files_sha256': implementation_hashes(),
        'runtime_implementation_hash': services.implementation_hash(),
        'split_evidence_sha256': split_evidence_sha256,
        'r1_result_manifest_sha256': r1_result_manifest_sha256,
        **dict.fromkeys(REVIEW_SAFEGUARDS, False),
    }


def build_receipt(baseline, plan, independent_review_path):
    raw = Path(independent_review_path).read_text(encoding='utf-8')
    value = {
        'campaign_id': CAMPAIGN_ID, 'authorization_text': r1.AUTHORIZATION,
        'reviewed_scope_interpretation': SCOPE,
        'baseline': copy.deepcopy(baseline), 'baseline_sha256': digest(baseline),
        'baseline_count': len(baseline['reservation']), 'plan': copy.deepcopy(plan),
        'plan_sha256': digest(plan), 'development_count': 103, 'fresh_holdout_count': 37,
        'max_attempts_per_turn': 3, 'max_additional_requests': 420,
        'absolute_limit': len(baseline['reservation']) + 420,
        'additional_money_cny': '0', 'budget_cny': '32.00000000', 'cap_cny': '25.60000000',
        'unknown_costs_released': False, 'independent_review_json': raw,
        'review_sha256': hashlib.sha256(raw.encode()).hexdigest(),
    }
    value['id'] = digest(value)
    return validate(value)


def validate(receipt):
    try:
        require(set(receipt) == {
            'campaign_id', 'authorization_text', 'reviewed_scope_interpretation', 'baseline',
            'baseline_sha256', 'baseline_count', 'plan', 'plan_sha256', 'development_count',
            'fresh_holdout_count', 'max_attempts_per_turn', 'max_additional_requests',
            'absolute_limit', 'additional_money_cny', 'budget_cny', 'cap_cny',
            'unknown_costs_released', 'independent_review_json', 'review_sha256', 'id',
        })
        b, plan = receipt['baseline'], receipt['plan']
        old, _ = baseline_validate(b)
        require(receipt['campaign_id'] == CAMPAIGN_ID and receipt['authorization_text'] == r1.AUTHORIZATION
                and receipt['reviewed_scope_interpretation'] == SCOPE)
        require(receipt['baseline_count'] == len(b['reservation'])
                and receipt['absolute_limit'] == receipt['baseline_count'] + MAX_REQUESTS
                and receipt['max_attempts_per_turn'] == 3 and receipt['max_additional_requests'] == 420
                and receipt['development_count'] == 103 and receipt['fresh_holdout_count'] == 37)
        require(receipt['additional_money_cny'] == '0' and receipt['budget_cny'] == '32.00000000'
                and receipt['cap_cny'] == '25.60000000' and receipt['unknown_costs_released'] is False)
        require(isinstance(plan, list) and len(plan) == 140 and plan[:103] == old['plan'][:103])
        require(all(set(case) == {'case_id', 'question', 'module', 'release_id'}
                    and isinstance(case['case_id'], str) and 1 <= len(case['case_id']) <= 128
                    and r1.prior._public_question(case['question']) and case['module'] == 'auto'
                    and case['release_id'] == r1.prior.RELEASE_ID for case in plan))
        require(len({case['case_id'] for case in plan}) == len({public_identity(case) for case in plan}) == 140)
        require(all(case['case_id'].startswith('stage22-r2-holdout-') for case in plan[103:]))
        seen = {public_identity(case) for case in old['plan']}
        seen.update(public_identity(case) for case in json.loads(
            old['baseline'][r1.prior.TABLE][0]['receipt_json'])['plan'])
        require(not seen.intersection(public_identity(case) for case in plan[103:]))
        require(digest(b) == receipt['baseline_sha256'] and digest(plan) == receipt['plan_sha256'])
        raw = receipt['independent_review_json']
        require(hashlib.sha256(raw.encode()).hexdigest() == receipt['review_sha256'])
        review = json.loads(raw)
        require(set(review) == {
            'reviewer', 'passed', 'checked_at', 'authorization_text', 'reviewed_scope_interpretation',
            'baseline_sha256', 'plan_sha256', 'reviewed_files_sha256', 'runtime_implementation_hash',
            'split_evidence_sha256', 'r1_result_manifest_sha256', *REVIEW_SAFEGUARDS,
        } and review['reviewer'] == 'independent_agent'
                and review['passed'] is True and all(review[k] is True for k in REVIEW_SAFEGUARDS))
        require(all(review[k] == receipt[k] for k in (
            'authorization_text', 'reviewed_scope_interpretation', 'baseline_sha256', 'plan_sha256',
        )))
        require(set(review['reviewed_files_sha256']) == set(implementation_paths()))
        require(all(r1.prior._hash(value) for value in review['reviewed_files_sha256'].values()))
        require(all(r1.prior._hash(review[k]) for k in (
            'runtime_implementation_hash', 'split_evidence_sha256', 'r1_result_manifest_sha256',
        )))
        require(datetime.fromisoformat(review['checked_at']).tzinfo is not None)
        require(receipt['id'] == digest({k: v for k, v in receipt.items() if k != 'id'}))
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        raise BudgetError('stage22_r2_receipt_invalid') from None
    return receipt


@lru_cache(maxsize=1)
def _validated_native_blob(raw):
    # Exact complete native text is the key. This function reads no DB or
    # current file content. It caches only deterministic receipt validation.
    return validate(json.loads(raw))


def _receipt_from_native_blob(raw):
    # Never expose the mutable cached object to a caller. Immutable strings
    # stay shared; every nested dict/list returned here is a private copy.
    return copy.deepcopy(_validated_native_blob(raw))


def registered(db):
    if not db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (TABLE,)).fetchone():
        return None
    rows = db.execute(f'SELECT * FROM {TABLE}').fetchall()
    require(len(rows) == 1, 'stage22_r2_registration_invalid')
    row = rows[0]
    receipt = _receipt_from_native_blob(row['receipt_json'])
    require(row['id'] == receipt['id'] and row['review_sha256'] == receipt['review_sha256'],
            'stage22_r2_registration_invalid')
    protections = {**r1._triggers(db, TABLE), **r1._triggers(db, ATTEMPT_TABLE)}
    require(protections == PROTECTIONS, 'stage22_r2_protection_invalid')
    now = snapshot(db)
    ids = {row['reservation_id'] for row in receipt['baseline']['reservation']}
    now['reservation'] = [row for row in now['reservation'] if row['reservation_id'] in ids]
    for key in ('frozen', 'freeze_code'):
        now['campaign'][0][key] = receipt['baseline']['campaign'][0][key]
    require(now == receipt['baseline'], 'stage22_r2_history_changed')
    review = json.loads(receipt['independent_review_json'])
    require(review['reviewed_files_sha256'] == implementation_hashes(), 'stage22_r2_implementation_changed')
    require(review['runtime_implementation_hash'] == services.implementation_hash(),
            'stage22_r2_runtime_changed')
    return receipt


def bounded_proof_gate(book, db, bound, continuation_id):
    """Original continuation/source proof and original request history gates.

    This is the exact proof portion of BudgetLedger._bounded_request_limit.
    Only its downstream active-round selector is explicitly succeeded by R2.
    Calling its original request_limit would select R1 again and reject all
    R2 heldout inputs; no fake BoundRequest or monkeypatch is used here.
    """
    require(isinstance(bound, BoundedRequest) and continuation_id == bound.continuation_id,
            'bounded_request_proof_invalid')
    bound.validate()
    row = db.execute('SELECT * FROM bounded_continuation WHERE continuation_id=? AND campaign_id=?',
                     (continuation_id, CAMPAIGN_ID)).fetchone()
    require(row is not None, 'continuation_not_authorized')
    receipt = continuation_validate(json.loads(row['receipt_json']))
    require(bound.profile_id == receipt['profile_id'] == STAGE16_PROFILE_ID, 'bounded_request_proof_invalid')
    for registration in db.execute('SELECT * FROM bounded_continuation'):
        other = continuation_validate(json.loads(registration['receipt_json']))
        require(registration['campaign_id'] == CAMPAIGN_ID
                and registration['continuation_id'] == other['continuation_id'], 'continuation_parent_invalid')
        require(other.get('parent_continuation_id') != continuation_id, 'continuation_superseded')
    book._validate_continuation_baseline(db, receipt)
    prior_rows = db.execute('SELECT * FROM request_allowance').fetchall()
    require(len(prior_rows) == 1, 'request_allowance_registration_invalid')
    original = original_requests.validate(json.loads(prior_rows[0]['receipt_json']))
    require(prior_rows[0]['id'] == original['id'], 'request_allowance_registration_invalid')
    original_requests.verify_baseline(db, original)


def guard(db, bound):
    receipt = registered(db)
    require(receipt is not None, 'stage22_r2_not_registered')
    require(bound.profile_id == STAGE16_PROFILE_ID
            and bound.public_input_sha256 in {public_identity(case) for case in receipt['plan']},
            'stage22_r2_request_outside_plan')
    attempts = [dict(row) for row in db.execute(f'SELECT * FROM {ATTEMPT_TABLE}')]
    old_ids = {row['reservation_id'] for row in receipt['baseline']['reservation']}
    new_rows = [dict(row) for row in db.execute('SELECT * FROM reservation') if row['reservation_id'] not in old_ids]
    require({row['reservation_id'] for row in attempts} == {row['reservation_id'] for row in new_rows}
            and all(row['allowance_id'] == receipt['id'] and row['public_input_sha256'] in {
                public_identity(case) for case in receipt['plan']} for row in attempts),
            'stage22_r2_attempt_history_invalid')
    for row in new_rows:
        r1._reservation_shape(row, 'settled')
    require(sum(row['public_input_sha256'] == bound.public_input_sha256 for row in attempts) < MAX_ATTEMPTS,
            'stage22_r2_turn_request_limit_reached')
    require(len(attempts) < MAX_REQUESTS, 'campaign_request_limit_reached')
    return receipt


class Stage22R2BudgetLedger(BudgetLedger):
    """Opt-in successor; no production default or old source file is modified."""

    def _summary(self, db, reservation=None):
        value = super()._summary(db, reservation)
        registered(db)
        return value

    def authorize_r2_public_plan(self, receipt):
        validate(receipt)
        with self._transaction() as db:
            status = self._summary(db)
            old = registered(db)
            if old is not None:
                require(old == receipt, 'stage22_r2_already_registered')
                return {'id': receipt['id'], 'reused': True, **status}
            require(not status['frozen'], 'campaign_frozen')
            require(status['inflight_count'] == 0, 'request_inflight')
            require(status['price_verification_status'] == 'ready', status['price_verification_status'])
            require(snapshot(db) == receipt['baseline'], 'stage22_r2_baseline_changed')
            require(status['budget_cny'] == '32.00000000' and status['cap_cny'] == '25.60000000',
                    'stage22_r2_money_scope_changed')
            review = json.loads(receipt['independent_review_json'])
            age = self.clock().astimezone(UTC) - datetime.fromisoformat(review['checked_at']).astimezone(UTC)
            require(timedelta(0) <= age < REVIEW_TTL, 'stage22_r2_review_expired')
            require(review['reviewed_files_sha256'] == implementation_hashes(), 'stage22_r2_implementation_changed')
            require(review['runtime_implementation_hash'] == services.implementation_hash(), 'stage22_r2_runtime_changed')
            db.execute(f'CREATE TABLE {TABLE} (id TEXT PRIMARY KEY, review_sha256 TEXT NOT NULL UNIQUE, receipt_json TEXT NOT NULL, authorized_at TEXT NOT NULL)')
            db.execute(f'CREATE TABLE {ATTEMPT_TABLE} (reservation_id TEXT PRIMARY KEY REFERENCES reservation(reservation_id), allowance_id TEXT NOT NULL REFERENCES {TABLE}(id), public_input_sha256 TEXT NOT NULL)')
            for statement in PROTECTIONS.values():
                db.execute(statement)
            db.execute(f'INSERT INTO {TABLE} VALUES (?,?,?,?)', (
                receipt['id'], receipt['review_sha256'], json.dumps(receipt, ensure_ascii=False, sort_keys=True),
                self.clock().astimezone(UTC).isoformat(),
            ))
            return {'id': receipt['id'], 'reused': False, **self._summary(db)}

    def reserve(self, input_tokens, output_tokens, input_price, output_price, *,
                bounded_request=None, continuation_id=None):
        require(isinstance(bounded_request, BoundedRequest)
                and type(input_tokens) is int and input_tokens == BOUNDED_INPUT_TOKENS
                and type(output_tokens) is int and 1 <= output_tokens <= MAX_OUTPUT_TOKENS,
                'reservation_token_bound_invalid')
        bounded_request.validate()
        ip = _decimal(input_price, minimum=INPUT_PRICE_FLOOR)
        op = _decimal(output_price, minimum=OUTPUT_PRICE_FLOOR)
        held = _cost(input_tokens, output_tokens, ip, op)
        with self._transaction() as db:
            status = self._summary(db)
            require(not status['frozen'], 'campaign_frozen')
            require(status['price_verification_status'] == 'ready', status['price_verification_status'])
            require(status['inflight_count'] == 0, 'request_inflight')
            bounded_proof_gate(self, db, bounded_request, continuation_id)
            receipt = guard(db, bounded_request)
            require(status['reservation_count'] < receipt['absolute_limit'], 'campaign_request_limit_reached')
            require(held <= Decimal(status['remaining_cny']), 'budget_exhausted')
            ident = uuid4().hex
            db.execute('INSERT INTO reservation (reservation_id,campaign_id,input_tokens,output_tokens,input_price,output_price,reserved_cny,state) VALUES (?,?,?,?,?,?,?,\'reserved\')',
                       (ident, CAMPAIGN_ID, input_tokens, output_tokens, str(ip), str(op), _money(held)))
            db.execute(f'INSERT INTO {ATTEMPT_TABLE} VALUES (?,?,?)',
                       (ident, receipt['id'], bounded_request.public_input_sha256))
            return ident
