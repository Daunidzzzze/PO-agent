"""§7: журнал доменных событий. Единственный источник аналитики §12."""
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .models import DomainEvent

EVENT_TYPES = {
    "backlog_item_created", "backlog_item_updated", "backlog_item_status_changed",
    "priority_changed", "item_decomposed", "items_merged",
    "acceptance_criteria_created", "acceptance_criteria_met",
    "requirement_created", "requirement_changed", "product_vision_updated",
    "risk_detected", "risk_status_changed", "dependency_detected",
    "assignment_made", "sprint_created", "standup_completed",
    "proposal_created", "proposal_resolved",
}


def snapshot(obj: Any) -> dict:
    """Плоский снимок ORM-объекта для payload_before/after."""
    if obj is None:
        return {}
    out = {}
    for c in obj.__table__.columns:
        v = getattr(obj, c.name)
        out[c.name] = v.isoformat() if hasattr(v, "isoformat") else v
    return out


async def log_event(
    session: AsyncSession,
    *,
    project_id: int,
    event_type: str,
    entity_type: str,
    entity_id: int | None = None,
    before: dict | None = None,
    after: dict | None = None,
    actor: str = "user",
    actor_user_id: int | None = None,
    proposal_id: int | None = None,
) -> DomainEvent:
    assert event_type in EVENT_TYPES, f"unknown event_type {event_type}"
    ev = DomainEvent(
        project_id=project_id, event_type=event_type, entity_type=entity_type,
        entity_id=entity_id, payload_before=before, payload_after=after,
        actor=actor, actor_user_id=actor_user_id, proposal_id=proposal_id,
    )
    session.add(ev)
    return ev
