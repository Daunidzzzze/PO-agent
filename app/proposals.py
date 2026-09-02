"""§6 п.4 + §16: применение принятого предложения.

Всё применение — в одной транзакции вместе с записью domain_events.
Частично применённое предложение недопустимо.
"""
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import config
from .events import log_event, snapshot
from .models import (
    AcceptanceCriterion, BacklogItem, Notification, ProductVision, Proposal,
    Requirement, Risk, Sprint, User, utcnow,
)


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.combine(date.fromisoformat(s[:10]), datetime.min.time())
    except ValueError:
        return None


def _subset(payload: dict, key: str, selected: list[int] | None) -> list:
    rows = payload.get(key) or []
    if selected is None:
        return rows
    return [r for i, r in enumerate(rows) if i in set(selected)]


async def _next_order(session: AsyncSession, project_id: int) -> int:
    v = (await session.execute(
        select(func.max(BacklogItem.priority_order)).where(BacklogItem.project_id == project_id)
    )).scalar()
    return (v or 0) + 1


async def apply_proposal(
    session: AsyncSession,
    proposal: Proposal,
    user: User | None,
    selected: list[int] | None = None,
) -> list[int]:
    """Применяет предложение. Возвращает id затронутых сущностей.
    Вызывающий делает commit — транзакция общая с resolve()."""
    pid = proposal.project_id
    p = proposal.payload or {}
    actor_uid = user.id if user else None
    touched: list[int] = []

    def ev(**kw):
        return log_event(session, project_id=pid, actor="agent",
                         actor_user_id=actor_uid, proposal_id=proposal.id, **kw)

    t = proposal.type

    if t in ("create_user_story", "create_task"):
        key = "stories" if t == "create_user_story" else "tasks"
        order = await _next_order(session, pid)
        for i, row in enumerate(_subset(p, key, selected)):
            item = BacklogItem(
                project_id=pid,
                type=row.get("type", "user_story" if key == "stories" else "task"),
                title=row["title"], description=row.get("description", ""),
                user_story_text=row.get("user_story_text", ""),
                priority=row.get("priority", "should"), priority_order=order + i,
                estimate=row.get("estimate", ""), parent_id=row.get("parent_id"),
                created_by="agent",
            )
            session.add(item)
            await session.flush()
            touched.append(item.id)
            await ev(event_type="backlog_item_created", entity_type="backlog_item",
                     entity_id=item.id, after=snapshot(item))
            for ac in row.get("acceptance_criteria") or []:
                c = AcceptanceCriterion(backlog_item_id=item.id, content=ac,
                                        format="checklist", created_by="agent")
                session.add(c)
                await session.flush()
                await ev(event_type="acceptance_criteria_created",
                         entity_type="acceptance_criterion", entity_id=c.id,
                         after=snapshot(c))

    elif t == "decompose_item":
        parent = await session.get(BacklogItem, p["item_id"])
        order = await _next_order(session, pid)
        for i, row in enumerate(_subset(p, "subitems", selected)):
            child = BacklogItem(
                project_id=pid, type="task", title=row["title"],
                description=row.get("description", ""), priority=parent.priority,
                priority_order=order + i, estimate=row.get("estimate", ""),
                parent_id=parent.id, created_by="agent",
            )
            session.add(child)
            await session.flush()
            touched.append(child.id)
            await ev(event_type="backlog_item_created", entity_type="backlog_item",
                     entity_id=child.id, after=snapshot(child))
        await ev(event_type="item_decomposed", entity_type="backlog_item",
                 entity_id=parent.id, after={"children": touched})

    elif t == "update_priority":
        for row in _subset(p, "changes", selected):
            item = await session.get(BacklogItem, row["item_id"])
            if not item:
                continue
            before = snapshot(item)
            item.priority = row["to"]
            if row.get("priority_order") is not None:
                item.priority_order = row["priority_order"]
            touched.append(item.id)
            await ev(event_type="priority_changed", entity_type="backlog_item",
                     entity_id=item.id, before=before, after=snapshot(item))

    elif t == "create_acceptance_criteria":
        item = await session.get(BacklogItem, p["item_id"])
        for row in _subset(p, "criteria", selected):
            c = AcceptanceCriterion(
                backlog_item_id=item.id, content=row["content"],
                format=row.get("format", "checklist"), created_by="agent",
            )
            session.add(c)
            await session.flush()
            touched.append(c.id)
            await ev(event_type="acceptance_criteria_created",
                     entity_type="acceptance_criterion", entity_id=c.id, after=snapshot(c))

    elif t == "update_requirement":
        if p["action"] == "create":
            r = Requirement(project_id=pid, type=p["type"], content=p["content"],
                            source="agent", status="confirmed")
            session.add(r)
            await session.flush()
            touched.append(r.id)
            await ev(event_type="requirement_created", entity_type="requirement",
                     entity_id=r.id, after=snapshot(r))
        else:
            old = await session.get(Requirement, p["requirement_id"])
            before = snapshot(old)
            if p["action"] == "remove":
                old.status = "removed"
            else:
                old.status = "changed"
                new = Requirement(project_id=pid, type=p["type"], content=p["content"],
                                  source="agent", status="confirmed")
                session.add(new)
                await session.flush()
                old.superseded_by_id = new.id
                touched.append(new.id)
            await ev(event_type="requirement_changed", entity_type="requirement",
                     entity_id=old.id, before=before, after=snapshot(old))

    elif t == "merge_duplicates":
        keep = await session.get(BacklogItem, p["keep_item_id"])
        for row in _subset(p, "merge", selected):
            dup = await session.get(BacklogItem, row["id"])
            if not dup or dup.id == keep.id:
                continue
            before = snapshot(dup)
            for c in (await session.execute(
                select(AcceptanceCriterion).where(AcceptanceCriterion.backlog_item_id == dup.id)
            )).scalars():
                c.backlog_item_id = keep.id
            for ch in (await session.execute(
                select(BacklogItem).where(BacklogItem.parent_id == dup.id)
            )).scalars():
                ch.parent_id = keep.id
            dup.status = "cancelled"
            dup.description = (dup.description + f"\n\nОбъединено с #{keep.id}").strip()
            touched.append(dup.id)
            await ev(event_type="items_merged", entity_type="backlog_item",
                     entity_id=dup.id, before=before,
                     after={"merged_into": keep.id})

    elif t == "create_risk":
        r = Risk(project_id=pid, title=p["title"], description=p.get("description", ""),
                 severity=p.get("severity", "medium"), category=p.get("category", "scope"),
                 status="open", detected_by="agent",
                 related_item_ids=p.get("related_item_ids") or [],
                 signature=p.get("signature"))
        session.add(r)
        await session.flush()
        touched.append(r.id)
        await ev(event_type="risk_detected", entity_type="risk", entity_id=r.id,
                 after=snapshot(r))

    elif t == "assign_item":
        for row in _subset(p, "assignments", selected):
            item = await session.get(BacklogItem, row["item_id"])
            if not item:
                continue
            before = snapshot(item)
            item.assignee_id = row["user_id"]
            touched.append(item.id)
            await ev(event_type="assignment_made", entity_type="backlog_item",
                     entity_id=item.id, before=before, after=snapshot(item))

    elif t == "update_product_vision":
        cur = (await session.execute(
            select(func.max(ProductVision.version)).where(ProductVision.project_id == pid)
        )).scalar() or 0
        v = ProductVision(project_id=pid, content=p["content"], version=cur + 1,
                          created_by="agent", confirmed_by_user_id=actor_uid)
        session.add(v)
        await session.flush()
        touched.append(v.id)
        await ev(event_type="product_vision_updated", entity_type="product_vision",
                 entity_id=v.id, after=snapshot(v))

    elif t == "sprint_plan":
        n = (await session.execute(
            select(func.max(Sprint.number)).where(Sprint.project_id == pid)
        )).scalar() or 0
        sprint = Sprint(project_id=pid, number=n + 1, goal=p["goal"],
                        starts_at=_parse_date(p.get("starts_at")) or utcnow(),
                        ends_at=_parse_date(p.get("ends_at")),
                        status="active")
        session.add(sprint)
        await session.flush()
        touched.append(sprint.id)
        for row in _subset(p, "items", selected):
            item = await session.get(BacklogItem, row["id"])
            if item:
                item.sprint_id = sprint.id
        await ev(event_type="sprint_created", entity_type="sprint", entity_id=sprint.id,
                 after=snapshot(sprint))

    else:
        raise ValueError(f"Неизвестный тип предложения: {t}")

    return touched


async def resolve(
    session: AsyncSession,
    proposal: Proposal,
    decision: str,
    user: User | None,
    comment: str = "",
    selected: list[int] | None = None,
) -> list[int]:
    """accept / modify / reject. Одна транзакция."""
    if proposal.status != "pending":
        raise ValueError("Предложение уже обработано")
    touched: list[int] = []
    if decision in ("accept", "modify"):
        touched = await apply_proposal(session, proposal, user, selected)
        proposal.status = "accepted" if decision == "accept" else "modified"
    elif decision == "reject":
        proposal.status = "rejected"
    else:
        raise ValueError("decision должен быть accept/modify/reject")

    proposal.resolved_at = utcnow()
    proposal.resolved_by_user_id = user.id if user else None
    proposal.user_comment = comment or ""
    await log_event(
        session, project_id=proposal.project_id, event_type="proposal_resolved",
        entity_type="proposal", entity_id=proposal.id,
        before={"status": "pending"},
        after={"status": proposal.status, "comment": comment,
               "type": proposal.type, "touched": touched},
        actor="user", actor_user_id=user.id if user else None, proposal_id=proposal.id,
    )
    await session.commit()
    return touched


async def expire_stale(session: AsyncSession) -> int:
    """§6 п.5. Отклонённые и изменённые не трогаем никогда."""
    cutoff = utcnow() - timedelta(days=config.PROPOSAL_EXPIRY_DAYS)
    rows = (await session.execute(
        select(Proposal).where(Proposal.status == "pending", Proposal.created_at < cutoff)
    )).scalars().all()
    for p in rows:
        p.status = "expired"
        p.resolved_at = utcnow()
        await log_event(session, project_id=p.project_id, event_type="proposal_resolved",
                        entity_type="proposal", entity_id=p.id,
                        before={"status": "pending"}, after={"status": "expired"},
                        actor="system", proposal_id=p.id)
    await session.commit()
    return len(rows)


async def notify(session: AsyncSession, team_id: int, type_: str, content: str,
                 link: str = "") -> None:
    session.add(Notification(team_id=team_id, type=type_, content=content, link=link))
