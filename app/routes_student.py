"""§10: студенческий интерфейс. Каждый запрос ограничен своей командой (§16)."""
import asyncio
import json
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import agent, bus, config
from .auth import (
    STUDENT_COOKIE, current_user, make_student_cookie, optional_user, rate_limit,
    user_by_codes,
)
from .db import Session, get_session
from .events import log_event, snapshot
from .models import (
    AcceptanceCriterion, BacklogItem, Conversation, DomainEvent, Message,
    Notification, ProductVision, Project, Proposal, Requirement, Risk, Sprint, Standup,
    StandupReport, Team, User, utcnow,
)
from .proposals import resolve as resolve_proposal
from .templating import templates

router = APIRouter()


# --------------------------------------------------------------- изоляция

async def team_project(session: AsyncSession, user: User) -> Project:
    project = (await session.execute(
        select(Project).where(Project.team_id == user.team_id)
    )).scalars().first()
    if not project:
        project = Project(team_id=user.team_id, title="Проект команды")
        session.add(project)
        await session.commit()
    return project


async def owned_item(session: AsyncSession, user: User, item_id: int) -> BacklogItem:
    project = await team_project(session, user)
    item = await session.get(BacklogItem, item_id)
    if not item or item.project_id != project.id:
        raise HTTPException(404, "Элемент не найден")
    return item


async def owned_proposal(session: AsyncSession, user: User, pid: int) -> Proposal:
    project = await team_project(session, user)
    p = await session.get(Proposal, pid)
    if not p or p.project_id != project.id:
        raise HTTPException(404, "Предложение не найдено")
    return p


# --------------------------------------------------------------- вход

@router.get("/", response_class=HTMLResponse)
async def index(request: Request, user: User | None = Depends(optional_user),
                session: AsyncSession = Depends(get_session)):
    if not user:
        return templates.TemplateResponse(request, "login.html", {})
    if not user.consent_given_at:
        return templates.TemplateResponse(request, "consent.html", {"user": user})
    project = await team_project(session, user)
    team = await session.get(Team, user.team_id)
    return templates.TemplateResponse(
        request, "app.html", {"user": user, "team": team, "project": project})


@router.post("/login")
async def login(request: Request, team_code: str = Form(...), user_code: str = Form(...),
                session: AsyncSession = Depends(get_session)):
    rate_limit(request)
    user = await user_by_codes(session, team_code, user_code)
    if not user:
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Неверный код команды или участника."}, status_code=401)
    if not user.first_login_at:
        user.first_login_at = utcnow()
    user.last_seen_at = utcnow()
    await session.commit()
    r = RedirectResponse("/", status_code=303)
    r.set_cookie(STUDENT_COOKIE, make_student_cookie(user.id, user.session_epoch),
                 max_age=config.AUTH_COOKIE_DAYS * 86400, httponly=True,
                 samesite="lax", secure=config.BASE_URL.startswith("https"))
    return r


@router.post("/consent")
async def consent(user: User = Depends(current_user),
                  session: AsyncSession = Depends(get_session)):
    user.consent_given_at = utcnow()
    await session.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
async def logout():
    r = RedirectResponse("/", status_code=303)
    r.delete_cookie(STUDENT_COOKIE)
    return r


# --------------------------------------------------------------- чат и SSE

@router.get("/events")
async def sse(request: Request, user: User = Depends(current_user)):
    team_id = user.team_id
    q = bus.subscribe(team_id)

    async def gen():
        try:
            yield "retry: 3000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event, data = await asyncio.wait_for(q.get(), timeout=20)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"  # держим соединение живым через прокси
                    continue
                yield f"event: {event}\ndata: {data}\n\n"
        finally:
            bus.unsubscribe(team_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


async def _agent_turn(team_id: int, source_message_id: int) -> None:
    """Фоновый ход агента со своей сессией — запросная уже закрыта."""
    async def once():
        async with Session() as s:
            team = await s.get(Team, team_id)
            project = (await s.execute(
                select(Project).where(Project.team_id == team_id))).scalars().first()
            if team and project:
                await agent.run_turn(s, team, project, initiator="user",
                                     source_message_id=source_message_id)
    await bus.run_serialized(team_id, once)


@router.post("/chat/send")
async def send(text: str = Form(...), user: User = Depends(current_user),
               session: AsyncSession = Depends(get_session)):
    text = text.strip()
    if not text:
        return Response(status_code=204)
    since = utcnow() - timedelta(days=1)
    used = (await session.execute(
        select(func.count()).select_from(Message).where(
            Message.team_id == user.team_id, Message.author == "user",
            Message.created_at >= since)
    )).scalar_one()
    if used >= config.DAILY_MESSAGE_LIMIT_PER_TEAM:
        raise HTTPException(429, "Дневной лимит сообщений команды исчерпан")

    project = await team_project(session, user)
    conv = (await session.execute(
        select(Conversation).where(Conversation.team_id == user.team_id)
    )).scalars().first()
    if not conv:
        conv = Conversation(team_id=user.team_id)
        session.add(conv)
        await session.flush()
    # §16: сообщение пользователя пишется в БД ДО вызова OpenAI
    msg = Message(conversation_id=conv.id, team_id=user.team_id, author="user",
                  author_user_id=user.id, content=text,
                  project_stage=project.current_stage, initiator="user")
    session.add(msg)
    await session.commit()

    bus.publish(user.team_id, "message", {"id": msg.id})
    agent.spawn(agent.classify_and_store(Session, msg.id, user.team_id))
    agent.spawn(_agent_turn(user.team_id, msg.id))
    return Response(status_code=204)


# --------------------------------------------------------------- партиалы

async def _render(request: Request, name: str, ctx: dict) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


@router.get("/partials/messages", response_class=HTMLResponse)
async def p_messages(request: Request, user: User = Depends(current_user),
                     session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(Message).where(Message.team_id == user.team_id)
        .order_by(Message.id).limit(300))).scalars().all()
    names = {u.id: u.full_name for u in (await session.execute(
        select(User).where(User.team_id == user.team_id))).scalars()}
    return await _render(request, "partials/messages.html",
                         {"messages": rows, "names": names, "me": user,
                          "busy": bus.busy(user.team_id)})


@router.get("/partials/backlog", response_class=HTMLResponse)
async def p_backlog(request: Request, status: str = "", assignee: str = "",
                    user: User = Depends(current_user),
                    session: AsyncSession = Depends(get_session)):
    project = await team_project(session, user)
    q = select(BacklogItem).where(BacklogItem.project_id == project.id)
    if status:
        q = q.where(BacklogItem.status == status)
    if assignee.isdigit():
        q = q.where(BacklogItem.assignee_id == int(assignee))
    items = (await session.execute(
        q.order_by(BacklogItem.priority_order, BacklogItem.id))).scalars().all()
    acs = (await session.execute(select(AcceptanceCriterion))).scalars().all()
    by_item: dict[int, list] = {}
    for a in acs:
        by_item.setdefault(a.backlog_item_id, []).append(a)
    members = (await session.execute(
        select(User).where(User.team_id == user.team_id))).scalars().all()
    return await _render(request, "partials/backlog.html", {
        "groups": [(p, [i for i in items if i.priority == p])
                   for p in ("must", "should", "could", "wont")],
        "acs": by_item, "members": members,
        "status": status, "assignee": assignee, "project": project})


@router.get("/partials/item/{item_id}", response_class=HTMLResponse)
async def p_item(request: Request, item_id: int, user: User = Depends(current_user),
                 session: AsyncSession = Depends(get_session)):
    item = await owned_item(session, user, item_id)
    acs = (await session.execute(
        select(AcceptanceCriterion).where(AcceptanceCriterion.backlog_item_id == item.id)
    )).scalars().all()
    children = (await session.execute(
        select(BacklogItem).where(BacklogItem.parent_id == item.id))).scalars().all()
    history = (await session.execute(
        select(DomainEvent).where(DomainEvent.entity_type == "backlog_item",
                                  DomainEvent.entity_id == item.id)
        .order_by(DomainEvent.id.desc()))).scalars().all()
    members = (await session.execute(
        select(User).where(User.team_id == user.team_id))).scalars().all()
    return await _render(request, "partials/item.html", {
        "item": item, "acs": acs, "children": children, "history": history,
        "members": members})


@router.get("/partials/requirements", response_class=HTMLResponse)
async def p_requirements(request: Request, user: User = Depends(current_user),
                         session: AsyncSession = Depends(get_session)):
    project = await team_project(session, user)
    rows = (await session.execute(
        select(Requirement).where(Requirement.project_id == project.id)
        .order_by(Requirement.id))).scalars().all()
    vision = (await session.execute(
        select(ProductVision).where(ProductVision.project_id == project.id)
        .order_by(ProductVision.version.desc()))).scalars().all()
    return await _render(request, "partials/requirements.html",
                         {"reqs": rows, "visions": vision, "project": project})


@router.get("/partials/sprint", response_class=HTMLResponse)
async def p_sprint(request: Request, user: User = Depends(current_user),
                   session: AsyncSession = Depends(get_session)):
    project = await team_project(session, user)
    sprint = (await session.execute(
        select(Sprint).where(Sprint.project_id == project.id, Sprint.status == "active")
        .order_by(Sprint.number.desc()))).scalars().first()
    items = []
    if sprint:
        items = (await session.execute(
            select(BacklogItem).where(BacklogItem.sprint_id == sprint.id))).scalars().all()
    days_left = None
    if sprint and sprint.ends_at:
        days_left = (sprint.ends_at - utcnow()).days
    return await _render(request, "partials/sprint.html",
                         {"sprint": sprint, "items": items, "days_left": days_left})


@router.get("/partials/risks", response_class=HTMLResponse)
async def p_risks(request: Request, user: User = Depends(current_user),
                  session: AsyncSession = Depends(get_session)):
    project = await team_project(session, user)
    rows = (await session.execute(
        select(Risk).where(Risk.project_id == project.id)
        .order_by(Risk.status, Risk.id.desc()))).scalars().all()
    return await _render(request, "partials/risks.html", {"risks": rows})


@router.get("/partials/proposals", response_class=HTMLResponse)
async def p_proposals(request: Request, user: User = Depends(current_user),
                      session: AsyncSession = Depends(get_session)):
    project = await team_project(session, user)
    rows = (await session.execute(
        select(Proposal).where(Proposal.project_id == project.id)
        .order_by(Proposal.status != "pending", Proposal.id.desc()).limit(60)
    )).scalars().all()
    names = {u.id: u.full_name for u in (await session.execute(
        select(User).where(User.team_id == user.team_id))).scalars()}
    return await _render(request, "partials/proposals.html",
                         {"proposals": rows, "names": names})


@router.get("/partials/feed", response_class=HTMLResponse)
async def p_feed(request: Request, user: User = Depends(current_user),
                 session: AsyncSession = Depends(get_session)):
    project = await team_project(session, user)
    rows = (await session.execute(
        select(DomainEvent).where(DomainEvent.project_id == project.id)
        .order_by(DomainEvent.id.desc()).limit(200))).scalars().all()
    names = {u.id: u.full_name for u in (await session.execute(
        select(User).where(User.team_id == user.team_id))).scalars()}
    return await _render(request, "partials/feed.html", {"events": rows, "names": names})


@router.get("/partials/notifications", response_class=HTMLResponse)
async def p_notifications(request: Request, user: User = Depends(current_user),
                          session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(Notification).where(Notification.team_id == user.team_id,
                                   Notification.type != "deferred_proactive",
                                   Notification.read_at.is_(None))
        .order_by(Notification.id.desc()).limit(20))).scalars().all()
    return await _render(request, "partials/notifications.html", {"items": rows})


@router.post("/notifications/read")
async def read_notifications(user: User = Depends(current_user),
                             session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(Notification).where(Notification.team_id == user.team_id,
                                   Notification.read_at.is_(None)))).scalars().all()
    for n in rows:
        n.read_at = utcnow()
    await session.commit()
    return Response(status_code=204)


# --------------------------------------------------------------- предложения

@router.post("/proposals/{pid}/resolve", response_class=HTMLResponse)
async def resolve(request: Request, pid: int, decision: str = Form(...),
                  comment: str = Form(""), selected: str = Form(""),
                  user: User = Depends(current_user),
                  session: AsyncSession = Depends(get_session)):
    p = await owned_proposal(session, user, pid)
    sel = None
    if decision == "modify" and selected.strip():
        sel = [int(x) for x in selected.split(",") if x.strip().isdigit()]
    try:
        await resolve_proposal(session, p, decision, user, comment, sel)
    except ValueError as e:
        raise HTTPException(400, str(e))
    bus.publish(user.team_id, "state", {"reason": "proposal"})
    return await p_proposals(request, user, session)


# --------------------------------------------------------------- ручной CRUD (§17.2)

async def _log_and_commit(session, project_id, user, **kw):
    await log_event(session, project_id=project_id, actor="user",
                    actor_user_id=user.id, **kw)
    await session.commit()


@router.post("/backlog/create")
async def create_item(title: str = Form(...), type: str = Form("user_story"),
                      priority: str = Form("should"), description: str = Form(""),
                      user_story_text: str = Form(""),
                      user: User = Depends(current_user),
                      session: AsyncSession = Depends(get_session)):
    project = await team_project(session, user)
    order = ((await session.execute(select(func.max(BacklogItem.priority_order))
                                    .where(BacklogItem.project_id == project.id))
              ).scalar() or 0) + 1
    item = BacklogItem(project_id=project.id, title=title.strip(), type=type,
                       priority=priority, description=description,
                       user_story_text=user_story_text, priority_order=order,
                       created_by="user")
    session.add(item)
    await session.flush()
    await _log_and_commit(session, project.id, user, event_type="backlog_item_created",
                          entity_type="backlog_item", entity_id=item.id,
                          after=snapshot(item))
    bus.publish(user.team_id, "state", {"reason": "backlog"})
    return Response(status_code=204)


@router.post("/backlog/{item_id}/update")
async def update_item(item_id: int, status: str = Form(None), priority: str = Form(None),
                      assignee_id: str = Form(None), title: str = Form(None),
                      description: str = Form(None), estimate: str = Form(None),
                      user: User = Depends(current_user),
                      session: AsyncSession = Depends(get_session)):
    item = await owned_item(session, user, item_id)
    before = snapshot(item)
    event = "backlog_item_updated"
    if status and status != item.status:
        item.status, event = status, "backlog_item_status_changed"
    if priority and priority != item.priority:
        item.priority, event = priority, "priority_changed"
    if assignee_id is not None:
        new = int(assignee_id) if assignee_id.isdigit() else None
        if new != item.assignee_id:
            if new is not None:
                u = await session.get(User, new)
                if not u or u.team_id != user.team_id:
                    raise HTTPException(400, "Участник не из вашей команды")
            item.assignee_id, event = new, "assignment_made"
    if title:
        item.title = title
    if description is not None:
        item.description = description
    if estimate is not None:
        item.estimate = estimate
    item.updated_at = utcnow()
    await _log_and_commit(session, item.project_id, user, event_type=event,
                          entity_type="backlog_item", entity_id=item.id,
                          before=before, after=snapshot(item))
    bus.publish(user.team_id, "state", {"reason": "backlog"})
    return Response(status_code=204)


@router.post("/backlog/reorder")
async def reorder(request: Request, user: User = Depends(current_user),
                  session: AsyncSession = Depends(get_session)):
    """drag-and-drop внутри уровня приоритета."""
    ids = (await request.json()).get("ids", [])
    project = await team_project(session, user)
    for order, raw in enumerate(ids):
        item = await session.get(BacklogItem, int(raw))
        if item and item.project_id == project.id:
            item.priority_order = order
    await session.commit()
    return Response(status_code=204)


@router.post("/ac/{ac_id}/toggle")
async def toggle_ac(ac_id: int, user: User = Depends(current_user),
                    session: AsyncSession = Depends(get_session)):
    ac = await session.get(AcceptanceCriterion, ac_id)
    if not ac:
        raise HTTPException(404, "нет такого критерия")
    item = await owned_item(session, user, ac.backlog_item_id)
    before = snapshot(ac)
    ac.is_met = not ac.is_met
    await _log_and_commit(session, item.project_id, user,
                          event_type="acceptance_criteria_met",
                          entity_type="acceptance_criterion", entity_id=ac.id,
                          before=before, after=snapshot(ac))
    bus.publish(user.team_id, "state", {"reason": "ac"})
    return Response(status_code=204)


@router.post("/ac/create")
async def create_ac(item_id: int = Form(...), content: str = Form(...),
                    format: str = Form("checklist"),
                    user: User = Depends(current_user),
                    session: AsyncSession = Depends(get_session)):
    item = await owned_item(session, user, item_id)
    ac = AcceptanceCriterion(backlog_item_id=item.id, content=content.strip(),
                             format=format, created_by="user")
    session.add(ac)
    await session.flush()
    await _log_and_commit(session, item.project_id, user,
                          event_type="acceptance_criteria_created",
                          entity_type="acceptance_criterion", entity_id=ac.id,
                          after=snapshot(ac))
    bus.publish(user.team_id, "state", {"reason": "ac"})
    return Response(status_code=204)


@router.post("/requirements/create")
async def create_req(content: str = Form(...), type: str = Form("functional"),
                     user: User = Depends(current_user),
                     session: AsyncSession = Depends(get_session)):
    project = await team_project(session, user)
    r = Requirement(project_id=project.id, type=type, content=content.strip(),
                    source="user", status="confirmed")
    session.add(r)
    await session.flush()
    await _log_and_commit(session, project.id, user, event_type="requirement_created",
                          entity_type="requirement", entity_id=r.id, after=snapshot(r))
    bus.publish(user.team_id, "state", {"reason": "requirements"})
    return Response(status_code=204)


@router.post("/risks/{risk_id}/respond")
async def respond_risk(risk_id: int, status: str = Form(...), response: str = Form(""),
                       user: User = Depends(current_user),
                       session: AsyncSession = Depends(get_session)):
    project = await team_project(session, user)
    risk = await session.get(Risk, risk_id)
    if not risk or risk.project_id != project.id:
        raise HTTPException(404, "Риск не найден")
    before = snapshot(risk)
    risk.status = status
    risk.team_response = response
    await _log_and_commit(session, project.id, user, event_type="risk_status_changed",
                          entity_type="risk", entity_id=risk.id, before=before,
                          after=snapshot(risk))
    bus.publish(user.team_id, "state", {"reason": "risks"})
    return Response(status_code=204)


@router.post("/project/update")
async def update_project(title: str = Form(""), idea_description: str = Form(""),
                         goals: str = Form(""), constraints: str = Form(""),
                         success_criteria: str = Form(""), stage: str = Form(""),
                         user: User = Depends(current_user),
                         session: AsyncSession = Depends(get_session)):
    project = await team_project(session, user)
    project.title = title or project.title
    project.idea_description = idea_description
    project.goals = goals
    project.constraints = constraints
    project.success_criteria = success_criteria
    if stage:
        project.current_stage = stage
    await session.commit()
    bus.publish(user.team_id, "state", {"reason": "project"})
    return RedirectResponse("/", status_code=303)


# --------------------------------------------------------------- стендапы

@router.get("/partials/standup", response_class=HTMLResponse)
async def p_standup(request: Request, user: User = Depends(current_user),
                    session: AsyncSession = Depends(get_session)):
    st = (await session.execute(
        select(Standup).where(Standup.team_id == user.team_id,
                              Standup.status == "collecting")
        .order_by(Standup.id.desc()))).scalars().first()
    mine = None
    if st:
        mine = (await session.execute(
            select(StandupReport).where(StandupReport.standup_id == st.id,
                                        StandupReport.user_id == user.id)
        )).scalars().first()
    history = (await session.execute(
        select(Standup).where(Standup.team_id == user.team_id,
                              Standup.status.in_(("summarized", "skipped")))
        .order_by(Standup.id.desc()).limit(10))).scalars().all()
    return await _render(request, "partials/standup.html",
                         {"standup": st, "mine": mine, "history": history})


@router.post("/standup/{sid}/report", response_class=HTMLResponse)
async def submit_report(request: Request, sid: int, done_yesterday: str = Form(""),
                        plan_today: str = Form(""), blockers: str = Form(""),
                        user: User = Depends(current_user),
                        session: AsyncSession = Depends(get_session)):
    st = await session.get(Standup, sid)
    if not st or st.team_id != user.team_id:
        raise HTTPException(404, "Стендап не найден")
    if st.status != "collecting":
        raise HTTPException(400, "Сбор отчётов закрыт")
    existing = (await session.execute(
        select(StandupReport).where(StandupReport.standup_id == sid,
                                    StandupReport.user_id == user.id)
    )).scalars().first()
    if existing:
        existing.done_yesterday, existing.plan_today = done_yesterday, plan_today
        existing.blockers, existing.submitted_at = blockers, utcnow()
    else:
        session.add(StandupReport(standup_id=sid, user_id=user.id,
                                  done_yesterday=done_yesterday, plan_today=plan_today,
                                  blockers=blockers))
    await session.commit()
    bus.publish(user.team_id, "state", {"reason": "standup"})
    return await p_standup(request, user, session)
