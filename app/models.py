"""Доменная модель (§3). Ядро системы — не чат.

Все datetime хранятся naive-UTC: SQLite не умеет tz-aware, а сравнения
должны работать одинаково на обоих бэкендах.
"""
from datetime import datetime, timezone

from sqlalchemy import (
    JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# JSONB на Postgres, обычный JSON на SQLite.
JSONType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Team(Base):
    __tablename__ = "teams"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # §8/§12: покомандный выключатель проактивности
    proactive_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    standup_days: Mapped[str] = mapped_column(String(50), default="mon,tue,wed,thu,fri")
    standup_time: Mapped[str] = mapped_column(String(5), default="10:00")

    users: Mapped[list["User"]] = relationship(back_populates="team")


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_code: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    role_in_team: Mapped[str] = mapped_column(String(120), default="")
    first_login_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    consent_given_at: Mapped[datetime | None] = mapped_column(DateTime)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # «Сбросить сессии» в панели: инкремент делает старые куки недействительными
    session_epoch: Mapped[int] = mapped_column(Integer, default=0)

    team: Mapped[Team] = relationship(back_populates="users")


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    idea_description: Mapped[str] = mapped_column(Text, default="")
    goals: Mapped[str] = mapped_column(Text, default="")
    constraints: Mapped[str] = mapped_column(Text, default="")
    success_criteria: Mapped[str] = mapped_column(Text, default="")
    # concept / requirements / development / testing / delivery
    current_stage: Mapped[str] = mapped_column(String(30), default="concept")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ProductVision(Base):
    __tablename__ = "product_visions"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    content: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_by: Mapped[str] = mapped_column(String(10), default="agent")  # agent/user
    confirmed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class Requirement(Base):
    __tablename__ = "requirements"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    type: Mapped[str] = mapped_column(String(20))  # functional/non_functional/constraint
    content: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(15), default="user")  # user/agent/document
    status: Mapped[str] = mapped_column(String(15), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    superseded_by_id: Mapped[int | None] = mapped_column(ForeignKey("requirements.id"))


class Sprint(Base):
    __tablename__ = "sprints"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    number: Mapped[int] = mapped_column(Integer, default=1)
    goal: Mapped[str] = mapped_column(Text, default="")
    starts_at: Mapped[datetime | None] = mapped_column(DateTime)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(15), default="active")


class BacklogItem(Base):
    __tablename__ = "backlog_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    type: Mapped[str] = mapped_column(String(15), default="user_story")
    title: Mapped[str] = mapped_column(String(400))
    description: Mapped[str] = mapped_column(Text, default="")
    user_story_text: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[str] = mapped_column(String(10), default="should")  # MoSCoW
    priority_order: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(15), default="new")
    estimate: Mapped[str] = mapped_column(String(30), default="")
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("backlog_items.id"))
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_by: Mapped[str] = mapped_column(String(10), default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    sprint_id: Mapped[int | None] = mapped_column(ForeignKey("sprints.id"))

    criteria: Mapped[list["AcceptanceCriterion"]] = relationship(
        back_populates="item", cascade="all,delete-orphan"
    )


class AcceptanceCriterion(Base):
    __tablename__ = "acceptance_criteria"
    id: Mapped[int] = mapped_column(primary_key=True)
    backlog_item_id: Mapped[int] = mapped_column(ForeignKey("backlog_items.id"), index=True)
    content: Mapped[str] = mapped_column(Text)
    format: Mapped[str] = mapped_column(String(15), default="checklist")
    is_met: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(10), default="agent")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    item: Mapped[BacklogItem] = relationship(back_populates="criteria")


class Dependency(Base):
    __tablename__ = "dependencies"
    id: Mapped[int] = mapped_column(primary_key=True)
    from_item_id: Mapped[int] = mapped_column(ForeignKey("backlog_items.id"), index=True)
    to_item_id: Mapped[int] = mapped_column(ForeignKey("backlog_items.id"), index=True)
    type: Mapped[str] = mapped_column(String(15), default="blocks")
    detected_by: Mapped[str] = mapped_column(String(10), default="agent")


class Standup(Base):
    __tablename__ = "standups"
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(15), default="pending")
    agent_summary: Mapped[str] = mapped_column(Text, default="")
    agent_recommendations: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # §16: перезапуск планировщика не создаёт дубль стендапа
    __table_args__ = (UniqueConstraint("team_id", "scheduled_at", name="uq_standup_slot"),)


class StandupReport(Base):
    __tablename__ = "standup_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    standup_id: Mapped[int] = mapped_column(ForeignKey("standups.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    done_yesterday: Mapped[str] = mapped_column(Text, default="")
    plan_today: Mapped[str] = mapped_column(Text, default="")
    blockers: Mapped[str] = mapped_column(Text, default="")
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("standup_id", "user_id", name="uq_report_once"),)


class Risk(Base):
    __tablename__ = "risks"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    title: Mapped[str] = mapped_column(String(400))
    description: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String(10), default="medium")
    category: Mapped[str] = mapped_column(String(20), default="scope")
    status: Mapped[str] = mapped_column(String(15), default="open")
    detected_by: Mapped[str] = mapped_column(String(10), default="agent")
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    related_item_ids: Mapped[list | None] = mapped_column(JSONType, default=list)
    team_response: Mapped[str] = mapped_column(Text, default="")
    # ключ дедупликации фоновых проверок (§8)
    signature: Mapped[str | None] = mapped_column(String(120), index=True)


class Proposal(Base):
    __tablename__ = "proposals"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    type: Mapped[str] = mapped_column(String(50), index=True)
    payload: Mapped[dict] = mapped_column(JSONType)
    rationale: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(15), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolved_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    user_comment: Mapped[str] = mapped_column(Text, default="")
    source_message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id"))
    is_proactive: Mapped[bool] = mapped_column(Boolean, default=False)


class DomainEvent(Base):
    __tablename__ = "domain_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(50), index=True)
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    payload_before: Mapped[dict | None] = mapped_column(JSONType)
    payload_after: Mapped[dict | None] = mapped_column(JSONType)
    actor: Mapped[str] = mapped_column(String(10), default="user")
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    proposal_id: Mapped[int | None] = mapped_column(ForeignKey("proposals.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    author: Mapped[str] = mapped_column(String(10))  # user/agent/system
    author_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    model: Mapped[str | None] = mapped_column(String(60))
    prompt_version_id: Mapped[int | None] = mapped_column(ForeignKey("prompt_versions.id"))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    request_type: Mapped[str | None] = mapped_column(String(30), index=True)
    request_type_confidence: Mapped[float | None] = mapped_column()
    request_type_manual: Mapped[str | None] = mapped_column(String(30))  # §11 валидация
    project_stage: Mapped[str | None] = mapped_column(String(20))
    initiator: Mapped[str] = mapped_column(String(20), default="user")
    related_item_ids: Mapped[list | None] = mapped_column(JSONType)
    tool_calls: Mapped[list | None] = mapped_column(JSONType)
    time_since_prev_agent_ms: Mapped[int | None] = mapped_column(Integer)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    type: Mapped[str] = mapped_column(String(40))
    content: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    read_at: Mapped[datetime | None] = mapped_column(DateTime)


class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), default="po_agent")
    content: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"))
    kind: Mapped[str] = mapped_column(String(50))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class TokenUsage(Base):
    """§14: расход фоновых проверок логируется отдельной строкой."""
    __tablename__ = "token_usage"
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)  # chat/classify/risk_scan/standup
    model: Mapped[str] = mapped_column(String(60))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Setting(Base):
    """Глобальные переключатели панели (проактивность и т.п.)."""
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(String(200))
