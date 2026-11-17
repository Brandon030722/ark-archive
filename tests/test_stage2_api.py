"""Stage 2 HTTP acceptance over published SQLite data, never live game facts.

All numeric values and operators in this fixture are deliberately invented.
The fixture exercises real persistence, release pinning, evidence checks,
question tools, combination calculation and audit retrieval together.
"""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_recruitment import synthetic_sources as synthetic_sources

from arknights_agent import agent, models, qa, services
from arknights_agent.api import create_app
from arknights_agent.config import Settings
from arknights_agent.ingestion.contracts import NormalizedRecord, SampleBatch, SnapshotInput
from arknights_agent.ingestion.recruitment import build_recruitment_batch


def _batch(revision, skill_cost, recruitment_sources, *, partial=False):
    document, records = {}, []

    def add(module, kind, ident, name, payload, parent=None):
        document[ident] = payload
        records.append(
            NormalizedRecord(module, kind, ident, name, payload, "fixture", f"/{ident}", parent)
        )

    add(
        "operator",
        "operator",
        "char_synthetic_chen",
        "陈",
        {
            "name": "陈",
            "rarity": "TIER_6",
            "profession": "WARRIOR",
            "position": "MELEE",
            "phases": [],
            "skills": [{"skillId": "synthetic_skill"}],
            "talents": [],
            "tagList": [],
        },
    )
    add(
        "operator",
        "skill",
        "synthetic_skill",
        "合成测试技能",
        {
            "levels": [
                {
                    "name": "合成测试技能",
                    "description": "合成倍率 {atk_scale:0%}",
                    "blackboard": [{"key": "atk_scale", "value": 1.2}],
                    "skillType": "MANUAL",
                    "duration": 3,
                    "spData": {
                        "spCost": skill_cost + rank,
                        "initSp": rank,
                        "spType": "INCREASE_WHEN_ATTACK",
                    },
                }
                for rank in range(1, 11)
            ]
        },
        "char_synthetic_chen",
    )
    add(
        "garrison",
        "garrison_rule",
        "unreviewed_fixture",
        "未核验合成规则",
        {"mapping_status": "unverified"},
    )
    base, wiki = recruitment_sources
    base = replace(
        base,
        revision=revision,
        snapshots=[replace(source, revision=revision) for source in base.snapshots],
        scopes={
            "recruitment": {
                "region": "CN",
                "mode": "public_recruitment",
                "source_revision": revision,
            }
        },
    )
    recruitment = build_recruitment_batch(base, wiki)
    coverage = {**recruitment.coverage["recruitment"]}
    if partial:
        coverage.update(status="partial", complete_pool=False, calculator_enabled=False)
    return SampleBatch(
        revision,
        [
            *recruitment.snapshots,
            SnapshotInput(
                "fixture",
                "synthetic_http_fixture",
                f"https://example.org/{revision}.json",
                revision,
                json.dumps(document, ensure_ascii=False).encode(),
                "application/json",
                {"authority": "synthetic_fixture"},
            ),
        ],
        [*recruitment.records, *records],
        {
            "operator": {"region": "CN"},
            "garrison": {"region": "CN"},
            "recruitment": recruitment.scopes["recruitment"],
        },
        {
            "operator": {"status": "synthetic_fixture", "operator_count": 1},
            "recruitment": coverage,
            "garrison": {
                "status": "synthetic_fixture",
                "mapping_status": "unverified",
                "metadata_qa_enabled": False,
                "rules_qa_enabled": False,
            },
        },
    )


@pytest.fixture
def stage2(tmp_path, synthetic_sources):
    app = create_app(
        Settings(database_url=f"sqlite:///{tmp_path / 'api.db'}", snapshot_root=tmp_path / "raw")
    )
    models.Base.metadata.create_all(app.state.engine)
    with app.state.session_factory() as session, session.begin():
        releases = []
        for revision, cost, partial in (
            ("a" * 40, 20, False),
            ("b" * 40, 40, False),
            ("c" * 40, 20, True),
        ):
            datasets = services.ingest_batch(
                session,
                _batch(revision, cost, synthetic_sources, partial=partial),
                app.state.snapshot_store,
                parser_version="synthetic-http-stage2",
            )
            releases.append(
                services.publish_release(
                    session,
                    {module: dataset.id for module, dataset in datasets.items()},
                    label=f"synthetic-stage2-{revision[0]}",
                    store=app.state.snapshot_store,
                )
            )
        services.activate_release(session, releases[0].id)
    with TestClient(app) as client:
        yield SimpleNamespace(
            app=app, client=client, a=releases[0], b=releases[1], partial=releases[2]
        )


def _ask(context, question="陈一技能7级技力消耗", **fields):
    return context.client.post("/v1/qa", json={"question": question, **fields})


def _cost(result):
    return next(claim["value"] for claim in result["claims"] if claim["label"] == "技力消耗")


def test_answered_question_has_version_field_evidence_and_audit(stage2):
    response = _ask(stage2)
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "answered"
    assert result["release_id"] == result["version"]["release_id"] == stage2.a.id
    assert result["manifest_hash"] == stage2.a.manifest_hash
    assert _cost(result) == 27
    assert "120%" in result["answer"] and stage2.a.id in result["answer"]
    citations = {citation["id"]: citation for citation in result["citations"]}
    assert all(claim["citation_ids"] for claim in result["claims"])
    for claim in result["claims"]:
        for ident in claim["citation_ids"]:
            citation = citations[ident]
            assert citation["source_revision"] == "a" * 40
            assert citation["source_url"].startswith("https://example.org/")
            assert len(citation["snapshot_hash"]) == len(citation["excerpt_hash"]) == 64
            evidence = stage2.client.get(
                f"/v1/evidence/{citation['evidence_id']}", params={"release_id": stage2.a.id}
            )
            assert evidence.status_code == 200
            assert evidence.json()["evidence"]["snapshot_id"] == citation["snapshot_id"]
    with stage2.app.state.session_factory() as session:
        run = session.get(models.QaRun, result["run_id"])
        assert run.release_id == stage2.a.id and run.question == result["question"]
    audit = stage2.client.get(f"/v1/qa/runs/{result['run_id']}", params={"release_id": stage2.a.id})
    assert audit.status_code == 200
    assert audit.json()["result"] == result


def test_questions_are_pinned_to_explicit_version_without_fallback(stage2):
    first = _ask(stage2, release_id=stage2.a.id).json()
    other = _ask(stage2, release_id=stage2.b.id).json()
    assert _cost(first) == 27 and _cost(other) == 47
    assert {c["source_revision"] for c in first["citations"]} == {"a" * 40}
    assert {c["source_revision"] for c in other["citations"]} == {"b" * 40}
    assert _ask(stage2, release_id="unknown").status_code == 404
    with stage2.app.state.session_factory() as session, session.begin():
        services.activate_release(session, stage2.b.id)
    pinned = _ask(stage2, release_id=stage2.a.id).json()
    active = _ask(stage2).json()
    assert _cost(pinned) == 27 and _cost(active) == 47


def test_audit_must_be_read_with_original_release(stage2):
    result = _ask(stage2).json()
    path = f"/v1/qa/runs/{result['run_id']}"
    assert stage2.client.get(path).status_code == 422
    for release_id in (stage2.b.id, "unknown"):
        assert stage2.client.get(path, params={"release_id": release_id}).status_code == 404
    assert (
        stage2.client.get("/v1/qa/runs/unknown", params={"release_id": stage2.a.id}).status_code
        == 404
    )


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"question": ""},
        {"question": " "},
        {"question": "x" * 2001},
        {"question": "陈", "module": "unknown"},
        {"question": "陈", "planner": "unknown"},
        {"question": "陈", "release_id": ""},
        {"question": "陈", "answer": "100000"},
    ],
)
def test_invalid_qa_requests_do_not_write_audit(stage2, body):
    assert stage2.client.post("/v1/qa", json=body).status_code == 422
    with stage2.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(models.QaRun)) == 0


def test_missing_skill_rank_is_audited_clarification_not_default_mastery(stage2):
    response = _ask(stage2, "陈一技能是什么")
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "needs_clarification"
    assert result["claims"] == result["citations"] == []
    assert result["run_id"]
    assert "等级" in result["answer"]


def test_unconfigured_openai_planner_falls_back_to_local_without_guessing(stage2):
    response = _ask(stage2, planner="openai")
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "answered" and _cost(result) == 27
    assert any(item.get("reason") == "legacy_provider_disabled" for item in result["tool_trace"])


@pytest.mark.parametrize(
    "question,alteration,expected_status",
    [
        ("陈一技能7级技力消耗", {"rank": 10}, "answered"),
        ("陈一技能7级技力消耗", {"skill_index": 2}, "answered"),
        ("陈一技能7级技力消耗", {"intent": "operator_overview"}, "answered"),
        ("陈一技能是什么", {"rank": 10}, "needs_clarification"),
    ],
)
def test_model_cannot_replace_explicit_conditions_or_invent_missing_ones(
    stage2, monkeypatch, question, alteration, expected_status
):
    with stage2.app.state.session_factory() as session:
        local = qa.plan_question(session, stage2.a, question)
    candidate = {**local, **alteration, "reason": "模型声称技力消耗999999"}
    monkeypatch.setattr(
        agent,
        "plan_with_model",
        lambda *args: (candidate, {"requested": "openai", "used": "openai"}),
    )
    response = _ask(stage2, question, planner="openai")
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == expected_status
    assert "999999" not in result["answer"]
    if expected_status == "answered":
        assert _cost(result) == 27
    else:
        assert result["claims"] == result["citations"] == []
    assert any(
        item.get("reason") == "model_conditions_conflict_or_missing"
        for item in result["tool_trace"]
    )


def test_unrecognized_model_fact_fields_never_reach_answer(stage2, monkeypatch):
    with stage2.app.state.session_factory() as session:
        local = qa.plan_question(session, stage2.a, "陈一技能7级技力消耗")
    monkeypatch.setattr(
        agent,
        "plan_with_model",
        lambda *args: ({**local, "skill_cost": 999999}, {"requested": "openai", "used": "openai"}),
    )
    response = _ask(stage2, planner="openai")
    assert response.status_code in (200, 422)
    if response.status_code == 200:
        assert _cost(response.json()) == 27
        assert "999999" not in response.json()["answer"]


def test_each_question_resolves_release_once(stage2, monkeypatch):
    original, calls = services.resolve_release, []

    def tracked(session, release_id=None):
        calls.append(release_id)
        return original(session, release_id)

    monkeypatch.setattr(services, "resolve_release", tracked)
    result = _ask(stage2).json()
    assert result["release_id"] == stage2.a.id and calls == [None]


def test_audit_response_hash_is_checked_before_replay(stage2):
    result = _ask(stage2).json()
    with stage2.app.state.session_factory() as session, session.begin():
        run = session.get(models.QaRun, result["run_id"])
        changed = {**run.trace, "result": {**run.trace["result"], "answer": "changed"}}
        run.trace = changed
    response = stage2.client.get(
        f"/v1/qa/runs/{result['run_id']}", params={"release_id": stage2.a.id}
    )
    assert response.status_code == 409


def test_recruitment_endpoint_uses_complete_pool_and_returns_evidence(stage2):
    response = stage2.client.post(
        "/v1/recruitment/calculate", json={"tags": ["医疗", "远程位", "高资"]}
    )
    assert response.status_code == 200
    result = response.json()
    assert result["release_id"] == stage2.a.id
    assert result["combination_count"] == 7
    assert result["probabilities"] is None
    combo = next(row for row in result["combinations"] if row["selected_tags"] == ["高级资深干员"])
    assert combo["minimum_rarity_if_tags_retained"] == 6
    assert [member["name"] for member in combo["candidates"]] == ["合成样本6"]
    assert result["sources"] and result["citations"] and combo["evidence_ids"]
    assert all(
        candidate["evidence_ids"]
        for row in result["combinations"]
        for candidate in row["candidates"]
    )


def test_recruitment_refuses_partial_and_unknown_releases(stage2):
    body = {"tags": ["医疗"], "release_id": stage2.partial.id}
    assert stage2.client.post("/v1/recruitment/calculate", json=body).status_code == 409
    body["release_id"] = "unknown"
    assert stage2.client.post("/v1/recruitment/calculate", json=body).status_code == 404


def test_natural_language_recruitment_does_not_silently_drop_unknown_tag(stage2):
    response = _ask(stage2, "公招标签医疗、远程位、火焰，9小时")
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "needs_clarification"
    assert result["release_id"] == result["version"]["release_id"] == stage2.a.id
    assert not result["claims"] and not result["citations"]
    assert "火焰" in result["answer"]


def test_natural_language_recruitment_refuses_unsupported_probability_question(stage2):
    response = _ask(stage2, "公招标签医疗、远程位、治疗，9小时，掉标签概率是多少")
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "unsupported"
    assert result["release_id"] == result["version"]["release_id"] == stage2.a.id
    assert not result["claims"] and not result["citations"]
    assert "概率" in result["answer"]


def test_natural_language_recruitment_parses_chinese_duration_and_echoes_tags(stage2):
    response = _ask(stage2, "公招标签医疗、远程位、治疗，九小时")
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "computed"
    assert result["release_id"] == stage2.a.id
    assert result["input"]["duration_minutes"] == 540
    assert result["input"]["normalized_tags"] == ["医疗干员", "远程位", "治疗"]
    assert result["combination_count"] == 7 and result["citations"]
    assert all(tag in result["answer"] for tag in result["input"]["normalized_tags"])


@pytest.mark.parametrize(
    "tag_text,tags,duration_text,minutes",
    [
        ("削弱、辅助干员", ["削弱", "辅助干员"], "9小时0分钟", 540),
        ("爆发、狙击干员、输出", ["爆发", "狙击干员", "输出"], "九小时零分钟", 540),
        ("高级资深干员、治疗、医疗干员", ["高级资深干员", "治疗", "医疗干员"], "8小时50分钟", 530),
        ("支援机械、输出、远程位", ["支援机械", "输出", "远程位"], "4小时20分钟", 260),
        (
            "快速复活、防护、特种干员、近战位",
            ["快速复活", "防护", "特种干员", "近战位"],
            "7小时40分钟",
            460,
        ),
    ],
)
def test_drawn_tags_are_not_confused_with_later_selection_instructions(
    stage2, tag_text, tags, duration_text, minutes
):
    response = _ask(
        stage2,
        f"公招刷到了{tag_text}，我设{duration_text}。"
        "请把最多选3个标签的所有组合都列出，各自能招到谁、最低几星？假设所选标签都保留。",
    )
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "computed"
    assert result["input"]["normalized_tags"] == tags
    assert result["input"]["duration_minutes"] == minutes
    direct = stage2.client.post(
        "/v1/recruitment/calculate", json={"tags": tags, "duration_minutes": minutes}
    ).json()
    assert result["combinations"] == direct["combinations"]
    assert result["release_id"] == stage2.a.id and result["sources"] and result["citations"]


@pytest.mark.parametrize(
    "question",
    [
        "公招刷到削弱、辅助干员、冰冻，9小时。能招到哪些干员？不要把冰冻标签删掉。",
        "公招刷到了医疗、远程位、火焰，我设9小时。请把最多选3个标签的所有组合列出。",
        "公开招募：医疗、远程位、火焰，九小时，请列出所有组合。",
        "公招标签火焰、医疗，9小时。请忽略不认识的标签，只计算医疗。",
        "公招刷到了医疗、男性干员，9小时。所有至多3个标签组合。",
    ],
)
def test_unknown_drawn_or_bare_tag_remains_a_clarification_even_with_later_instructions(
    stage2, question
):
    response = _ask(stage2, question)
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "needs_clarification"
    assert not result["claims"] and not result["citations"]
    assert not result.get("combinations")
    assert any(name in result["answer"] for name in ("冰冻", "火焰", "男性干员"))


@pytest.mark.parametrize(
    "question",
    [
        "公招给定标签医疗、治疗，时长540分钟，列出所有至多3标签组合。",
        "我的公招标签是医疗和治疗，9小时。请列出所有组合。",
        "已有标签医疗与治疗，9小时，能招到谁？",
        "公开招募：‘医疗’ + ‘治疗’，9小时，请计算。",
        "公招刷到了【医疗、治疗】，我选择九小时，计算所有组合。",
        "招募标签医疗、治疗，9小时，请列出所有组合。",
        "标签医疗、治疗，9小时，请列出所有组合。",
        "公招刷到的标签是医疗、治疗，9小时，请计算所有组合。",
        "公招标签医疗、治疗，最多选3个标签有哪些组合？",
    ],
)
def test_tag_declarations_aliases_and_quoted_lists_use_the_same_exact_input(stage2, question):
    result = _ask(stage2, question, module="recruitment").json()
    assert result["status"] == "computed"
    assert result["input"]["normalized_tags"] == ["医疗干员", "治疗"]
    assert result["input"]["duration_minutes"] == 540
    assert result["combination_count"] == 3


@pytest.mark.parametrize(
    "question",
    [
        "公招刷到了医疗、医疗干员，9小时。请列出最多3个标签组合。",
        "公招刷到了治疗、支援、输出、远程位、医疗、防护，9小时。请列出所有组合。",
    ],
)
def test_natural_language_exact_input_does_not_deduplicate_or_truncate_limits(stage2, question):
    response = _ask(stage2, question)
    assert response.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"tags": ["高资"] * 6},
        {"tags": ["女性干员"]},
        {"tags": ["医疗", "医疗干员"]},
        {"tags": ["医疗"], "duration_minutes": 59},
        {"tags": ["医疗"], "duration_minutes": 61},
        {"tags": ["医疗"], "duration_minutes": True},
        {"tags": ["医疗"], "duration_minutes": "540"},
        {"tags": [1]},
        {"tags": [], "release_id": ""},
        {"tags": [], "probability": 1},
    ],
)
def test_invalid_recruitment_input_is_not_coerced_or_calculated(stage2, body):
    assert stage2.client.post("/v1/recruitment/calculate", json=body).status_code == 422


def test_unreviewed_garrison_mapping_keeps_rule_questions_closed(stage2):
    response = _ask(stage2, "卫戍协议盟约 V2_2 装备攻击力是多少", module="garrison")
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "needs_verification"
    assert result["release_id"] == stage2.a.id
    assert not result["claims"] and not result["citations"]


@pytest.mark.parametrize("module", ["operator", "recruitment"])
def test_damaged_source_object_prevents_answers_and_calculation(stage2, module):
    release = stage2.a
    with stage2.app.state.session_factory() as session:
        # Stage 1 links all batch sources conservatively. Damage an object used
        # by this tool, rather than an unrelated source in the same batch.
        kind = "operator" if module == "operator" else "recruitment_member"
        snapshot = session.scalars(
            select(models.SourceSnapshot)
            .join(models.EvidenceSpan, models.EvidenceSpan.snapshot_id == models.SourceSnapshot.id)
            .join(
                models.RecordEvidence, models.RecordEvidence.evidence_id == models.EvidenceSpan.id
            )
            .join(
                models.KnowledgeRecord, models.KnowledgeRecord.id == models.RecordEvidence.record_id
            )
            .where(
                models.KnowledgeRecord.dataset_id
                == release.manifest["datasets"][module]["dataset_id"],
                models.KnowledgeRecord.kind == kind,
            )
        ).first()
        stage2.app.state.snapshot_store.path_for(snapshot.content_hash).write_bytes(b"damaged")
    response = (
        _ask(stage2)
        if module == "operator"
        else stage2.client.post("/v1/recruitment/calculate", json={"tags": ["医疗"]})
    )
    assert response.status_code == 409


@pytest.mark.parametrize("module", ["operator", "recruitment"])
def test_invalid_evidence_hash_prevents_fact_output(stage2, module):
    entry = stage2.a.manifest["datasets"][module]
    with stage2.app.state.session_factory() as session, session.begin():
        evidence = session.scalars(
            select(models.EvidenceSpan).where(models.EvidenceSpan.dataset_id == entry["dataset_id"])
        ).first()
        evidence.excerpt_hash = "0" * 64
    response = (
        _ask(stage2, "介绍一下陈")
        if module == "operator"
        else stage2.client.post("/v1/recruitment/calculate", json={"tags": ["医疗"]})
    )
    assert response.status_code == 409
