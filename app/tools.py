"""§6: инструменты агента.

Два класса. Читающие выполняются сразу. Изменяющих инструментов у агента
НЕТ вообще — есть только `propose_*`, которые пишут строку в `proposals`.
Поэтому §18.2 («агент ни при каких формулировках не меняет бэклог напрямую»)
выполняется структурно, а не уговорами в промпте.
"""
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    AcceptanceCriterion, BacklogItem, Dependency, DomainEvent, Project, Proposal,
    Requirement, Risk, Sprint, Standup, StandupReport, User,
)

PRIORITIES = ("must", "should", "could", "wont")
ITEM_STATUSES = ("new", "in_progress", "blocked", "done", "cancelled")
ITEM_TYPES = ("user_story", "task", "spike")
SEVERITIES = ("low", "medium", "high")
RISK_CATEGORIES = ("scope", "technical", "schedule", "team", "requirements")
REQ_TYPES = ("functional", "non_functional", "constraint")


class ToolError(Exception):
    """Понятная модели ошибка. Команде показывается не она, а ответ агента."""


@dataclass
class ToolContext:
    session: AsyncSession
    project: Project
    team_id: int
    user_id: int | None = None
    message_id: int | None = None
    is_proactive: bool = False


# ---------------------------------------------------------------- валидация


def _enum(value: str, allowed: tuple, field: str) -> str:
    if value not in allowed:
        raise ToolError(f"Недопустимое значение {field}={value!r}. Разрешены: {', '.join(allowed)}")
    return value


async def _item(ctx: ToolContext, item_id: Any) -> BacklogItem:
    if not isinstance(item_id, int):
        raise ToolError(f"item_id должен быть числом, получено {item_id!r}")
    item = await ctx.session.get(BacklogItem, item_id)
    if not item or item.project_id != ctx.project.id:
        raise ToolError(
            f"Элемента бэклога id={item_id} нет в этом проекте. "
            f"Сначала вызови get_backlog и возьми существующие id."
        )
    return item


async def _user(ctx: ToolContext, user_id: Any) -> User:
    if not isinstance(user_id, int):
        raise ToolError(f"user_id должен быть числом, получено {user_id!r}")
    user = await ctx.session.get(User, user_id)
    if not user or user.team_id != ctx.team_id:
        raise ToolError(f"Участника id={user_id} нет в этой команде.")
    return user


async def _would_cycle(ctx: ToolContext, child_id: int, parent_id: int) -> bool:
    """Подъём по parent_id: не окажется ли child предком нового родителя."""
    seen, cur = set(), parent_id
    while cur is not None and cur not in seen:
        if cur == child_id:
            return True
        seen.add(cur)
        row = await ctx.session.get(BacklogItem, cur)
        cur = row.parent_id if row else None
    return False


async def _dup_titles(ctx: ToolContext, titles: list[str]) -> list[str]:
    """Дубли ищем и среди уже предложенного: иначе на повторную просьбу агент
    создаёт второй такой же пакет, и команда решает одно и то же дважды."""
    existing = set(
        t.strip().lower()
        for t in (
            await ctx.session.execute(
                select(BacklogItem.title).where(
                    BacklogItem.project_id == ctx.project.id,
                    BacklogItem.status != "cancelled",
                )
            )
        ).scalars()
    )
    pending = (await ctx.session.execute(
        select(Proposal).where(
            Proposal.project_id == ctx.project.id,
            Proposal.status == "pending",
            Proposal.type.in_(("create_user_story", "create_task", "decompose_item")),
        )
    )).scalars().all()
    for p in pending:
        payload = p.payload or {}
        for key in ("stories", "tasks", "subitems"):
            for row in payload.get(key) or []:
                existing.add((row.get("title") or "").strip().lower())
    dups, seen = [], set()
    for t in titles:
        key = t.strip().lower()
        if key in existing or key in seen:
            dups.append(t)
        seen.add(key)
    return dups


# ------------------------------------------------------------ читающие


async def get_project_state(ctx: ToolContext, **_) -> dict:
    p = ctx.project
    total = (
        await ctx.session.execute(
            select(func.count()).select_from(BacklogItem).where(BacklogItem.project_id == p.id)
        )
    ).scalar_one()
    return {
        "title": p.title, "idea": p.idea_description, "goals": p.goals,
        "constraints": p.constraints, "success_criteria": p.success_criteria,
        "stage": p.current_stage, "backlog_size": total,
    }


async def get_backlog(ctx: ToolContext, status=None, priority=None, assignee_id=None,
                      type=None, limit: int = 60, **_) -> dict:
    q = select(BacklogItem).where(BacklogItem.project_id == ctx.project.id)
    if status:
        q = q.where(BacklogItem.status == _enum(status, ITEM_STATUSES, "status"))
    if priority:
        q = q.where(BacklogItem.priority == _enum(priority, PRIORITIES, "priority"))
    if type:
        q = q.where(BacklogItem.type == _enum(type, ITEM_TYPES, "type"))
    if assignee_id:
        q = q.where(BacklogItem.assignee_id == assignee_id)
    q = q.order_by(BacklogItem.priority_order, BacklogItem.id).limit(min(int(limit), 200))
    items = (await ctx.session.execute(q)).scalars().all()
    return {"items": [
        {"id": i.id, "type": i.type, "title": i.title, "priority": i.priority,
         "status": i.status, "parent_id": i.parent_id, "assignee_id": i.assignee_id,
         "estimate": i.estimate}
        for i in items
    ]}


async def get_backlog_item(ctx: ToolContext, item_id: int, **_) -> dict:
    item = await _item(ctx, item_id)
    acs = (await ctx.session.execute(
        select(AcceptanceCriterion).where(AcceptanceCriterion.backlog_item_id == item.id)
    )).scalars().all()
    children = (await ctx.session.execute(
        select(BacklogItem.id, BacklogItem.title).where(BacklogItem.parent_id == item.id)
    )).all()
    deps = (await ctx.session.execute(
        select(Dependency).where(Dependency.from_item_id == item.id)
    )).scalars().all()
    return {
        "id": item.id, "type": item.type, "title": item.title,
        "description": item.description, "user_story_text": item.user_story_text,
        "priority": item.priority, "status": item.status, "estimate": item.estimate,
        "assignee_id": item.assignee_id, "created_by": item.created_by,
        "acceptance_criteria": [
            {"id": a.id, "content": a.content, "is_met": a.is_met} for a in acs
        ],
        "children": [{"id": c[0], "title": c[1]} for c in children],
        "depends_on": [{"to_item_id": d.to_item_id, "type": d.type} for d in deps],
    }


async def get_requirements(ctx: ToolContext, type=None, **_) -> dict:
    q = select(Requirement).where(
        Requirement.project_id == ctx.project.id, Requirement.status != "removed"
    )
    if type:
        q = q.where(Requirement.type == _enum(type, REQ_TYPES, "type"))
    rows = (await ctx.session.execute(q)).scalars().all()
    return {"requirements": [
        {"id": r.id, "type": r.type, "content": r.content, "status": r.status,
         "source": r.source} for r in rows
    ]}


async def get_acceptance_criteria(ctx: ToolContext, item_id: int, **_) -> dict:
    item = await _item(ctx, item_id)
    rows = (await ctx.session.execute(
        select(AcceptanceCriterion).where(AcceptanceCriterion.backlog_item_id == item.id)
    )).scalars().all()
    return {"item_id": item.id, "criteria": [
        {"id": a.id, "content": a.content, "format": a.format, "is_met": a.is_met}
        for a in rows
    ]}


async def get_risks(ctx: ToolContext, status="open", **_) -> dict:
    q = select(Risk).where(Risk.project_id == ctx.project.id)
    if status:
        q = q.where(Risk.status == status)
    rows = (await ctx.session.execute(q)).scalars().all()
    return {"risks": [
        {"id": r.id, "title": r.title, "severity": r.severity, "category": r.category,
         "status": r.status, "team_response": r.team_response} for r in rows
    ]}


async def get_sprint_status(ctx: ToolContext, **_) -> dict:
    sprint = (await ctx.session.execute(
        select(Sprint).where(Sprint.project_id == ctx.project.id, Sprint.status == "active")
        .order_by(Sprint.number.desc())
    )).scalars().first()
    if not sprint:
        return {"active_sprint": None}
    items = (await ctx.session.execute(
        select(BacklogItem).where(BacklogItem.sprint_id == sprint.id)
    )).scalars().all()
    return {"active_sprint": {
        "id": sprint.id, "number": sprint.number, "goal": sprint.goal,
        "starts_at": str(sprint.starts_at), "ends_at": str(sprint.ends_at),
        "items": [{"id": i.id, "title": i.title, "status": i.status} for i in items],
    }}


async def get_recent_events(ctx: ToolContext, limit: int = 20, **_) -> dict:
    rows = (await ctx.session.execute(
        select(DomainEvent).where(DomainEvent.project_id == ctx.project.id)
        .order_by(DomainEvent.id.desc()).limit(min(int(limit), 50))
    )).scalars().all()
    return {"events": [
        {"type": e.event_type, "entity": e.entity_type, "entity_id": e.entity_id,
         "actor": e.actor, "at": e.created_at.isoformat()} for e in rows
    ]}


async def get_standup_history(ctx: ToolContext, limit: int = 5, **_) -> dict:
    rows = (await ctx.session.execute(
        select(Standup).where(Standup.team_id == ctx.team_id)
        .order_by(Standup.scheduled_at.desc()).limit(min(int(limit), 20))
    )).scalars().all()
    out = []
    for s in rows:
        reports = (await ctx.session.execute(
            select(StandupReport, User.full_name).join(User, User.id == StandupReport.user_id)
            .where(StandupReport.standup_id == s.id)
        )).all()
        out.append({
            "at": s.scheduled_at.isoformat(), "status": s.status,
            "summary": s.agent_summary,
            "reports": [{"user": n, "done": r.done_yesterday, "plan": r.plan_today,
                         "blockers": r.blockers} for r, n in reports],
        })
    return {"standups": out}


# ------------------------------------------------------------ предлагающие


async def _propose(ctx: ToolContext, ptype: str, payload: dict, rationale: str) -> dict:
    p = Proposal(
        project_id=ctx.project.id, type=ptype, payload=payload,
        rationale=rationale or "", status="pending",
        source_message_id=ctx.message_id, is_proactive=ctx.is_proactive,
    )
    ctx.session.add(p)
    await ctx.session.flush()
    return {"proposal_id": p.id, "status": "pending",
            "note": "Предложение отправлено команде на подтверждение. "
                    "Изменений в проекте пока нет."}


async def propose_create_user_story(ctx, stories: list, rationale: str = "", **_) -> dict:
    if not stories:
        raise ToolError("Список stories пуст.")
    titles = []
    for s in stories:
        if not s.get("title"):
            raise ToolError("У каждой истории должен быть title.")
        _enum(s.get("priority", "should"), PRIORITIES, "priority")
        titles.append(s["title"])
    dups = await _dup_titles(ctx, titles)
    if dups:
        raise ToolError(
            "Это уже есть в бэклоге или уже предложено и ждёт решения команды: "
            + "; ".join(dups) + ". Не предлагай повторно — скажи команде, что "
            "предложение уже висит на подтверждении, либо предложи что-то другое."
        )
    return await _propose(ctx, "create_user_story", {"stories": stories}, rationale)


async def propose_create_task(ctx, tasks: list, rationale: str = "", **_) -> dict:
    if not tasks:
        raise ToolError("Список tasks пуст.")
    for t in tasks:
        if not t.get("title"):
            raise ToolError("У каждой задачи должен быть title.")
        _enum(t.get("priority", "should"), PRIORITIES, "priority")
        _enum(t.get("type", "task"), ITEM_TYPES, "type")
        if t.get("parent_id") is not None:
            await _item(ctx, t["parent_id"])
    dups = await _dup_titles(ctx, [t["title"] for t in tasks])
    if dups:
        raise ToolError("Задачи с такими названиями уже есть в бэклоге или уже "
                        "предложены и ждут решения: " + "; ".join(dups))
    return await _propose(ctx, "create_task", {"tasks": tasks}, rationale)


async def propose_decompose_item(ctx, item_id: int, subitems: list, rationale: str = "", **_) -> dict:
    parent = await _item(ctx, item_id)
    if not subitems:
        raise ToolError("Список subitems пуст.")
    if await _would_cycle(ctx, parent.id, parent.parent_id or 0) and parent.parent_id:
        raise ToolError("Декомпозиция создаст цикл в дереве задач.")
    for s in subitems:
        if not s.get("title"):
            raise ToolError("У каждой подзадачи должен быть title.")
    return await _propose(
        ctx, "decompose_item",
        {"item_id": parent.id, "parent_title": parent.title, "subitems": subitems}, rationale,
    )


async def propose_update_priority(ctx, changes: list, rationale: str = "", **_) -> dict:
    if not changes:
        raise ToolError("Список changes пуст.")
    detailed = []
    for c in changes:
        item = await _item(ctx, c.get("item_id"))
        new = _enum(c.get("priority", item.priority), PRIORITIES, "priority")
        detailed.append({
            "item_id": item.id, "title": item.title,
            "from": item.priority, "to": new,
            "priority_order": c.get("priority_order"),
        })
    return await _propose(ctx, "update_priority", {"changes": detailed}, rationale)


async def propose_create_acceptance_criteria(ctx, item_id: int, criteria: list,
                                             rationale: str = "", **_) -> dict:
    item = await _item(ctx, item_id)
    if not criteria:
        raise ToolError("Список criteria пуст.")
    for c in criteria:
        if not c.get("content"):
            raise ToolError("У каждого критерия должен быть content.")
        _enum(c.get("format", "checklist"), ("gherkin", "checklist"), "format")
    return await _propose(
        ctx, "create_acceptance_criteria",
        {"item_id": item.id, "item_title": item.title, "criteria": criteria}, rationale,
    )


async def propose_update_requirement(ctx, action: str, type: str, content: str,
                                     requirement_id: int | None = None,
                                     rationale: str = "", **_) -> dict:
    _enum(action, ("create", "change", "remove"), "action")
    _enum(type, REQ_TYPES, "type")
    if not content and action != "remove":
        raise ToolError("content обязателен.")
    old = None
    if action in ("change", "remove"):
        if requirement_id is None:
            raise ToolError("Для change/remove нужен requirement_id.")
        req = await ctx.session.get(Requirement, requirement_id)
        if not req or req.project_id != ctx.project.id:
            raise ToolError(f"Требования id={requirement_id} нет в этом проекте.")
        old = req.content
    return await _propose(ctx, "update_requirement", {
        "action": action, "type": type, "content": content,
        "requirement_id": requirement_id, "old_content": old,
    }, rationale)


async def propose_merge_duplicates(ctx, keep_item_id: int, merge_item_ids: list,
                                   rationale: str = "", **_) -> dict:
    keep = await _item(ctx, keep_item_id)
    if not merge_item_ids:
        raise ToolError("merge_item_ids пуст.")
    if keep_item_id in merge_item_ids:
        raise ToolError("keep_item_id не может быть в merge_item_ids.")
    merged = [await _item(ctx, i) for i in merge_item_ids]
    return await _propose(ctx, "merge_duplicates", {
        "keep_item_id": keep.id, "keep_title": keep.title,
        "merge": [{"id": m.id, "title": m.title} for m in merged],
    }, rationale)


async def propose_create_risk(ctx, title: str, description: str = "", severity: str = "medium",
                              category: str = "scope", related_item_ids: list | None = None,
                              signature: str | None = None, rationale: str = "", **_) -> dict:
    if not title:
        raise ToolError("title обязателен.")
    _enum(severity, SEVERITIES, "severity")
    _enum(category, RISK_CATEGORIES, "category")
    for i in related_item_ids or []:
        await _item(ctx, i)
    return await _propose(ctx, "create_risk", {
        "title": title, "description": description, "severity": severity,
        "category": category, "related_item_ids": related_item_ids or [],
        "signature": signature,
    }, rationale)


async def propose_assign_item(ctx, assignments: list, rationale: str = "", **_) -> dict:
    if not assignments:
        raise ToolError("assignments пуст.")
    detailed = []
    for a in assignments:
        item = await _item(ctx, a.get("item_id"))
        user = await _user(ctx, a.get("user_id"))
        detailed.append({"item_id": item.id, "item_title": item.title,
                         "user_id": user.id, "user_name": user.full_name})
    return await _propose(ctx, "assign_item", {"assignments": detailed}, rationale)


async def propose_update_product_vision(ctx, content: str, rationale: str = "", **_) -> dict:
    if not content or len(content) < 20:
        raise ToolError("content слишком короткий для Product Vision.")
    return await _propose(ctx, "update_product_vision", {"content": content}, rationale)


async def propose_sprint_plan(ctx, goal: str, item_ids: list, starts_at: str = "",
                              ends_at: str = "", rationale: str = "", **_) -> dict:
    if not goal:
        raise ToolError("goal обязателен.")
    items = [await _item(ctx, i) for i in item_ids or []]
    if not items:
        raise ToolError("В спринт нужно включить хотя бы одну задачу.")
    return await _propose(ctx, "sprint_plan", {
        "goal": goal, "starts_at": starts_at, "ends_at": ends_at,
        "items": [{"id": i.id, "title": i.title} for i in items],
    }, rationale)


# ------------------------------------------------------------ реестр

READ_TOOLS = {
    "get_project_state": get_project_state,
    "get_backlog": get_backlog,
    "get_backlog_item": get_backlog_item,
    "get_requirements": get_requirements,
    "get_acceptance_criteria": get_acceptance_criteria,
    "get_risks": get_risks,
    "get_sprint_status": get_sprint_status,
    "get_recent_events": get_recent_events,
    "get_standup_history": get_standup_history,
}

PROPOSE_TOOLS = {
    "propose_create_user_story": propose_create_user_story,
    "propose_create_task": propose_create_task,
    "propose_decompose_item": propose_decompose_item,
    "propose_update_priority": propose_update_priority,
    "propose_create_acceptance_criteria": propose_create_acceptance_criteria,
    "propose_update_requirement": propose_update_requirement,
    "propose_merge_duplicates": propose_merge_duplicates,
    "propose_create_risk": propose_create_risk,
    "propose_assign_item": propose_assign_item,
    "propose_update_product_vision": propose_update_product_vision,
    "propose_sprint_plan": propose_sprint_plan,
}

HANDLERS = {**READ_TOOLS, **PROPOSE_TOOLS}


def _fn(name: str, desc: str, props: dict, required: list) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required},
    }}


_STORY = {"type": "object", "properties": {
    "title": {"type": "string"},
    "user_story_text": {"type": "string",
                        "description": "Как <роль>, я хочу <цель>, чтобы <ценность>"},
    "description": {"type": "string"},
    "priority": {"type": "string", "enum": list(PRIORITIES)},
    "estimate": {"type": "string"},
    "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
}, "required": ["title", "user_story_text", "priority"]}

TOOL_SCHEMAS = [
    _fn("get_project_state", "Идея, цели, ограничения, этап и размер бэклога.", {}, []),
    _fn("get_backlog", "Список элементов бэклога с фильтрами.", {
        "status": {"type": "string", "enum": list(ITEM_STATUSES)},
        "priority": {"type": "string", "enum": list(PRIORITIES)},
        "type": {"type": "string", "enum": list(ITEM_TYPES)},
        "assignee_id": {"type": "integer"},
        "limit": {"type": "integer"},
    }, []),
    _fn("get_backlog_item", "Полная карточка элемента: описание, AC, подзадачи, зависимости.",
        {"item_id": {"type": "integer"}}, ["item_id"]),
    _fn("get_requirements", "Требования проекта.",
        {"type": {"type": "string", "enum": list(REQ_TYPES)}}, []),
    _fn("get_acceptance_criteria", "Критерии приёмки элемента.",
        {"item_id": {"type": "integer"}}, ["item_id"]),
    _fn("get_risks", "Риски проекта.",
        {"status": {"type": "string", "enum": ["open", "mitigated", "accepted", "closed"]}}, []),
    _fn("get_sprint_status", "Активный спринт: цель, состав, сроки.", {}, []),
    _fn("get_recent_events", "Последние изменения проекта из журнала.",
        {"limit": {"type": "integer"}}, []),
    _fn("get_standup_history", "История стендапов с отчётами участников.",
        {"limit": {"type": "integer"}}, []),

    _fn("propose_create_user_story",
        "Предложить команде создать User Stories. Можно и нужно передавать сразу пакет "
        "историй одним вызовом — команда примет или отклонит их вместе.",
        {"stories": {"type": "array", "items": _STORY},
         "rationale": {"type": "string", "description": "Почему именно так"}},
        ["stories", "rationale"]),
    _fn("propose_create_task", "Предложить создать задачи или spike.", {
        "tasks": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "description": {"type": "string"},
            "type": {"type": "string", "enum": list(ITEM_TYPES)},
            "priority": {"type": "string", "enum": list(PRIORITIES)},
            "estimate": {"type": "string"}, "parent_id": {"type": "integer"},
        }, "required": ["title", "priority"]}},
        "rationale": {"type": "string"}}, ["tasks", "rationale"]),
    _fn("propose_decompose_item", "Предложить декомпозицию элемента на подзадачи.", {
        "item_id": {"type": "integer"},
        "subitems": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "description": {"type": "string"},
            "estimate": {"type": "string"},
        }, "required": ["title"]}},
        "rationale": {"type": "string"}}, ["item_id", "subitems", "rationale"]),
    _fn("propose_update_priority", "Предложить изменение приоритетов MoSCoW.", {
        "changes": {"type": "array", "items": {"type": "object", "properties": {
            "item_id": {"type": "integer"},
            "priority": {"type": "string", "enum": list(PRIORITIES)},
            "priority_order": {"type": "integer"},
        }, "required": ["item_id", "priority"]}},
        "rationale": {"type": "string"}}, ["changes", "rationale"]),
    _fn("propose_create_acceptance_criteria", "Предложить критерии приёмки для элемента.", {
        "item_id": {"type": "integer"},
        "criteria": {"type": "array", "items": {"type": "object", "properties": {
            "content": {"type": "string"},
            "format": {"type": "string", "enum": ["gherkin", "checklist"]},
        }, "required": ["content"]}},
        "rationale": {"type": "string"}}, ["item_id", "criteria", "rationale"]),
    _fn("propose_update_requirement", "Предложить создать/изменить/убрать требование.", {
        "action": {"type": "string", "enum": ["create", "change", "remove"]},
        "type": {"type": "string", "enum": list(REQ_TYPES)},
        "content": {"type": "string"},
        "requirement_id": {"type": "integer"},
        "rationale": {"type": "string"}}, ["action", "type", "content", "rationale"]),
    _fn("propose_merge_duplicates", "Предложить объединить дубликаты в бэклоге.", {
        "keep_item_id": {"type": "integer"},
        "merge_item_ids": {"type": "array", "items": {"type": "integer"}},
        "rationale": {"type": "string"}}, ["keep_item_id", "merge_item_ids", "rationale"]),
    _fn("propose_create_risk", "Предложить зафиксировать риск.", {
        "title": {"type": "string"}, "description": {"type": "string"},
        "severity": {"type": "string", "enum": list(SEVERITIES)},
        "category": {"type": "string", "enum": list(RISK_CATEGORIES)},
        "related_item_ids": {"type": "array", "items": {"type": "integer"}},
        "rationale": {"type": "string"}}, ["title", "severity", "category", "rationale"]),
    _fn("propose_assign_item", "Предложить назначить исполнителей. Без подтверждения "
        "команды назначение не произойдёт.", {
        "assignments": {"type": "array", "items": {"type": "object", "properties": {
            "item_id": {"type": "integer"}, "user_id": {"type": "integer"},
        }, "required": ["item_id", "user_id"]}},
        "rationale": {"type": "string"}}, ["assignments", "rationale"]),
    _fn("propose_update_product_vision", "Предложить новую версию Product Vision.", {
        "content": {"type": "string"}, "rationale": {"type": "string"}},
        ["content", "rationale"]),
    _fn("propose_sprint_plan", "Предложить план спринта.", {
        "goal": {"type": "string"},
        "item_ids": {"type": "array", "items": {"type": "integer"}},
        "starts_at": {"type": "string", "description": "YYYY-MM-DD"},
        "ends_at": {"type": "string", "description": "YYYY-MM-DD"},
        "rationale": {"type": "string"}}, ["goal", "item_ids", "rationale"]),
]
