"""§5, §6, §9, §11, §14 — агент: контекст, вызов модели, цикл инструментов."""
import asyncio
import json
import logging
import time

from openai import AsyncOpenAI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import bus, config
from .models import (
    AcceptanceCriterion, BacklogItem, Conversation, DomainEvent, Message,
    ProductVision, Project, PromptVersion, Proposal, Requirement, Risk, Sprint,
    Team, TokenUsage, User, utcnow,
)
from .tools import HANDLERS, PROPOSE_TOOLS, TOOL_SCHEMAS, ToolContext, ToolError

log = logging.getLogger("agent")
client = AsyncOpenAI(api_key=config.OPENAI_API_KEY) if config.OPENAI_API_KEY else None

REQUEST_TYPES = [
    "requirements", "backlog", "prioritization", "decomposition", "acceptance_criteria",
    "architecture", "risk", "standup", "planning", "documentation", "process", "other",
]

DEFAULT_PROMPT = """Ты — Product Owner в студенческой команде, разрабатывающей робототехнический проект. Ты отвечаешь за то, чтобы команда строила правильный продукт: за требования, бэклог, приоритеты и критерии готовности.

Что ты делаешь:
- Помогаешь уточнить идею продукта и превратить её в конкретные требования.
- Ведёшь Product Backlog: создаёшь User Stories и задачи, декомпозируешь крупное, объединяешь дубли.
- Расставляешь приоритеты по MoSCoW и объясняешь каждое решение.
- Формулируешь Acceptance Criteria — проверяемые, а не общие.
- Следишь за соответствием работы заявленным требованиям и целям проекта.
- Модерируешь стендапы и даёшь рекомендации по итогам.
- Выявляешь риски и зависимости между задачами.

Как ты работаешь:
- Ты не описываешь изменения словами — ты их предлагаешь через доступные тебе инструменты. Если по ходу разговора нужно создать историю, изменить приоритет или добавить критерий — вызывай соответствующий инструмент.
- Каждое предложение сопровождаешь коротким обоснованием: почему именно так.
- Чётко отделяешь факты от рекомендаций. Факт — то, что есть в состоянии проекта. Рекомендация — твоё суждение.
- Опираешься на текущее состояние проекта и на ранее принятые решения, а не только на последние реплики.
- При нескольких разумных вариантах показываешь их с плюсами и минусами и объясняешь, какой предпочёл бы и почему.
- При нехватке информации сначала спрашиваешь. Не выдумывай требования, целевую аудиторию и ограничения за команду.
- Используешь терминологию Agile и Scrum: бэклог, User Story, Acceptance Criteria, спринт, инкремент, definition of done.

Чего ты не делаешь:
- Не принимаешь окончательные решения за команду — только предлагаешь.
- Не назначаешь ответственных без подтверждения участников.
- Не меняешь требования без согласования.
- Не принимаешь инженерные и технические решения за команду: выбор библиотек, алгоритмов, архитектуры реализации — их зона.
- Не пишешь программный код, если тебя об этом прямо не попросили.
- Не оцениваешь студентов и не заменяешь преподавателя. Вопросы об оценках, сроках сдачи и требованиях курса адресуешь преподавателю.
- Не выполняешь проект за команду.

Формат: Markdown, по существу, без вступлений. Обращение к команде на «вы», к участнику по имени. Не более 200 слов, если только не просят развёрнутый разбор."""


async def active_prompt(session: AsyncSession) -> PromptVersion:
    pv = (await session.execute(
        select(PromptVersion).where(PromptVersion.is_active.is_(True))
        .order_by(PromptVersion.version.desc())
    )).scalars().first()
    if pv:
        return pv
    pv = PromptVersion(name="po_agent", content=DEFAULT_PROMPT, version=1, is_active=True)
    session.add(pv)
    await session.commit()
    return pv


def _trunc(s: str, n: int) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


async def build_state_slice(session: AsyncSession, team: Team, project: Project) -> str:
    """§5 п.2. Компактный и стабильный по форме — идёт в каждый запрос."""
    L: list[str] = ["# Состояние проекта"]
    L.append(f"Название: {project.title or '—'}")
    L.append(f"Идея: {_trunc(project.idea_description, 400) or '—'}")
    L.append(f"Этап: {project.current_stage}")
    if project.goals:
        L.append(f"Цели: {_trunc(project.goals, 250)}")
    if project.constraints:
        L.append(f"Ограничения: {_trunc(project.constraints, 250)}")

    members = (await session.execute(
        select(User).where(User.team_id == team.id, User.is_active.is_(True))
    )).scalars().all()
    L.append("\n## Команда (user_id для назначений)")
    L += [f"- {u.id}: {u.full_name}" + (f" — {u.role_in_team}" if u.role_in_team else "")
          for u in members]

    vision = (await session.execute(
        select(ProductVision).where(ProductVision.project_id == project.id)
        .order_by(ProductVision.version.desc()).limit(1)
    )).scalars().first()
    L.append("\n## Product Vision")
    L.append(f"v{vision.version}: {_trunc(vision.content, 400)}" if vision else "не задан")

    reqs = (await session.execute(
        select(Requirement).where(Requirement.project_id == project.id,
                                  Requirement.status == "confirmed")
    )).scalars().all()
    L.append(f"\n## Подтверждённые требования ({len(reqs)})")
    L += [f"- [{r.id}|{r.type}] {_trunc(r.content, 160)}" for r in reqs[:25]] or ["нет"]

    ac_counts = dict((await session.execute(
        select(AcceptanceCriterion.backlog_item_id, func.count())
        .group_by(AcceptanceCriterion.backlog_item_id)
    )).all())
    names = {u.id: u.full_name for u in members}
    items = (await session.execute(
        select(BacklogItem).where(BacklogItem.project_id == project.id,
                                  BacklogItem.status != "cancelled")
        .order_by(BacklogItem.priority_order, BacklogItem.id)
    )).scalars().all()
    L.append(f"\n## Бэклог ({len(items)}) — только заголовки, детали через get_backlog_item")
    if not items:
        L.append("пуст")
    for prio in ("must", "should", "could", "wont"):
        group = [i for i in items if i.priority == prio]
        if not group:
            continue
        L.append(f"{prio.upper()}:")
        for i in group[:40]:
            who = f" @{names.get(i.assignee_id)}" if i.assignee_id else ""
            par = f" ^{i.parent_id}" if i.parent_id else ""
            L.append(f"  #{i.id} [{i.status}] {_trunc(i.title, 90)} "
                     f"(AC:{ac_counts.get(i.id, 0)}){who}{par}")

    sprint = (await session.execute(
        select(Sprint).where(Sprint.project_id == project.id, Sprint.status == "active")
        .order_by(Sprint.number.desc()).limit(1)
    )).scalars().first()
    L.append("\n## Активный спринт")
    L.append(f"№{sprint.number} «{_trunc(sprint.goal, 150)}», до "
             f"{sprint.ends_at.date() if sprint.ends_at else '—'}" if sprint else "нет")

    risks = (await session.execute(
        select(Risk).where(Risk.project_id == project.id, Risk.status == "open")
    )).scalars().all()
    L.append(f"\n## Открытые риски ({len(risks)})")
    L += [f"- [{r.severity}/{r.category}] {_trunc(r.title, 120)}" for r in risks[:10]] or ["нет"]

    pending = (await session.execute(
        select(Proposal).where(Proposal.project_id == project.id, Proposal.status == "pending")
    )).scalars().all()
    L.append(f"\n## Нерешённые предложения: {len(pending)} — НЕ предлагай это повторно")
    for p in pending[:10]:
        payload = p.payload or {}
        titles = [r.get("title") or r.get("content") or ""
                  for key in ("stories", "tasks", "subitems", "criteria", "changes")
                  for r in payload.get(key) or []]
        gist = "; ".join(_trunc(t, 60) for t in titles[:6]) or _trunc(
            payload.get("title") or payload.get("goal") or payload.get("content", ""), 80)
        L.append(f"- #{p.id} {p.type}: {gist}")

    # §5: ранее принятые решения = журнал событий, а не пересказ переписки
    events = (await session.execute(
        select(DomainEvent).where(DomainEvent.project_id == project.id)
        .order_by(DomainEvent.id.desc()).limit(20)
    )).scalars().all()
    L.append("\n## Последние решения (свежие сверху)")
    for e in events:
        L.append(f"- {e.created_at:%m-%d %H:%M} {e.event_type} "
                 f"{e.entity_type}#{e.entity_id} ({e.actor})")
    return "\n".join(L)


async def _history(session: AsyncSession, team_id: int) -> list[dict]:
    rows = (await session.execute(
        select(Message).where(Message.team_id == team_id)
        .order_by(Message.id.desc()).limit(config.CONTEXT_MESSAGES_LIMIT)
    )).scalars().all()
    out = []
    for m in reversed(rows):
        if m.author == "agent":
            out.append({"role": "assistant", "content": m.content})
        else:
            who = m.author_user_id
            name = ""
            if who:
                u = await session.get(User, who)
                name = u.full_name if u else ""
            prefix = f"[{name}] " if name else "[система] "
            out.append({"role": "user", "content": prefix + m.content})
    return out


async def _record_usage(session: AsyncSession, team_id: int | None, kind: str,
                        model: str, tin: int, tout: int) -> None:
    session.add(TokenUsage(team_id=team_id, kind=kind, model=model,
                           tokens_in=tin, tokens_out=tout))


async def run_turn(
    session: AsyncSession,
    team: Team,
    project: Project,
    *,
    initiator: str = "user",
    injected: str | None = None,
    source_message_id: int | None = None,
    resolver_user_id: int | None = None,
) -> Message:
    """Один ход агента. Пишет сообщение агента в БД и стримит его в SSE."""
    if client is None:
        return await _fallback(session, team, "OPENAI_API_KEY не задан — агент отключён.",
                               initiator)

    started = time.monotonic()
    pv = await active_prompt(session)
    slice_text = await build_state_slice(session, team, project)

    # Порядок важен для prompt caching (§14): статический префикс сначала.
    messages = [
        {"role": "system", "content": pv.content},
        {"role": "system", "content": slice_text},
    ]
    messages += await _history(session, team.id)
    if injected:
        messages.append({"role": "user", "content": injected})

    ctx = ToolContext(session=session, project=project, team_id=team.id,
                      user_id=resolver_user_id, message_id=source_message_id,
                      is_proactive=initiator == "agent_proactive")

    tool_log: list[dict] = []
    tin = tout = 0
    text = ""
    proposal_ids: list[int] = []
    retries_left = 1  # §6: одна попытка исправиться на неверных аргументах

    for _ in range(6):
        content, calls, u_in, u_out = await _stream_round(team.id, messages)
        tin += u_in
        tout += u_out
        text += content
        if not calls:
            break
        messages.append({"role": "assistant", "content": content or None,
                         "tool_calls": calls})
        had_error = False
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            handler = HANDLERS.get(name)
            if handler is None:
                result, ok = {"error": f"Инструмента {name} не существует."}, False
            else:
                try:
                    result, ok = await handler(ctx, **args), True
                    if name in PROPOSE_TOOLS:
                        proposal_ids.append(result["proposal_id"])
                except ToolError as e:
                    result, ok = {"error": str(e)}, False
                except Exception as e:  # §16: сбой инструмента не роняет диалог
                    log.exception("tool %s failed", name)
                    result, ok = {"error": f"Внутренняя ошибка инструмента: {e}"}, False
            had_error = had_error or not ok
            tool_log.append({"name": name, "args": args, "ok": ok})
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(result, ensure_ascii=False)[:4000]})
        if had_error:
            if retries_left <= 0:
                messages.append({"role": "system", "content":
                                 "Больше не вызывай инструменты. Ответь команде текстом."})
            retries_left -= 1

    prev_agent = (await session.execute(
        select(Message).where(Message.team_id == team.id, Message.author == "agent")
        .order_by(Message.id.desc()).limit(1)
    )).scalars().first()

    msg = Message(
        conversation_id=(await _conversation_id(session, team.id)),
        team_id=team.id, author="agent",
        content=text.strip() or "_(пустой ответ модели)_",
        model=config.OPENAI_MODEL, prompt_version_id=pv.id,
        tokens_in=tin, tokens_out=tout,
        latency_ms=int((time.monotonic() - started) * 1000),
        project_stage=project.current_stage, initiator=initiator,
        tool_calls=tool_log, related_item_ids=proposal_ids or None,
        time_since_prev_agent_ms=(
            int((utcnow() - prev_agent.created_at).total_seconds() * 1000)
            if prev_agent else None
        ),
    )
    session.add(msg)
    await _record_usage(session, team.id, "chat", config.OPENAI_MODEL, tin, tout)
    await session.commit()

    bus.publish(team.id, "message", {"id": msg.id})
    if proposal_ids:
        bus.publish(team.id, "proposals", {"ids": proposal_ids})
    return msg


async def _stream_round(team_id: int, messages: list) -> tuple[str, list, int, int]:
    """Один вызов модели со стримингом текста в SSE-шину."""
    stream = await client.chat.completions.create(
        model=config.OPENAI_MODEL,
        messages=messages,
        tools=TOOL_SCHEMAS,
        max_completion_tokens=config.MAX_RESPONSE_TOKENS,
        stream=True,
        stream_options={"include_usage": True},
    )
    content = ""
    calls: dict[int, dict] = {}
    tin = tout = 0
    async for chunk in stream:
        if chunk.usage:
            tin, tout = chunk.usage.prompt_tokens, chunk.usage.completion_tokens
        if not chunk.choices:
            continue
        d = chunk.choices[0].delta
        if d.content:
            content += d.content
            bus.publish(team_id, "token", {"t": d.content})
        for tc in d.tool_calls or []:
            slot = calls.setdefault(tc.index, {"id": "", "type": "function",
                                               "function": {"name": "", "arguments": ""}})
            if tc.id:
                slot["id"] = tc.id
            if tc.function and tc.function.name:
                slot["function"]["name"] = tc.function.name
            if tc.function and tc.function.arguments:
                slot["function"]["arguments"] += tc.function.arguments
    return content, [calls[k] for k in sorted(calls)], tin, tout


async def _conversation_id(session: AsyncSession, team_id: int) -> int:
    conv = (await session.execute(
        select(Conversation).where(Conversation.team_id == team_id)
    )).scalars().first()
    if not conv:
        conv = Conversation(team_id=team_id)
        session.add(conv)
        await session.flush()
    return conv.id


async def _fallback(session: AsyncSession, team: Team, text: str, initiator: str) -> Message:
    msg = Message(conversation_id=await _conversation_id(session, team.id),
                  team_id=team.id, author="system", content=text, initiator=initiator)
    session.add(msg)
    await session.commit()
    bus.publish(team.id, "message", {"id": msg.id})
    return msg


# ------------------------------------------------------------ §11 классификатор

CLASSIFY_PROMPT = (
    "Определи тип обращения участника студенческой команды к ИИ Product Owner. "
    "Ровно одно значение из списка: " + ", ".join(REQUEST_TYPES) + ". "
    'Ответь JSON: {"type": "...", "confidence": 0.0-1.0}'
)


async def classify(text: str) -> tuple[str | None, float | None]:
    if client is None or not text.strip():
        return None, None
    try:
        r = await client.chat.completions.create(
            model=config.OPENAI_UTILITY_MODEL,
            messages=[{"role": "system", "content": CLASSIFY_PROMPT},
                      {"role": "user", "content": text[:2000]}],
            response_format={"type": "json_object"},
            max_completion_tokens=200,
        )
        data = json.loads(r.choices[0].message.content or "{}")
        t = data.get("type")
        return (t if t in REQUEST_TYPES else "other"), float(data.get("confidence", 0))
    except Exception:
        log.exception("classify failed")
        return None, None


async def classify_and_store(session_factory, message_id: int, team_id: int) -> None:
    """Асинхронно: ответ агента не ждёт классификацию (§11)."""
    async with session_factory() as s:
        msg = await s.get(Message, message_id)
        if not msg:
            return
        t, conf = await classify(msg.content)
        if t is None:
            return
        msg.request_type, msg.request_type_confidence = t, conf
        s.add(TokenUsage(team_id=team_id, kind="classify",
                         model=config.OPENAI_UTILITY_MODEL, tokens_in=0, tokens_out=0))
        await s.commit()


def spawn(coro) -> None:
    """Фоновая задача, чьё падение не должно ронять запрос."""
    task = asyncio.create_task(coro)
    task.add_done_callback(lambda t: t.cancelled() or t.exception() and
                           log.error("background task failed", exc_info=t.exception()))
