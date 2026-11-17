"""Restore bounded, release-pinned context only from verified server audits."""

import copy
import re

from arknights_agent import models, services
from arknights_agent.errors import Conflict, InvalidData, NotFound
from arknights_agent.runtime.security import contains_secret, redact

MAX_DIALOGUE_TURNS = 128


def load_previous(session, run_id, release_id=None, *, allow_general=False):
    if run_id is None:
        return None
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
        raise InvalidData("invalid previous run selector")
    row = session.get(models.QaRun, run_id)
    if row is None:
        raise NotFound("previous server answer was not found")
    result = row.trace.get("result")
    if not isinstance(result, dict) or services.content_digest(result) != row.trace.get(
        "response_hash"
    ):
        raise Conflict("previous server answer failed integrity verification")
    if (
        result.get("run_id") != row.id
        or result.get("release_id") != row.release_id
        or result.get("question") != row.question
    ):
        raise Conflict("previous server answer identity differs")
    if release_id is not None and release_id != row.release_id:
        raise Conflict("dialogue cannot cross knowledge releases")
    supported = {"roguelike", "operator", "recruitment", "garrison", "auto"}
    if result.get("module") not in supported:
        raise InvalidData("previous answer has an unsupported module")
    if not allow_general and result.get("module") != "roguelike":
        raise InvalidData("previous answer is not a roguelike dialogue")
    release = services.resolve_release(session, row.release_id)
    if result.get("manifest_hash") != release.manifest_hash:
        raise Conflict("previous answer manifest differs")
    if contains_secret(result) or redact(result) != result:
        raise InvalidData("sensitive dialogue context")
    result = copy.deepcopy(result)
    previous_questions(result)
    return result


def previous_questions(previous):
    if previous is None:
        return []
    state = previous.get("dialogue_state")
    questions = state.get("questions") if isinstance(state, dict) else [previous["question"]]
    if (
        not isinstance(questions, list)
        or not 1 <= len(questions) < MAX_DIALOGUE_TURNS
        or any(
            not isinstance(q, str) or not q.strip() or len(q) > 2000 or contains_secret(q)
            for q in questions
        )
        or questions[-1] != previous["question"]
    ):
        raise InvalidData("dialogue history is invalid or reached the turn limit")
    return list(questions)


def binding_question(question, previous=None):
    questions = previous_questions(previous)
    return (
        services.canonical_json({"current_question": question, "previous_questions": questions})
        if questions
        else question
    )


def load_visible_history(session, previous):
    """Authenticate every real ancestor; saved prose is context, never new evidence."""
    if previous is None:
        return []
    expected = previous_questions(previous)
    current = previous
    seen = set()
    history = []
    while current is not None:
        if current['run_id'] in seen or previous_questions(current) != expected:
            raise Conflict('dialogue ancestor chain differs from saved questions')
        seen.add(current['run_id'])
        history.append({'run_id': current['run_id'], 'question': current['question'],
            'answer': current.get('answer', ''), 'status': current.get('status'),
            'query_completed': current.get('agent_runtime', {}).get('server_committed') is True,
            'historical_answer_is_not_new_source_evidence': True})
        expected = expected[:-1]
        parent_id = current.get('dialogue_state', {}).get('previous_run_id')
        if bool(parent_id) != bool(expected):
            raise Conflict('dialogue ancestor chain is incomplete')
        current = load_previous(session, parent_id, previous['release_id'], allow_general=True)
    return list(reversed(history))


def attach_state(answer, question, previous=None, previous_run_id=None):
    answer["dialogue_state"] = {
        "questions": [*previous_questions(previous), question],
        "previous_run_id": previous_run_id,
        "context_source": "verified_server_audit"
        if previous is not None
        else "current_user_question",
        "release_pinned": True,
        "maximum_turns": MAX_DIALOGUE_TURNS,
    }
    return answer
