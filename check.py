"""Ручной запуск фоновых задач — чтобы не ждать расписания при проверке.

  python check.py state          что сейчас в базе
  python check.py risk-scan      фоновая проверка рисков (обычно раз в сутки)
  python check.py standup-open   открыть стендап прямо сейчас
  python check.py standup-close  закрыть сбор и получить сводку агента
  python check.py expire         просрочить нерешённые предложения
  python check.py age-proposals  состарить предложения на 10 дней (для expire)
  python check.py remind         напоминание о зависших предложениях и дедлайне
  python check.py flush          отправить отложенное тихими часами прямо сейчас
  python check.py say            любое проактивное сообщение сейчас, мимо тихих часов
  python check.py reset          снести базу и залить демо-данные заново
"""
import asyncio
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app import config, scheduler
from app.db import Session
from app.models import (
    BacklogItem, DomainEvent, Message, Proposal, Risk, Standup, Team, utcnow,
)


async def state() -> None:
    async with Session() as s:
        for team in (await s.execute(select(Team))).scalars():
            print(f"\n=== {team.name} ({team.code}) "
                  f"проактивность: {'вкл' if team.proactive_enabled else 'выкл'}, "
                  f"стендап {team.standup_days} в {team.standup_time}")
            msgs = (await s.execute(
                select(Message).where(Message.team_id == team.id))).scalars().all()
            print(f"  сообщений: {len(msgs)} "
                  f"(проактивных: {sum(1 for m in msgs if m.initiator == 'agent_proactive')})")
            props = (await s.execute(select(Proposal))).scalars().all()
            by_status: dict[str, int] = {}
            for p in props:
                by_status[p.status] = by_status.get(p.status, 0) + 1
            print(f"  предложения: {by_status or 'нет'}")
            st = (await s.execute(
                select(Standup).where(Standup.team_id == team.id))).scalars().all()
            print(f"  стендапы: {[x.status for x in st] or 'нет'}")
        items = (await s.execute(select(BacklogItem))).scalars().all()
        print(f"\nбэклог: {len(items)} "
              f"(создано агентом: {sum(1 for i in items if i.created_by == 'agent')})")
        print(f"риски: {len((await s.execute(select(Risk))).scalars().all())}")
        print(f"доменных событий: "
              f"{len((await s.execute(select(DomainEvent))).scalars().all())}")


async def standup_open() -> None:
    now = datetime.now(ZoneInfo(config.TZNAME))
    async with Session() as s:
        for t in (await s.execute(select(Team))).scalars():
            t.standup_days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][now.weekday()]
            t.standup_time = now.strftime("%H:%M")
        await s.commit()
    await scheduler.ensure_standups()
    await scheduler.ensure_standups()   # второй раз — дублей быть не должно
    async with Session() as s:
        rows = (await s.execute(
            select(Standup).where(Standup.status == "collecting"))).scalars().all()
    print(f"открыто стендапов: {len(rows)} (после двух запусков подряд)")
    print("теперь заполните форму на вкладке «Стендап», потом: python check.py standup-close")


async def standup_close() -> None:
    async with Session() as s:
        for st in (await s.execute(
                select(Standup).where(Standup.status == "collecting"))).scalars():
            st.scheduled_at = utcnow() - timedelta(days=1)   # имитируем истёкший сбор
        await s.commit()
    await scheduler.close_standups()
    async with Session() as s:
        for st in (await s.execute(select(Standup).order_by(Standup.id.desc())
                                   .limit(3))).scalars():
            print(f"\n--- стендап {st.id}: {st.status}")
            print((st.agent_summary or "(без сводки — отчётов не было)")[:800])


async def age_proposals() -> None:
    async with Session() as s:
        rows = (await s.execute(
            select(Proposal).where(Proposal.status == "pending"))).scalars().all()
        for p in rows:
            p.created_at = utcnow() - timedelta(days=config.PROPOSAL_EXPIRY_DAYS + 5)
        await s.commit()
    print(f"состарено предложений: {len(rows)}. Теперь: python check.py expire")


async def flush() -> None:
    """В тихие часы проактивные сообщения копятся. Здесь снимаем окно, чтобы
    проверить доставку не дожидаясь утра."""
    was = scheduler.in_quiet_hours
    scheduler.in_quiet_hours = lambda *a, **k: False
    try:
        pending = (await _deferred_count())
        print(f"отложенных сообщений: {pending}")
        await scheduler.flush_deferred()
    finally:
        scheduler.in_quiet_hours = was
    async with Session() as s:
        rows = (await s.execute(
            select(Message).where(Message.initiator == "agent_proactive")
            .order_by(Message.id.desc()).limit(2))).scalars().all()
        for m in rows:
            print(f"\n--- проактивное сообщение {m.id}")
            print(m.content[:700])


async def _deferred_count() -> int:
    from app.models import Notification
    async with Session() as s:
        return len((await s.execute(
            select(Notification).where(Notification.type == scheduler.DEFERRED,
                                       Notification.read_at.is_(None)))).scalars().all())


async def say() -> None:
    """Полный цикл проактивности без ожидания расписания и тихих часов:
    найти проблемы -> оформить рисками -> написать команде в чат."""
    was = scheduler.in_quiet_hours
    scheduler.in_quiet_hours = lambda *a, **k: False
    try:
        await scheduler.risk_scan()
        await scheduler.remind_pending()
        await scheduler.flush_deferred()
    finally:
        scheduler.in_quiet_hours = was
    async with Session() as s:
        rows = (await s.execute(
            select(Message).where(Message.initiator.in_(
                ("agent_proactive", "agent_scheduled")))
            .order_by(Message.id.desc()).limit(3))).scalars().all()
        if not rows:
            print("агенту нечего сказать: проблем не найдено либо исчерпан "
                  f"суточный лимит ({config.PROACTIVE_MAX_PER_DAY} сообщения)")
        for m in reversed(rows):
            print(f"\n--- {m.initiator} #{m.id}\n{m.content[:900]}")


COMMANDS = {
    "state": state,
    "flush": flush,
    "say": say,
    "remind": scheduler.remind_pending,
    "risk-scan": scheduler.risk_scan,
    "standup-open": standup_open,
    "standup-close": standup_close,
    "expire": scheduler.expire_proposals,
    "age-proposals": age_proposals,
}


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "state"
    if cmd == "reset":
        import os
        import subprocess
        from app.config import ROOT
        db = ROOT / "po_agent.db"
        if db.exists():
            os.remove(db)
        subprocess.run([sys.executable, str(ROOT / "seed.py")], check=True)
        return
    if cmd not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    asyncio.run(COMMANDS[cmd]())
    print("\nготово")


if __name__ == "__main__":
    main()
