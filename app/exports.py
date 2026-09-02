"""§12: экспорт. Строка = одна сущность, все поля, без предобработки в R/SPSS."""
import csv
import io
import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .analytics import team_overview
from .models import (
    BacklogItem, DomainEvent, Message, Project, Proposal, Risk, Standup,
    StandupReport, Team, User,
)

TABLES = ["messages", "proposals", "domain_events", "backlog_items",
          "standups", "risks", "teams_summary", "users_summary"]


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, datetime):
        return v.isoformat(sep=" ", timespec="seconds")
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _dump(obj, drop: tuple = ()) -> dict:
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns
            if c.name not in drop}


async def _lookup(session: AsyncSession):
    users = (await session.execute(select(User))).scalars().all()
    teams = (await session.execute(select(Team))).scalars().all()
    projects = (await session.execute(select(Project))).scalars().all()
    return (
        {u.id: u for u in users},
        {t.id: t for t in teams},
        {p.id: p for p in projects},
    )


async def build_rows(session: AsyncSession, table: str, anonymize: bool = True) -> list[dict]:
    users, teams, projects = await _lookup(session)

    def team_code(team_id):
        t = teams.get(team_id)
        return t.code if t else ""

    def proj_team(project_id):
        p = projects.get(project_id)
        return p.team_id if p else None

    def uname(uid):
        u = users.get(uid)
        if not u:
            return ""
        return u.user_code if anonymize else u.full_name

    if table == "messages":
        rows = (await session.execute(select(Message).order_by(Message.id))).scalars().all()
        return [{**_dump(m), "team_code": team_code(m.team_id),
                 "author_name": uname(m.author_user_id),
                 "n_tool_calls": len(m.tool_calls or [])} for m in rows]

    if table == "proposals":
        rows = (await session.execute(select(Proposal).order_by(Proposal.id))).scalars().all()
        out = []
        for p in rows:
            tid = proj_team(p.project_id)
            secs = ((p.resolved_at - p.created_at).total_seconds()
                    if p.resolved_at and p.created_at else None)
            out.append({**_dump(p), "team_code": team_code(tid),
                        "resolved_by_name": uname(p.resolved_by_user_id),
                        "seconds_to_decision": round(secs) if secs is not None else "",
                        "payload_size": len(json.dumps(p.payload or {}))})
        return out

    if table == "domain_events":
        rows = (await session.execute(
            select(DomainEvent).order_by(DomainEvent.id))).scalars().all()
        return [{**_dump(e), "team_code": team_code(proj_team(e.project_id)),
                 "actor_name": uname(e.actor_user_id)} for e in rows]

    if table == "backlog_items":
        rows = (await session.execute(
            select(BacklogItem).order_by(BacklogItem.id))).scalars().all()
        return [{**_dump(i), "team_code": team_code(proj_team(i.project_id)),
                 "assignee_name": uname(i.assignee_id)} for i in rows]

    if table == "standups":
        rows = (await session.execute(select(Standup).order_by(Standup.id))).scalars().all()
        reports = (await session.execute(select(StandupReport))).scalars().all()
        by_standup: dict[int, int] = {}
        for r in reports:
            by_standup[r.standup_id] = by_standup.get(r.standup_id, 0) + 1
        return [{**_dump(s), "team_code": team_code(s.team_id),
                 "reports_count": by_standup.get(s.id, 0)} for s in rows]

    if table == "risks":
        rows = (await session.execute(select(Risk).order_by(Risk.id))).scalars().all()
        return [{**_dump(r), "team_code": team_code(proj_team(r.project_id))} for r in rows]

    if table == "teams_summary":
        out = []
        for row in await team_overview(session):
            t = row["team"]
            out.append({
                "team_id": t.id, "team_code": t.code,
                "team_name": "" if anonymize else t.name,
                "members": row["members"], "stage": row["stage"],
                "user_messages": row["messages"], "proactive_messages": row["proactive"],
                "backlog_changes": row["backlog_changes"],
                "user_stories_created": row["user_stories"],
                "acceptance_criteria_created": row["acceptance_criteria"],
                "risks_total": row["risks_total"],
                "proposals_total": row["proposals_total"],
                "proposals_accepted": row["proposals"].get("accepted", 0),
                "proposals_rejected": row["proposals"].get("rejected", 0),
                "proposals_modified": row["proposals"].get("modified", 0),
                "proposals_expired": row["proposals"].get("expired", 0),
                "accept_rate_pct": row["accept_rate"],
                "top_member_share_pct": row["gini_top_share"],
                "silent_members": row["silent_members"],
            })
        return out

    if table == "users_summary":
        msgs = (await session.execute(select(Message))).scalars().all()
        per_user: dict[int, list] = {}
        for m in msgs:
            if m.author == "user" and m.author_user_id:
                per_user.setdefault(m.author_user_id, []).append(m)
        resolved = (await session.execute(select(Proposal))).scalars().all()
        per_resolver: dict[int, list] = {}
        for p in resolved:
            if p.resolved_by_user_id:
                per_resolver.setdefault(p.resolved_by_user_id, []).append(p)
        out = []
        for u in users.values():
            mine = per_user.get(u.id, [])
            decided = per_resolver.get(u.id, [])
            out.append({
                "user_id": u.id, "user_code": u.user_code,
                "full_name": "" if anonymize else u.full_name,
                "team_code": team_code(u.team_id), "role_in_team": u.role_in_team,
                "is_active": u.is_active,
                "first_login_at": u.first_login_at, "last_seen_at": u.last_seen_at,
                "consent_given_at": u.consent_given_at,
                "messages": len(mine),
                "proposals_resolved": len(decided),
                "proposals_accepted": sum(1 for p in decided if p.status == "accepted"),
                "proposals_rejected": sum(1 for p in decided if p.status == "rejected"),
            })
        return out

    raise ValueError(f"Неизвестная таблица {table}")


def to_csv(rows: list[dict]) -> bytes:
    """UTF-8 с BOM — открывается в Excel двойным кликом."""
    if not rows:
        return "﻿".encode("utf-8")
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: _cell(v) for k, v in r.items()})
    return ("﻿" + buf.getvalue()).encode("utf-8")


async def to_xlsx(session: AsyncSession, anonymize: bool = True) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for name in TABLES:
        rows = await build_rows(session, name, anonymize)
        ws = wb.create_sheet(name[:31])
        if not rows:
            continue
        headers = list(rows[0].keys())
        ws.append(headers)
        for r in rows:
            ws.append([_cell(r.get(h)) for h in headers])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
