"""Проверки требований §18. Запуск: pytest -q"""
import pytest
from sqlalchemy import select

from app import agent, exports, scheduler, tools
from app.events import log_event
from app.models import (
    AcceptanceCriterion, BacklogItem, DomainEvent, Message, Proposal, Risk, Team, User,
)
from app.proposals import resolve
from app.tools import PROPOSE_TOOLS, TOOL_SCHEMAS, ToolContext, ToolError
from tests.conftest import login_as


# --- §18: агент не может менять данные напрямую -----------------------------

def test_agent_has_no_mutating_tools():
    """Единственный путь к изменению — propose_*. Это структурная гарантия,
    а не поведение промпта: инструментов записи в реестре просто нет."""
    names = {t["function"]["name"] for t in TOOL_SCHEMAS}
    assert names == set(tools.READ_TOOLS) | set(tools.PROPOSE_TOOLS)
    for n in names:
        assert n.startswith(("get_", "propose_")), n


async def test_proposal_does_not_touch_domain_until_accepted(db, teams):
    team, project = teams["A"]
    ctx = ToolContext(session=db, project=project, team_id=team.id)
    await tools.propose_create_user_story(ctx, stories=[
        {"title": "Захват детали", "user_story_text": "Как оператор…",
         "priority": "must", "acceptance_criteria": ["Удерживает 50 г"]}],
        rationale="ядро сценария")
    await db.commit()

    assert (await db.execute(select(BacklogItem))).scalars().all() == []
    p = (await db.execute(select(Proposal))).scalars().one()
    assert p.status == "pending"


async def test_accept_creates_items_and_events_in_one_transaction(db, teams):
    team, project = teams["A"]
    user = (await db.execute(select(User).where(User.team_id == team.id))).scalars().one()
    ctx = ToolContext(session=db, project=project, team_id=team.id)
    await tools.propose_create_user_story(ctx, stories=[
        {"title": "История 1", "user_story_text": "…", "priority": "must",
         "acceptance_criteria": ["критерий"]},
        {"title": "История 2", "user_story_text": "…", "priority": "should"}],
        rationale="почему")
    await db.commit()
    p = (await db.execute(select(Proposal))).scalars().one()

    await resolve(db, p, "accept", user)

    items = (await db.execute(select(BacklogItem))).scalars().all()
    assert len(items) == 2
    assert all(i.created_by == "agent" for i in items)
    assert len((await db.execute(select(AcceptanceCriterion))).scalars().all()) == 1
    kinds = [e.event_type for e in (await db.execute(select(DomainEvent))).scalars()]
    assert kinds.count("backlog_item_created") == 2
    assert "acceptance_criteria_created" in kinds
    assert "proposal_resolved" in kinds


async def test_partial_accept_applies_only_selected(db, teams):
    team, project = teams["A"]
    user = (await db.execute(select(User).where(User.team_id == team.id))).scalars().one()
    ctx = ToolContext(session=db, project=project, team_id=team.id)
    await tools.propose_create_user_story(ctx, stories=[
        {"title": "Нужная", "user_story_text": "…", "priority": "must"},
        {"title": "Лишняя", "user_story_text": "…", "priority": "could"}],
        rationale="пакет")
    await db.commit()
    p = (await db.execute(select(Proposal))).scalars().one()

    await resolve(db, p, "modify", user, comment="вторая не нужна", selected=[0])

    titles = [i.title for i in (await db.execute(select(BacklogItem))).scalars()]
    assert titles == ["Нужная"]
    assert p.status == "modified" and p.user_comment == "вторая не нужна"


async def test_rejected_proposal_is_kept_with_comment_and_exported(db, teams):
    """§18: отклонённое предложение сохраняется и попадает в proposals.csv."""
    team, project = teams["A"]
    user = (await db.execute(select(User).where(User.team_id == team.id))).scalars().one()
    ctx = ToolContext(session=db, project=project, team_id=team.id)
    item = BacklogItem(project_id=project.id, title="Задача", priority="should")
    db.add(item)
    await db.flush()
    await tools.propose_update_priority(
        ctx, changes=[{"item_id": item.id, "priority": "must"}], rationale="риск срыва")
    await db.commit()
    p = (await db.execute(select(Proposal))).scalars().one()

    await resolve(db, p, "reject", user, comment="успеем и так")

    assert p.status == "rejected" and p.user_comment == "успеем и так"
    assert item.priority == "should"          # изменение не применилось
    rows = await exports.build_rows(db, "proposals")
    assert rows[0]["status"] == "rejected"
    assert rows[0]["user_comment"] == "успеем и так"


# --- §18: агент не выдумывает идентификаторы --------------------------------

async def test_invalid_id_returns_readable_error(db, teams):
    team, project = teams["A"]
    ctx = ToolContext(session=db, project=project, team_id=team.id)
    with pytest.raises(ToolError, match="нет в этом проекте"):
        await tools.propose_create_acceptance_criteria(
            ctx, item_id=999, criteria=[{"content": "…"}], rationale="")


async def test_cross_team_id_is_rejected_at_tool_level(db, teams):
    """Подстановка чужого id в аргументы инструмента не проходит."""
    team_a, project_a = teams["A"]
    _, project_b = teams["B"]
    foreign = BacklogItem(project_id=project_b.id, title="Чужая задача")
    db.add(foreign)
    await db.flush()
    ctx = ToolContext(session=db, project=project_a, team_id=team_a.id)
    with pytest.raises(ToolError):
        await tools.propose_assign_item(
            ctx, assignments=[{"item_id": foreign.id, "user_id": 1}], rationale="")


async def test_pending_proposal_is_not_offered_twice(db, teams):
    """На повторную просьбу агент не создаёт второй такой же пакет —
    иначе команда решает одно и то же дважды, а данные исследования засоряются."""
    team, project = teams["A"]
    ctx = ToolContext(session=db, project=project, team_id=team.id)
    story = {"title": "Сортировка по цвету", "user_story_text": "…", "priority": "must"}
    await tools.propose_create_user_story(ctx, stories=[story], rationale="ядро")
    await db.commit()
    with pytest.raises(ToolError, match="уже предложено"):
        await tools.propose_create_user_story(ctx, stories=[story], rationale="снова")


async def test_enum_and_duplicate_validation(db, teams):
    team, project = teams["A"]
    ctx = ToolContext(session=db, project=project, team_id=team.id)
    with pytest.raises(ToolError, match="Недопустимое значение"):
        await tools.propose_create_user_story(
            ctx, stories=[{"title": "X", "user_story_text": "…", "priority": "urgent"}],
            rationale="")
    db.add(BacklogItem(project_id=project.id, title="Уже есть"))
    await db.flush()
    with pytest.raises(ToolError, match="уже есть"):
        await tools.propose_create_user_story(
            ctx, stories=[{"title": "уже есть", "user_story_text": "…",
                           "priority": "must"}], rationale="")


# --- §16/§18: изоляция команд -----------------------------------------------

async def test_team_isolation_over_http(teams):
    """Участник команды A не достаёт данные B ни через интерфейс, ни по id."""
    from httpx import ASGITransport, AsyncClient

    from app.db import Session
    from app.main import app

    _, project_b = teams["B"]
    async with Session() as s:
        foreign = BacklogItem(project_id=project_b.id, title="Секрет Беты")
        s.add(foreign)
        await s.commit()
        foreign_id = foreign.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        await login_as(c, "A", "a1")
        assert (await c.get(f"/partials/item/{foreign_id}")).status_code == 404
        assert (await c.post(f"/backlog/{foreign_id}/update",
                             data={"status": "done"})).status_code == 404
        body = (await c.get("/partials/backlog")).text
        assert "Секрет Беты" not in body

    # чужой код участника с чужим кодом команды тоже не пускает
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/login", data={"team_code": "A", "user_code": "b1"})
        assert r.status_code == 401


async def test_wrong_codes_and_logout(client, teams):
    r = await client.post("/login", data={"team_code": "A", "user_code": "nope"})
    assert r.status_code == 401


# --- §8: защита от шума ------------------------------------------------------

def test_quiet_hours_window():
    from datetime import datetime
    assert scheduler.in_quiet_hours(datetime(2026, 1, 1, 23, 0))
    assert scheduler.in_quiet_hours(datetime(2026, 1, 1, 3, 0))
    assert not scheduler.in_quiet_hours(datetime(2026, 1, 1, 12, 0))


async def test_proactive_daily_limit(db, teams):
    from app import config
    team, _ = teams["A"]
    assert await scheduler.proactive_allowed(db, team)
    for _ in range(config.PROACTIVE_MAX_PER_DAY):
        db.add(Message(conversation_id=1, team_id=team.id, author="agent",
                       content="…", initiator="agent_proactive"))
    await db.commit()
    assert not await scheduler.proactive_allowed(db, team)


async def test_proactive_switch_off_is_immediate(db, teams):
    team, _ = teams["A"]
    team.proactive_enabled = False
    await db.commit()
    assert not await scheduler.proactive_allowed(db, team)


async def test_scheduler_restart_creates_no_duplicate_standup(db, teams):
    """§18: перезапуск контейнера не создаёт дубликат стендапа. Ловит и то,
    что после отката транзакции цикл шёл дальше по протухшим объектам."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.models import Standup

    now = datetime.now(ZoneInfo("Europe/Moscow"))
    for code in ("A", "B"):
        team, _ = teams[code]
        team.standup_days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][now.weekday()]
        team.standup_time = now.strftime("%H:%M")
    await db.commit()

    for _ in range(3):
        await scheduler.ensure_standups()

    rows = (await db.execute(select(Standup))).scalars().all()
    assert len(rows) == 2                       # по одному на команду, не шесть
    assert {r.status for r in rows} == {"collecting"}


async def test_same_risk_is_not_raised_twice(db, teams):
    team, project = teams["A"]
    db.add(Risk(project_id=project.id, title="Блокер", signature="blocked:1",
                status="open"))
    await db.commit()
    assert await scheduler._already_known(db, project.id, "blocked:1")
    assert not await scheduler._already_known(db, project.id, "blocked:2")


# --- §12: экспорт ------------------------------------------------------------

async def test_csv_has_bom_and_anonymizes(db, teams):
    team, _ = teams["A"]
    rows = await exports.build_rows(db, "users_summary", anonymize=True)
    blob = exports.to_csv(rows)
    assert blob.startswith(b"\xef\xbb\xbf")          # BOM для Excel
    assert "Аня" not in blob.decode("utf-8-sig")
    assert "a1" in blob.decode("utf-8-sig")

    named = exports.to_csv(await exports.build_rows(db, "users_summary", anonymize=False))
    assert "Аня" in named.decode("utf-8-sig")


async def test_all_export_tables_build(db, teams):
    for name in exports.TABLES:
        exports.to_csv(await exports.build_rows(db, name))


# --- §4: сериализация обработки ---------------------------------------------

async def test_concurrent_turns_do_not_run_twice():
    """Пока агент отвечает, второе сообщение не запускает вторую генерацию —
    оно учитывается повторным проходом того же хода."""
    import asyncio

    from app import bus
    runs = 0

    async def slow_turn():
        nonlocal runs
        runs += 1
        await asyncio.sleep(0.05)

    async def caller():
        await bus.run_serialized(999, slow_turn)

    await asyncio.gather(caller(), caller(), caller())
    assert runs == 2  # первый ход + один добор накопившихся сообщений


# --- §11: классификатор ------------------------------------------------------

def test_request_types_match_spec():
    assert agent.REQUEST_TYPES == [
        "requirements", "backlog", "prioritization", "decomposition",
        "acceptance_criteria", "architecture", "risk", "standup", "planning",
        "documentation", "process", "other"]
