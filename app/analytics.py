"""§12: аналитика панели. Всё считается из журнала событий и логов, а не из
текущего состояния таблиц (§7) — состояние меняется, история должна остаться.
"""
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import config
from .models import (
    DomainEvent, Message, Project, Proposal, Risk, Team, TokenUsage, User, utcnow,
)


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


async def _counts(session: AsyncSession, col, q):
    return dict((await session.execute(q.group_by(col))).all())


async def team_overview(session: AsyncSession) -> list[dict]:
    teams = (await session.execute(select(Team).order_by(Team.name))).scalars().all()
    out = []
    for t in teams:
        project = (await session.execute(
            select(Project).where(Project.team_id == t.id)
        )).scalars().first()
        pid = project.id if project else -1
        msgs = (await session.execute(
            select(func.count()).select_from(Message).where(
                Message.team_id == t.id, Message.author == "user")
        )).scalar_one()
        proactive = (await session.execute(
            select(func.count()).select_from(Message).where(
                Message.team_id == t.id, Message.initiator == "agent_proactive")
        )).scalar_one()
        ev = await _counts(
            session, DomainEvent.event_type,
            select(DomainEvent.event_type, func.count()).where(DomainEvent.project_id == pid),
        )
        pr = await _counts(
            session, Proposal.status,
            select(Proposal.status, func.count()).where(Proposal.project_id == pid),
        )
        risks = await _counts(
            session, Risk.category,
            select(Risk.category, func.count()).where(Risk.project_id == pid),
        )
        members = (await session.execute(
            select(func.count()).select_from(User).where(User.team_id == t.id)
        )).scalar_one()
        per_user = dict((await session.execute(
            select(Message.author_user_id, func.count()).where(
                Message.team_id == t.id, Message.author == "user")
            .group_by(Message.author_user_id)
        )).all())
        vals = [v for k, v in per_user.items() if k]
        total_pr = sum(pr.values())
        out.append({
            "team": t, "project": project, "members": members,
            "messages": msgs, "proactive": proactive,
            "stage": project.current_stage if project else "—",
            "backlog_changes": sum(v for k, v in ev.items() if k.startswith("backlog_item")
                                   or k in ("priority_changed", "item_decomposed",
                                            "items_merged")),
            "user_stories": ev.get("backlog_item_created", 0),
            "acceptance_criteria": ev.get("acceptance_criteria_created", 0),
            "priority_proposals": 0, "risks_total": sum(risks.values()),
            "risk_categories": risks,
            "proposals": pr, "proposals_total": total_pr,
            "accept_rate": _pct(pr.get("accepted", 0) + pr.get("modified", 0), total_pr),
            # §12: доля сообщений самого активного участника — работает ли команда
            "gini_top_share": _pct(max(vals) if vals else 0, sum(vals) if vals else 0),
            "silent_members": members - len([v for v in vals if v]),
        })
    return out


async def proposal_stats(session: AsyncSession, team_id: int | None = None) -> dict:
    q = select(Proposal.type, Proposal.status, func.count())
    if team_id:
        sub = select(Project.id).where(Project.team_id == team_id).scalar_subquery()
        q = q.where(Proposal.project_id.in_(sub))
    rows = (await session.execute(q.group_by(Proposal.type, Proposal.status))).all()
    by_type: dict[str, dict] = {}
    for ptype, status, n in rows:
        by_type.setdefault(ptype, {})[status] = n
    for ptype, d in by_type.items():
        total = sum(d.values())
        d["total"] = total
        d["accept_rate"] = _pct(d.get("accepted", 0) + d.get("modified", 0), total)
        d["reject_rate"] = _pct(d.get("rejected", 0), total)

    resolved = (await session.execute(
        select(Proposal.created_at, Proposal.resolved_at).where(
            Proposal.resolved_at.is_not(None), Proposal.status != "expired")
    )).all()
    deltas = sorted((r - c).total_seconds() / 60 for c, r in resolved if c and r)
    median = round(deltas[len(deltas) // 2], 1) if deltas else None
    return {"by_type": by_type, "median_minutes_to_decision": median,
            "resolved_count": len(deltas)}


async def request_type_distribution(session: AsyncSession) -> dict:
    by_type = dict((await session.execute(
        select(Message.request_type, func.count()).where(Message.author == "user")
        .group_by(Message.request_type)
    )).all())
    by_stage = dict((await session.execute(
        select(Message.project_stage, func.count()).where(Message.author == "user")
        .group_by(Message.project_stage)
    )).all())
    by_initiator = dict((await session.execute(
        select(Message.initiator, func.count()).group_by(Message.initiator)
    )).all())
    return {"by_type": by_type, "by_stage": by_stage, "by_initiator": by_initiator}


async def activity_timeline(session: AsyncSession, days: int = 30) -> dict:
    """§12: две линии на одних осях — активность команды и активность агента."""
    since = utcnow() - timedelta(days=days)
    rows = (await session.execute(
        select(func.date(Message.created_at), Message.author, func.count())
        .where(Message.created_at >= since)
        .group_by(func.date(Message.created_at), Message.author)
    )).all()
    days_set = sorted({str(d) for d, _, _ in rows})
    team_line = {d: 0 for d in days_set}
    agent_line = {d: 0 for d in days_set}
    for d, author, n in rows:
        (team_line if author == "user" else agent_line)[str(d)] += n
    return {"labels": days_set,
            "team": [team_line[d] for d in days_set],
            "agent": [agent_line[d] for d in days_set]}


async def token_costs(session: AsyncSession) -> dict:
    rows = (await session.execute(
        select(func.date(TokenUsage.created_at), TokenUsage.kind,
               func.sum(TokenUsage.tokens_in), func.sum(TokenUsage.tokens_out))
        .group_by(func.date(TokenUsage.created_at), TokenUsage.kind)
        .order_by(func.date(TokenUsage.created_at))
    )).all()
    daily = []
    for d, kind, tin, tout in rows:
        tin, tout = int(tin or 0), int(tout or 0)
        daily.append({
            "date": str(d), "kind": kind, "tokens_in": tin, "tokens_out": tout,
            "usd": round(tin / 1e6 * config.PRICE_IN_PER_M
                         + tout / 1e6 * config.PRICE_OUT_PER_M, 4),
        })
    total_usd = round(sum(r["usd"] for r in daily), 2)
    # §14: фоновый расход без участия людей — отдельной строкой
    background = round(sum(r["usd"] for r in daily
                           if r["kind"] in ("risk_scan", "standup")), 2)
    return {"daily": daily, "total_usd": total_usd, "background_usd": background}


async def classification_agreement(session: AsyncSession) -> dict:
    """§11: процент совпадения автоматической разметки с ручной."""
    rows = (await session.execute(
        select(Message.request_type, Message.request_type_manual).where(
            Message.request_type_manual.is_not(None))
    )).all()
    matched = sum(1 for a, m in rows if a == m)
    confusion: dict[str, dict[str, int]] = {}
    for auto, manual in rows:
        confusion.setdefault(manual or "?", {}).setdefault(auto or "?", 0)
        confusion[manual or "?"][auto or "?"] += 1
    return {"validated": len(rows), "matched": matched,
            "accuracy": _pct(matched, len(rows)), "confusion": confusion}
