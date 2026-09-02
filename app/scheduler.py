"""§8: проактивность агента. Стендапы, фоновые проверки рисков, защита от шума."""
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import agent, bus, config, proposals as prop
from .db import Session
from .models import (
    AcceptanceCriterion, BacklogItem, DomainEvent, Message, Notification, Project,
    Proposal, Requirement, Risk, Setting, Sprint, Standup, StandupReport, Team, User,
    utcnow,
)
from .tools import ToolContext, propose_create_risk

log = logging.getLogger("scheduler")
TZ = ZoneInfo(config.TZNAME)
DEFERRED = "deferred_proactive"
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _local(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc).astimezone(TZ)


def _to_utc(local_dt: datetime) -> datetime:
    return local_dt.astimezone(timezone.utc).replace(tzinfo=None)


def in_quiet_hours(local_dt: datetime | None = None) -> bool:
    h = (local_dt or _local(utcnow())).hour
    s, e = config.QUIET_HOURS_START, config.QUIET_HOURS_END
    return (h >= s or h < e) if s > e else (s <= h < e)


async def _flag(session: AsyncSession, key: str, default: bool) -> bool:
    row = await session.get(Setting, key)
    return default if row is None else row.value == "true"


async def proactive_allowed(session: AsyncSession, team: Team) -> bool:
    if not await _flag(session, "proactive_enabled", config.PROACTIVE_ENABLED):
        return False
    if not team.proactive_enabled:
        return False
    since = utcnow() - timedelta(days=1)
    sent = (await session.execute(
        select(func.count()).select_from(Message).where(
            Message.team_id == team.id,
            Message.initiator == "agent_proactive",
            Message.created_at >= since,
        )
    )).scalar_one()
    return sent < config.PROACTIVE_MAX_PER_DAY


async def _teams_with_projects(session: AsyncSession) -> list[tuple[Team, Project]]:
    rows = (await session.execute(
        select(Team, Project).join(Project, Project.team_id == Team.id)
        .where(Team.is_active.is_(True))
    )).all()
    return [(t, p) for t, p in rows]


# ------------------------------------------------------------------ стендапы


async def ensure_standups() -> None:
    """Идемпотентно: перезапуск контейнера не создаёт дубль (uq_standup_slot)."""
    now_local = _local(utcnow())
    async with Session() as s:
        # Забираем поля значениями: rollback ниже протухает ORM-объекты, и
        # обращение к team.* на следующей итерации ушло бы в ленивую загрузку.
        rows = (await s.execute(
            select(Team.id, Team.standup_days, Team.standup_time)
            .where(Team.is_active.is_(True))
        )).all()

    for team_id, days_raw, time_raw in rows:
        days = [d.strip() for d in (days_raw or "").split(",") if d.strip()]
        if WEEKDAYS[now_local.weekday()] not in days:
            continue
        try:
            hh, mm = (int(x) for x in (time_raw or "").split(":"))
        except ValueError:
            continue
        slot_local = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if not (timedelta(0) <= now_local - slot_local < timedelta(minutes=30)):
            continue
        async with Session() as s:
            s.add(Standup(team_id=team_id, scheduled_at=_to_utc(slot_local),
                          status="collecting"))
            s.add(Notification(team_id=team_id, type="standup",
                               content="Стендап: заполните отчёт", link="/#standup"))
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()   # слот уже занят — перезапуск, не новый стендап
                continue
        bus.publish(team_id, "standup", {"state": "open"})
        log.info("standup opened for team %s", team_id)


async def close_standups() -> None:
    cutoff = utcnow() - timedelta(hours=config.STANDUP_COLLECTION_HOURS)
    async with Session() as s:
        rows = (await s.execute(
            select(Standup).where(Standup.status == "collecting",
                                  Standup.scheduled_at <= cutoff)
        )).scalars().all()
        for st in rows:
            reports = (await s.execute(
                select(StandupReport, User.full_name)
                .join(User, User.id == StandupReport.user_id)
                .where(StandupReport.standup_id == st.id)
            )).all()
            if not reports:
                st.status = "skipped"  # §8 п.5: это тоже данные
                await s.commit()
                bus.publish(st.team_id, "standup", {"state": "skipped"})
                continue
            st.status = "summarized"
            await s.commit()
            team = await s.get(Team, st.team_id)
            project = (await s.execute(
                select(Project).where(Project.team_id == team.id)
            )).scalars().first()
            if not project:
                continue
            body = "\n".join(
                f"- {name}: сделал — {r.done_yesterday or '—'}; "
                f"план — {r.plan_today or '—'}; блокеры — {r.blockers or 'нет'}"
                for r, name in reports
            )
            injected = (
                "[система] Стендап закрыт. Отчёты участников:\n" + body +
                "\n\nСверь их с текущим состоянием бэклога и спринта. Дай сводку: "
                "прогресс относительно цели спринта, блокеры, расхождения между "
                "заявленным и статусами задач, рекомендации. Если видишь нужные "
                "изменения — предложи их инструментами."
            )
            msg = await agent.run_turn(s, team, project, initiator="agent_scheduled",
                                       injected=injected)
            st.agent_summary = msg.content
            await s.commit()
            from .events import log_event
            await log_event(s, project_id=project.id, event_type="standup_completed",
                            entity_type="standup", entity_id=st.id,
                            after={"reports": len(reports)}, actor="agent")
            await s.commit()


# ------------------------------------------------------- фоновая проверка рисков


async def _findings(s: AsyncSession, team: Team, project: Project) -> list[dict]:
    """Правила §8. Сигнатура — ключ дедупликации."""
    out: list[dict] = []
    now = utcnow()

    blocked = (await s.execute(
        select(BacklogItem).where(
            BacklogItem.project_id == project.id, BacklogItem.status == "blocked",
            BacklogItem.updated_at < now - timedelta(days=config.BLOCKED_DAYS_THRESHOLD),
        )
    )).scalars().all()
    for i in blocked:
        out.append({
            "signature": f"blocked:{i.id}", "category": "schedule", "severity": "high",
            "title": f"Задача #{i.id} «{i.title}» заблокирована дольше "
                     f"{config.BLOCKED_DAYS_THRESHOLD} дней",
            "description": f"Статус blocked с {i.updated_at:%Y-%m-%d}. Движения нет.",
            "related_item_ids": [i.id],
        })

    sprint = (await s.execute(
        select(Sprint).where(Sprint.project_id == project.id, Sprint.status == "active")
    )).scalars().first()
    if sprint and sprint.ends_at and sprint.ends_at - now < timedelta(days=3):
        ac_ids = set((await s.execute(select(AcceptanceCriterion.backlog_item_id))).scalars())
        naked = (await s.execute(
            select(BacklogItem).where(BacklogItem.sprint_id == sprint.id,
                                      BacklogItem.status != "done")
        )).scalars().all()
        naked = [i for i in naked if i.id not in ac_ids]
        if naked:
            out.append({
                "signature": f"no_ac:sprint{sprint.id}", "category": "requirements",
                "severity": "high",
                "title": f"{len(naked)} задач спринта №{sprint.number} без Acceptance Criteria "
                         f"перед дедлайном",
                "description": "Без критериев приёмки нельзя проверить готовность: "
                               + ", ".join(f"#{i.id} {i.title}" for i in naked[:8]),
                "related_item_ids": [i.id for i in naked[:10]],
            })

    stale = (await s.execute(
        select(BacklogItem).where(
            BacklogItem.project_id == project.id, BacklogItem.priority == "must",
            BacklogItem.status == "new",
            BacklogItem.updated_at < now - timedelta(days=config.STALE_MUST_DAYS),
        )
    )).scalars().all()
    if stale:
        out.append({
            "signature": "stale_must", "category": "schedule", "severity": "medium",
            "title": f"{len(stale)} задач приоритета must не сдвинулись "
                     f"с места за {config.STALE_MUST_DAYS} дней",
            "description": ", ".join(f"#{i.id} {i.title}" for i in stale[:8]),
            "related_item_ids": [i.id for i in stale[:10]],
        })

    # §8: расхождение с требованиями — прямое требование ТЗ
    n_reqs = (await s.execute(
        select(func.count()).select_from(Requirement).where(
            Requirement.project_id == project.id, Requirement.status == "confirmed")
    )).scalar_one()
    in_work = (await s.execute(
        select(BacklogItem).where(BacklogItem.project_id == project.id,
                                  BacklogItem.status == "in_progress")
    )).scalars().all()
    if in_work and n_reqs == 0:
        out.append({
            "signature": "work_without_requirements", "category": "requirements",
            "severity": "high",
            "title": "Команда работает над задачами, не имея ни одного подтверждённого "
                     "требования",
            "description": f"В работе {len(in_work)} задач, требований в проекте нет. "
                           "Непонятно, чему должен соответствовать результат.",
            "related_item_ids": [i.id for i in in_work[:10]],
        })

    members = (await s.execute(
        select(User).where(User.team_id == team.id, User.is_active.is_(True))
    )).scalars().all()
    load = dict((await s.execute(
        select(BacklogItem.assignee_id, func.count()).where(
            BacklogItem.project_id == project.id,
            BacklogItem.status.in_(("new", "in_progress")),
        ).group_by(BacklogItem.assignee_id)
    )).all())
    idle = [u for u in members if not load.get(u.id)]
    if idle and sum(load.values()) >= len(members):
        out.append({
            "signature": "idle_members", "category": "team", "severity": "medium",
            "title": f"Без задач: {', '.join(u.full_name for u in idle)}",
            "description": "Работа распределена не на всю команду.",
            "related_item_ids": [],
        })
    if load:
        top_uid, top_n = max(load.items(), key=lambda kv: kv[1])
        if top_uid and top_n >= 5 and top_n > 2 * (sum(load.values()) / max(len(load), 1)):
            u = await s.get(User, top_uid)
            out.append({
                "signature": f"overload:{top_uid}", "category": "team", "severity": "medium",
                "title": f"Перегрузка: на {u.full_name if u else top_uid} "
                         f"{top_n} активных задач",
                "description": "Заметно больше, чем у остальных участников.",
                "related_item_ids": [],
            })

    n_pending = (await s.execute(
        select(func.count()).select_from(Proposal).where(
            Proposal.project_id == project.id, Proposal.status == "pending")
    )).scalar_one()
    if n_pending >= 5:
        out.append({
            "signature": "pending_pileup", "category": "scope", "severity": "low",
            "title": f"Накопилось {n_pending} нерешённых предложений",
            "description": "Решения по бэклогу не принимаются — состояние проекта "
                           "расходится с реальностью.",
            "related_item_ids": [],
        })

    last_msg = (await s.execute(
        select(func.max(Message.created_at)).where(
            Message.team_id == team.id, Message.author == "user")
    )).scalar()
    if last_msg and now - last_msg > timedelta(days=5):
        out.append({
            "signature": "activity_drop", "category": "team", "severity": "medium",
            "title": f"Команда не выходила на связь с {last_msg:%Y-%m-%d}",
            "description": "Активность упала.",
            "related_item_ids": [],
        })
    return out


async def _already_known(s: AsyncSession, project_id: int, sig: str) -> bool:
    open_risk = (await s.execute(
        select(Risk.id).where(Risk.project_id == project_id, Risk.signature == sig,
                              Risk.status == "open")
    )).scalars().first()
    if open_risk:
        return True
    # §8: не поднимать тот же риск повторно, пока команда не отреагировала
    pending = (await s.execute(
        select(Proposal).where(Proposal.project_id == project_id,
                               Proposal.type == "create_risk",
                               Proposal.status == "pending")
    )).scalars().all()
    return any((p.payload or {}).get("signature") == sig for p in pending)


async def risk_scan() -> None:
    async with Session() as s:
        team_ids = [t.id for t, _ in await _teams_with_projects(s)]

    # Сессия на команду: rollback при сбое одной не должен трогать остальные.
    for team_id in team_ids:
        async with Session() as s:
            team = await s.get(Team, team_id)
            project = (await s.execute(
                select(Project).where(Project.team_id == team_id))).scalars().first()
            if not (team and project):
                continue
            try:
                if not await proactive_allowed(s, team):
                    continue
                found = await _findings(s, team, project)
                fresh = [f for f in found if not await _already_known(s, project.id, f["signature"])]
                if not fresh:
                    continue
                ctx = ToolContext(session=s, project=project, team_id=team.id,
                                  is_proactive=True)
                # Риск создаётся тем же propose_create_risk, что и у агента —
                # но найден детерминированно, поэтому дедуп по signature надёжен.
                for f in fresh[:3]:
                    await propose_create_risk(
                        ctx, title=f["title"], description=f["description"],
                        severity=f["severity"], category=f["category"],
                        related_item_ids=f["related_item_ids"], signature=f["signature"],
                        rationale="Найдено фоновой проверкой состояния проекта.",
                    )
                s.add(Notification(team_id=team.id, type="risk",
                                   content=f"Новых рисков: {len(fresh[:3])}",
                                   link="/#risks"))
                await s.commit()
                bus.publish(team.id, "proposals", {"ids": []})

                text = "\n".join(f"- [{f['severity']}] {f['title']}" for f in fresh[:3])
                injected = (
                    "[система] Фоновая проверка нашла проблемы и уже оформила их "
                    "предложениями-рисками:\n" + text +
                    "\n\nКоротко (до 120 слов) объясни команде, чем это грозит и что "
                    "сделать в первую очередь. Инструменты вызывать не нужно."
                )
                await _proactive_say(s, team, project, injected)
            except Exception:
                log.exception("risk scan failed for team %s", team_id)
                await s.rollback()


async def _proactive_say(s: AsyncSession, team: Team, project: Project, injected: str) -> None:
    """Тихие часы: сообщение копится и уходит утром (§8)."""
    if in_quiet_hours():
        s.add(Notification(team_id=team.id, type=DEFERRED, content=injected))
        await s.commit()
        return
    await bus.run_serialized(
        team.id,
        lambda: agent.run_turn(s, team, project, initiator="agent_proactive",
                               injected=injected),
    )


async def flush_deferred() -> None:
    """Запускается на границе тихих часов."""
    if in_quiet_hours():
        return
    async with Session() as s:
        rows = (await s.execute(
            select(Notification).where(Notification.type == DEFERRED,
                                       Notification.read_at.is_(None))
        )).scalars().all()
        by_team: dict[int, list[Notification]] = {}
        for n in rows:
            by_team.setdefault(n.team_id, []).append(n)
        for team_id, items in by_team.items():
            team = await s.get(Team, team_id)
            project = (await s.execute(
                select(Project).where(Project.team_id == team_id)
            )).scalars().first()
            for n in items:
                n.read_at = utcnow()
            await s.commit()
            if not (team and project and await proactive_allowed(s, team)):
                continue
            await bus.run_serialized(
                team_id,
                lambda t=team, p=project, txt=items[0].content: agent.run_turn(
                    s, t, p, initiator="agent_proactive", injected=txt),
            )


async def remind_pending() -> None:
    """§8: напоминания о нерешённых предложениях и дедлайне спринта."""
    async with Session() as s:
        for team, project in await _teams_with_projects(s):
            if not await proactive_allowed(s, team):
                continue
            old = utcnow() - timedelta(days=2)
            n = (await s.execute(
                select(func.count()).select_from(Proposal).where(
                    Proposal.project_id == project.id, Proposal.status == "pending",
                    Proposal.created_at < old)
            )).scalar_one()
            sprint = (await s.execute(
                select(Sprint).where(Sprint.project_id == project.id,
                                     Sprint.status == "active")
            )).scalars().first()
            near = bool(sprint and sprint.ends_at
                        and timedelta(0) < sprint.ends_at - utcnow() < timedelta(days=2))
            if not n and not near:
                continue
            parts = []
            if n:
                parts.append(f"{n} предложений ждут решения дольше двух дней")
            if near:
                parts.append(f"спринт №{sprint.number} заканчивается "
                             f"{sprint.ends_at:%d.%m}")
            await _proactive_say(
                s, team, project,
                "[система] Напомни команде: " + "; ".join(parts) +
                ". Две-три фразы, без вызова инструментов.",
            )


async def expire_proposals() -> None:
    async with Session() as s:
        n = await prop.expire_stale(s)
        if n:
            log.info("expired %s proposals", n)


def build_scheduler() -> AsyncIOScheduler:
    sch = AsyncIOScheduler(timezone=TZ)
    sch.add_job(ensure_standups, "interval", minutes=5, id="standup_open",
                replace_existing=True, coalesce=True, max_instances=1)
    sch.add_job(close_standups, "interval", minutes=10, id="standup_close",
                replace_existing=True, coalesce=True, max_instances=1)
    sch.add_job(risk_scan, CronTrigger.from_crontab(config.RISK_SCAN_CRON, timezone=TZ),
                id="risk_scan", replace_existing=True, coalesce=True, max_instances=1)
    sch.add_job(flush_deferred, "cron", hour=config.QUIET_HOURS_END, minute=5,
                id="flush_deferred", replace_existing=True, coalesce=True)
    sch.add_job(remind_pending, "cron", hour=max(config.QUIET_HOURS_END, 10), minute=30,
                id="remind", replace_existing=True, coalesce=True)
    sch.add_job(expire_proposals, "cron", hour=3, minute=0, id="expire",
                replace_existing=True, coalesce=True)
    return sch
