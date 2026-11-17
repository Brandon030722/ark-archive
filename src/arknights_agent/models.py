"""Version-scoped schema; PostgreSQL migration adds immutable-publication guards."""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    return uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


JSON_VALUE = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class Entity(Base):
    __tablename__ = "entity"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    game: Mapped[str] = mapped_column(String(32), default="arknights")
    kind: Mapped[str] = mapped_column(String(40))
    native_id: Mapped[str] = mapped_column(String(240))
    __table_args__ = (UniqueConstraint("game", "kind", "native_id"),)


class EntityAlias(Base):
    __tablename__ = "entity_alias"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    entity_id: Mapped[str] = mapped_column(ForeignKey("entity.id"))
    alias: Mapped[str] = mapped_column(String(240), index=True)
    language: Mapped[str] = mapped_column(String(24), default="zh_CN")
    __table_args__ = (UniqueConstraint("entity_id", "alias", "language"),)


class Ruleset(Base):
    __tablename__ = "ruleset"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    module: Mapped[str] = mapped_column(String(32))
    region: Mapped[str] = mapped_column(String(16), default="CN")
    source_revision: Mapped[str] = mapped_column(String(160))
    scope: Mapped[dict] = mapped_column(JSON_VALUE)
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scope_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SourceSnapshot(Base):
    __tablename__ = "source_snapshot"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_key: Mapped[str] = mapped_column(String(240))
    source_type: Mapped[str] = mapped_column(String(40))
    url: Mapped[str] = mapped_column(Text)
    revision: Mapped[str] = mapped_column(String(160))
    content_hash: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(64))
    media_type: Mapped[str] = mapped_column(String(100))
    byte_size: Mapped[int] = mapped_column(Integer)
    source_metadata: Mapped[dict] = mapped_column(JSON_VALUE, default=dict)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("url", "revision", "content_hash"),
        CheckConstraint("byte_size >= 0", name="nonnegative_size"),
        CheckConstraint(
            "length(content_hash) = 64 AND storage_key = content_hash", name="hash_key"
        ),
    )


class Dataset(Base):
    __tablename__ = "dataset"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    module: Mapped[str] = mapped_column(String(32))
    ruleset_id: Mapped[str] = mapped_column(ForeignKey("ruleset.id"))
    source_revision: Mapped[str] = mapped_column(String(160))
    parser_version: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(16), default="staging")
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    coverage: Mapped[dict] = mapped_column(JSON_VALUE)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        CheckConstraint(
            "module IN ('operator','recruitment','garrison','roguelike')", name="supported_module"
        ),
        CheckConstraint("status IN ('staging','sealed')", name="status"),
    )


class DatasetSource(Base):
    __tablename__ = "dataset_source"
    dataset_id: Mapped[str] = mapped_column(ForeignKey("dataset.id"), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("source_snapshot.id"), primary_key=True)
    source_key: Mapped[str] = mapped_column(String(240))
    __table_args__ = (UniqueConstraint("dataset_id", "source_key"),)


class KnowledgeRecord(Base):
    __tablename__ = "knowledge_record"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("dataset.id"), index=True)
    entity_id: Mapped[str] = mapped_column(ForeignKey("entity.id"))
    module: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(40))
    native_id: Mapped[str] = mapped_column(String(240))
    name: Mapped[str] = mapped_column(String(240), index=True)
    parent_native_id: Mapped[str | None] = mapped_column(String(240))
    payload: Mapped[dict] = mapped_column(JSON_VALUE)
    __table_args__ = (
        UniqueConstraint("dataset_id", "kind", "native_id"),
        UniqueConstraint("id", "dataset_id"),
        Index("ix_record_scope", "dataset_id", "module", "kind"),
    )


# Public query alias for clients which use a generic record interface.
Record = KnowledgeRecord


class EvidenceSpan(Base):
    __tablename__ = "evidence_span"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("dataset.id"))
    snapshot_id: Mapped[str] = mapped_column(String(32))
    pointer: Mapped[str] = mapped_column(Text)
    excerpt: Mapped[dict | list | str | int | float | None] = mapped_column(
        JSON_VALUE, nullable=False
    )
    excerpt_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (
        ForeignKeyConstraint(
            ["dataset_id", "snapshot_id"],
            ["dataset_source.dataset_id", "dataset_source.snapshot_id"],
        ),
        UniqueConstraint("id", "dataset_id"),
    )


class RecordEvidence(Base):
    __tablename__ = "record_evidence"
    record_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(String(32))
    __table_args__ = (
        ForeignKeyConstraint(
            ["record_id", "dataset_id"], ["knowledge_record.id", "knowledge_record.dataset_id"]
        ),
        ForeignKeyConstraint(
            ["evidence_id", "dataset_id"], ["evidence_span.id", "evidence_span.dataset_id"]
        ),
    )


class FactAssertion(Base):
    __tablename__ = "fact_assertion"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("dataset.id"))
    record_id: Mapped[str] = mapped_column(String(32))
    predicate: Mapped[str] = mapped_column(String(100))
    value: Mapped[dict] = mapped_column(JSON_VALUE)
    evidence_id: Mapped[str] = mapped_column(String(32))
    review_status: Mapped[str] = mapped_column(String(40), default="source_extracted")
    adopted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(
            ["evidence_id", "dataset_id"], ["evidence_span.id", "evidence_span.dataset_id"]
        ),
        ForeignKeyConstraint(
            ["record_id", "dataset_id"], ["knowledge_record.id", "knowledge_record.dataset_id"]
        ),
    )


class EffectRule(Base):
    __tablename__ = "effect_rule"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("dataset.id"))
    rule_key: Mapped[str] = mapped_column(String(240))
    expression: Mapped[dict] = mapped_column(JSON_VALUE)
    review_status: Mapped[str] = mapped_column(String(40), default="unverified")
    __table_args__ = (UniqueConstraint("dataset_id", "rule_key"),)


class Release(Base):
    __tablename__ = "release_manifest"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    label: Mapped[str] = mapped_column(String(160))
    manifest: Mapped[dict] = mapped_column(JSON_VALUE)
    manifest_canonical: Mapped[str] = mapped_column(Text)
    manifest_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReleaseDataset(Base):
    __tablename__ = "release_dataset"
    release_id: Mapped[str] = mapped_column(ForeignKey("release_manifest.id"), primary_key=True)
    module: Mapped[str] = mapped_column(String(32), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("dataset.id"))


class ActiveRelease(Base):
    __tablename__ = "active_release"
    channel: Mapped[str] = mapped_column(String(32), primary_key=True, default="default")
    release_id: Mapped[str] = mapped_column(ForeignKey("release_manifest.id"))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GarrisonState(Base):
    __tablename__ = "garrison_state"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    state_revision: Mapped[int] = mapped_column(Integer)
    ruleset_id: Mapped[str] = mapped_column(ForeignKey("ruleset.id"))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    state: Mapped[dict] = mapped_column(JSON_VALUE)


class QaRun(Base):
    __tablename__ = "qa_run"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    release_id: Mapped[str] = mapped_column(ForeignKey("release_manifest.id"))
    question: Mapped[str] = mapped_column(Text)
    trace: Mapped[dict] = mapped_column(JSON_VALUE)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Operator(Base):
    __tablename__ = "operator"
    record_id: Mapped[str] = mapped_column(ForeignKey("knowledge_record.id"), primary_key=True)
    rarity: Mapped[int] = mapped_column(Integer)
    profession: Mapped[str] = mapped_column(String(40))
    sub_profession: Mapped[str | None] = mapped_column(String(80))
    position: Mapped[str] = mapped_column(String(40))
    phases: Mapped[list] = mapped_column(JSON_VALUE)


class SkillLevel(Base):
    __tablename__ = "skill_level"
    record_id: Mapped[str] = mapped_column(ForeignKey("knowledge_record.id"), primary_key=True)
    rank: Mapped[int] = mapped_column(Integer, primary_key=True)
    skill_type: Mapped[str] = mapped_column(String(40))
    sp_type: Mapped[str | int | None] = mapped_column(JSON_VALUE)
    sp_cost: Mapped[float | None] = mapped_column(Numeric(18, 6))
    initial_sp: Mapped[float | None] = mapped_column(Numeric(18, 6))
    duration: Mapped[float | None] = mapped_column(Numeric(18, 6))
    description: Mapped[str | None] = mapped_column(Text)
    blackboard: Mapped[list] = mapped_column(JSON_VALUE)
    __table_args__ = (CheckConstraint("rank > 0", name="positive_rank"),)


class TalentVariant(Base):
    __tablename__ = "talent_variant"
    record_id: Mapped[str] = mapped_column(ForeignKey("knowledge_record.id"), primary_key=True)
    unlock_condition: Mapped[dict] = mapped_column(JSON_VALUE)
    required_potential_rank: Mapped[int | None] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(Text)
    blackboard: Mapped[list] = mapped_column(JSON_VALUE)


class ModulePhase(Base):
    __tablename__ = "module_phase"
    record_id: Mapped[str] = mapped_column(ForeignKey("knowledge_record.id"), primary_key=True)
    equip_level: Mapped[int] = mapped_column(Integer)
    attribute_blackboard: Mapped[list] = mapped_column(JSON_VALUE)
    parts: Mapped[list] = mapped_column(JSON_VALUE)


class RecruitPoolMember(Base):
    __tablename__ = "recruit_pool_member"
    record_id: Mapped[str] = mapped_column(ForeignKey("knowledge_record.id"), primary_key=True)
    rarity: Mapped[int] = mapped_column(Integer)
    tags: Mapped[list | None] = mapped_column(JSON_VALUE, nullable=True)
    __table_args__ = (CheckConstraint("rarity BETWEEN 1 AND 6", name="rarity"),)


class GarrisonUnit(Base):
    __tablename__ = "garrison_unit"
    record_id: Mapped[str] = mapped_column(ForeignKey("knowledge_record.id"), primary_key=True)
    activity_id: Mapped[str] = mapped_column(String(80))
    chess_id: Mapped[str] = mapped_column(String(120))
    data: Mapped[dict] = mapped_column(JSON_VALUE)
