"""§12: панель исследователя. Отдельное приложение, вход по логину и паролю."""
import csv
import io
import random
import secrets

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import analytics, config, exports
from .agent import DEFAULT_PROMPT, REQUEST_TYPES
from .auth import ADMIN_COOKIE, rate_limit, require_admin, sign
from .db import get_session
from .models import (
    BacklogItem, DomainEvent, Message, Project, Proposal, PromptVersion, Risk,
    Setting, Standup, Team, TokenUsage, User, utcnow,
)
from .templating import templates

router = APIRouter(prefix="/panel")
Admin = Depends(require_admin)


def page(request: Request, name: str, ctx: dict) -> HTMLResponse:
    return templates.TemplateResponse(request, f"panel/{name}", {**ctx, "active": name})


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return templates.TemplateResponse(request, "panel/login.html", {})


@router.post("/login")
async def login(request: Request, login: str = Form(...), password: str = Form(...)):
    rate_limit(request)
    ok = (secrets.compare_digest(login, config.ADMIN_LOGIN)
          and secrets.compare_digest(password, config.ADMIN_PASSWORD))
    if not ok:
        return templates.TemplateResponse(
            request, "panel/login.html", {"error": "Неверные данные"}, status_code=401)
    r = RedirectResponse("/panel/", status_code=303)
    r.set_cookie(ADMIN_COOKIE, sign(config.ADMIN_LOGIN), httponly=True, samesite="lax",
                 max_age=86400, secure=config.BASE_URL.startswith("https"))
    return r


@router.post("/logout")
async def logout():
    r = RedirectResponse("/panel/login", status_code=303)
    r.delete_cookie(ADMIN_COOKIE)
    return r


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, _: str = Admin,
                    session: AsyncSession = Depends(get_session)):
    rows = await analytics.team_overview(session)
    return page(request, "dashboard.html", {"rows": rows})


# ------------------------------------------------------------ команды


@router.get("/teams", response_class=HTMLResponse)
async def teams(request: Request, _: str = Admin,
                session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(Team).order_by(Team.name))).scalars().all()
    users = (await session.execute(select(User).order_by(User.team_id, User.full_name))
             ).scalars().all()
    by_team: dict[int, list] = {}
    for u in users:
        by_team.setdefault(u.team_id, []).append(u)
    return page(request, "teams.html", {"teams": rows, "by_team": by_team})


@router.post("/teams/import")
async def import_csv(request: Request, file: UploadFile = File(...), _: str = Admin,
                     session: AsyncSession = Depends(get_session)):
    """CSV: user_code, full_name, team_code, role_in_team (+ team_name опционально)."""
    raw = (await file.read()).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(raw))
    created_u = created_t = skipped = 0
    for row in reader:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        code, name = row.get("user_code"), row.get("full_name")
        tcode = row.get("team_code")
        if not code or not tcode:
            skipped += 1
            continue
        team = (await session.execute(select(Team).where(Team.code == tcode))
                ).scalars().first()
        if not team:
            team = Team(code=tcode, name=row.get("team_name") or tcode)
            session.add(team)
            await session.flush()
            session.add(Project(team_id=team.id, title=f"Проект {team.name}"))
            created_t += 1
        user = (await session.execute(select(User).where(User.user_code == code))
                ).scalars().first()
        if user:
            user.full_name, user.team_id = name or user.full_name, team.id
            user.role_in_team = row.get("role_in_team") or user.role_in_team
        else:
            session.add(User(user_code=code, full_name=name or code, team_id=team.id,
                             role_in_team=row.get("role_in_team") or ""))
            created_u += 1
    await session.commit()
    return RedirectResponse(
        f"/panel/teams?msg=Импортировано: команд {created_t}, участников {created_u}, "
        f"пропущено строк {skipped}", status_code=303)


@router.post("/users/{uid}/toggle")
async def toggle_user(uid: int, _: str = Admin,
                      session: AsyncSession = Depends(get_session)):
    u = await session.get(User, uid)
    if not u:
        raise HTTPException(404)
    u.is_active = not u.is_active
    await session.commit()
    return RedirectResponse("/panel/teams", status_code=303)


@router.post("/users/{uid}/reset-session")
async def reset_session(uid: int, _: str = Admin,
                        session: AsyncSession = Depends(get_session)):
    u = await session.get(User, uid)
    if not u:
        raise HTTPException(404)
    u.session_epoch += 1
    await session.commit()
    return RedirectResponse("/panel/teams", status_code=303)


# ------------------------------------------------------------ проекты


@router.get("/projects", response_class=HTMLResponse)
async def projects(request: Request, _: str = Admin,
                   session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(Project, Team).join(Team, Team.id == Project.team_id))).all()
    out = []
    for p, t in rows:
        backlog = (await session.execute(
            select(func.count()).select_from(BacklogItem)
            .where(BacklogItem.project_id == p.id))).scalar_one()
        risks = (await session.execute(
            select(func.count()).select_from(Risk)
            .where(Risk.project_id == p.id, Risk.status == "open"))).scalar_one()
        last = (await session.execute(
            select(func.max(Message.created_at)).where(Message.team_id == t.id))).scalar()
        out.append({"project": p, "team": t, "backlog": backlog, "risks": risks,
                    "last": last})
    return page(request, "projects.html", {"rows": out})


@router.post("/projects/{pid}/stage")
async def set_stage(pid: int, stage: str = Form(...), _: str = Admin,
                    session: AsyncSession = Depends(get_session)):
    p = await session.get(Project, pid)
    if not p:
        raise HTTPException(404)
    p.current_stage = stage
    await session.commit()
    return RedirectResponse("/panel/projects", status_code=303)


# ------------------------------------------------------------ диалоги


@router.get("/dialogs", response_class=HTMLResponse)
async def dialogs(request: Request, team: str = "", user: str = "", rtype: str = "",
                  stage: str = "", initiator: str = "", q: str = "",
                  date_from: str = "", date_to: str = "", _: str = Admin,
                  session: AsyncSession = Depends(get_session)):
    stmt = select(Message).order_by(Message.id.desc()).limit(500)
    if team.isdigit():
        stmt = stmt.where(Message.team_id == int(team))
    if user.isdigit():
        stmt = stmt.where(Message.author_user_id == int(user))
    if rtype:
        stmt = stmt.where(Message.request_type == rtype)
    if stage:
        stmt = stmt.where(Message.project_stage == stage)
    if initiator:
        stmt = stmt.where(Message.initiator == initiator)
    if q:
        stmt = stmt.where(Message.content.ilike(f"%{q}%"))
    if date_from:
        stmt = stmt.where(func.date(Message.created_at) >= date_from)
    if date_to:
        stmt = stmt.where(func.date(Message.created_at) <= date_to)
    rows = list(reversed((await session.execute(stmt)).scalars().all()))
    names = {u.id: u.full_name for u in (await session.execute(select(User))).scalars()}
    teams = (await session.execute(select(Team).order_by(Team.name))).scalars().all()
    users = (await session.execute(select(User).order_by(User.full_name))).scalars().all()
    # пауза до предыдущего сообщения — для просмотра ритма диалога
    gaps = {}
    prev = None
    for m in rows:
        gaps[m.id] = int((m.created_at - prev).total_seconds()) if prev else 0
        prev = m.created_at
    return page(request, "dialogs.html", {
        "messages": rows, "names": names, "teams": teams, "users": users,
        "gaps": gaps, "types": REQUEST_TYPES,
        "f": {"team": team, "user": user, "rtype": rtype, "stage": stage,
              "initiator": initiator, "q": q, "date_from": date_from, "date_to": date_to}})


# ------------------------------------------------------------ предложения


@router.get("/proposals", response_class=HTMLResponse)
async def panel_proposals(request: Request, ptype: str = "", status: str = "",
                          team: str = "", _: str = Admin,
                          session: AsyncSession = Depends(get_session)):
    stmt = select(Proposal).order_by(Proposal.id.desc()).limit(400)
    if ptype:
        stmt = stmt.where(Proposal.type == ptype)
    if status:
        stmt = stmt.where(Proposal.status == status)
    if team.isdigit():
        sub = select(Project.id).where(Project.team_id == int(team)).scalar_subquery()
        stmt = stmt.where(Proposal.project_id.in_(sub))
    rows = (await session.execute(stmt)).scalars().all()
    stats = await analytics.proposal_stats(
        session, int(team) if team.isdigit() else None)
    teams = (await session.execute(select(Team).order_by(Team.name))).scalars().all()
    names = {u.id: u.full_name for u in (await session.execute(select(User))).scalars()}
    return page(request, "proposals.html", {
        "proposals": rows, "stats": stats, "teams": teams, "names": names,
        "f": {"ptype": ptype, "status": status, "team": team}})


# ------------------------------------------------------------ промпты


@router.get("/prompts", response_class=HTMLResponse)
async def prompts(request: Request, _: str = Admin,
                  session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(PromptVersion).order_by(PromptVersion.version.desc()))).scalars().all()
    counts = dict((await session.execute(
        select(Message.prompt_version_id, func.count()).group_by(Message.prompt_version_id)
    )).all())
    if not rows:
        rows = [PromptVersion(id=0, content=DEFAULT_PROMPT, version=0, is_active=False)]
    return page(request, "prompts.html", {"versions": rows, "counts": counts})


@router.post("/prompts/save")
async def save_prompt(content: str = Form(...), activate: str = Form(""),
                      _: str = Admin, session: AsyncSession = Depends(get_session)):
    top = (await session.execute(select(func.max(PromptVersion.version)))).scalar() or 0
    pv = PromptVersion(name="po_agent", content=content, version=top + 1,
                       is_active=bool(activate))
    if activate:
        for old in (await session.execute(
                select(PromptVersion).where(PromptVersion.is_active.is_(True)))).scalars():
            old.is_active = False
    session.add(pv)
    await session.commit()
    return RedirectResponse("/panel/prompts", status_code=303)


@router.post("/prompts/{pid}/activate")
async def activate_prompt(pid: int, _: str = Admin,
                          session: AsyncSession = Depends(get_session)):
    for old in (await session.execute(
            select(PromptVersion).where(PromptVersion.is_active.is_(True)))).scalars():
        old.is_active = False
    pv = await session.get(PromptVersion, pid)
    if pv:
        pv.is_active = True
    await session.commit()
    return RedirectResponse("/panel/prompts", status_code=303)


# ------------------------------------------------------------ проактивность


@router.get("/proactive", response_class=HTMLResponse)
async def proactive(request: Request, _: str = Admin,
                    session: AsyncSession = Depends(get_session)):
    teams = (await session.execute(select(Team).order_by(Team.name))).scalars().all()
    g = await session.get(Setting, "proactive_enabled")
    return page(request, "proactive.html", {
        "teams": teams,
        "global_enabled": config.PROACTIVE_ENABLED if g is None else g.value == "true",
        "cfg": config})


@router.post("/proactive/global")
async def set_global(enabled: str = Form(""), _: str = Admin,
                     session: AsyncSession = Depends(get_session)):
    row = await session.get(Setting, "proactive_enabled")
    value = "true" if enabled else "false"
    if row:
        row.value = value
    else:
        session.add(Setting(key="proactive_enabled", value=value))
    await session.commit()
    return RedirectResponse("/panel/proactive", status_code=303)


@router.post("/proactive/team/{tid}")
async def set_team_proactive(tid: int, enabled: str = Form(""),
                             standup_days: str = Form(""), standup_time: str = Form(""),
                             _: str = Admin, session: AsyncSession = Depends(get_session)):
    t = await session.get(Team, tid)
    if not t:
        raise HTTPException(404)
    t.proactive_enabled = bool(enabled)
    if standup_days:
        t.standup_days = standup_days
    if standup_time:
        t.standup_time = standup_time
    await session.commit()
    return RedirectResponse("/panel/proactive", status_code=303)


# ------------------------------------------------------------ §11 валидация


@router.get("/validation", response_class=HTMLResponse)
async def validation(request: Request, n: int = 30, _: str = Admin,
                     session: AsyncSession = Depends(get_session)):
    pool = (await session.execute(
        select(Message).where(Message.author == "user",
                              Message.request_type.is_not(None),
                              Message.request_type_manual.is_(None))
    )).scalars().all()
    sample = random.sample(pool, min(n, len(pool)))
    sample.sort(key=lambda m: m.id)
    return page(request, "validation.html", {
        "sample": sample, "types": REQUEST_TYPES,
        "stats": await analytics.classification_agreement(session),
        "remaining": len(pool)})


@router.post("/validation/save")
async def save_validation(request: Request, _: str = Admin,
                          session: AsyncSession = Depends(get_session)):
    form = await request.form()
    saved = 0
    for key, value in form.items():
        if not key.startswith("m_") or not value:
            continue
        msg = await session.get(Message, int(key[2:]))
        if msg and value in REQUEST_TYPES:
            msg.request_type_manual = value
            saved += 1
    await session.commit()
    return RedirectResponse("/panel/validation", status_code=303)


# ------------------------------------------------------------ аналитика


@router.get("/analytics", response_class=HTMLResponse)
async def panel_analytics(request: Request, _: str = Admin,
                          session: AsyncSession = Depends(get_session)):
    return page(request, "analytics.html", {
        "teams": await analytics.team_overview(session),
        "dist": await analytics.request_type_distribution(session),
        "timeline": await analytics.activity_timeline(session),
        "proposals": await analytics.proposal_stats(session),
        "costs": await analytics.token_costs(session),
        "agreement": await analytics.classification_agreement(session),
    })


# ------------------------------------------------------------ экспорт


@router.get("/export", response_class=HTMLResponse)
async def export_page(request: Request, _: str = Admin):
    return page(request, "export.html", {"tables": exports.TABLES})


@router.get("/export/{table}.csv")
async def export_csv(table: str, anon: int = 1, _: str = Admin,
                     session: AsyncSession = Depends(get_session)):
    if table not in exports.TABLES:
        raise HTTPException(404)
    rows = await exports.build_rows(session, table, anonymize=bool(anon))
    return Response(exports.to_csv(rows), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{table}.csv"'})


@router.get("/export/all.xlsx")
async def export_xlsx(anon: int = 1, _: str = Admin,
                      session: AsyncSession = Depends(get_session)):
    data = await exports.to_xlsx(session, anonymize=bool(anon))
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="po_agent_export.xlsx"'})
