"""Демо-данные для локальной проверки: python seed.py"""
import asyncio
from datetime import timedelta

from sqlalchemy import select

from app.db import Session, init_db
from app.events import log_event, snapshot
from app.models import (
    AcceptanceCriterion, BacklogItem, Project, Requirement, Sprint, Team, User, utcnow,
)

DEMO = [
    ("R1", "Команда «Манипулятор»", [
        ("s101", "Анна Ковалёва", "тимлид"),
        ("s102", "Пётр Ильин", "конструктор"),
        ("s103", "Мария Гусева", "разработчик"),
        ("s104", "Игорь Дёмин", "электроника"),
    ]),
    ("R2", "Команда «Ровер»", [
        ("s201", "Олег Титов", "тимлид"),
        ("s202", "Дарья Белова", "разработчик"),
        ("s203", "Кирилл Марков", "механика"),
    ]),
]


async def main() -> None:
    await init_db()
    async with Session() as s:
        if (await s.execute(select(Team))).scalars().first():
            print("Данные уже есть — сид пропущен.")
            return

        for code, name, members in DEMO:
            team = Team(code=code, name=name)
            s.add(team)
            await s.flush()
            project = Project(
                team_id=team.id,
                title="Роботизированный манипулятор для сортировки"
                if code == "R1" else "Автономный ровер для теплицы",
                idea_description=(
                    "Настольный манипулятор с камерой, который сортирует детали "
                    "по цвету и размеру в лотки."
                    if code == "R1" else
                    "Небольшой колёсный робот, объезжающий грядки и снимающий "
                    "показания влажности почвы."),
                goals="Показать рабочий прототип на защите в конце семестра.",
                constraints="Бюджет 15 000 ₽, доступен только принтер FDM и Arduino.",
                current_stage="requirements",
            )
            s.add(project)
            for ucode, fname, role in members:
                s.add(User(user_code=ucode, full_name=fname, team_id=team.id,
                           role_in_team=role))
            await s.flush()

            if code == "R1":
                for t, content in [
                    ("functional", "Манипулятор берёт деталь из зоны загрузки"),
                    ("functional", "Система распознаёт цвет детали по камере"),
                    ("non_functional", "Цикл сортировки одной детали — не более 8 секунд"),
                    ("constraint", "Питание только от 12 В блока, без сети 220 В в рабочей зоне"),
                ]:
                    r = Requirement(project_id=project.id, type=t, content=content,
                                    source="user", status="confirmed")
                    s.add(r)
                    await s.flush()
                    await log_event(s, project_id=project.id,
                                    event_type="requirement_created",
                                    entity_type="requirement", entity_id=r.id,
                                    after=snapshot(r), actor="user")

                sprint = Sprint(project_id=project.id, number=1,
                                goal="Собрать механику и захват",
                                starts_at=utcnow() - timedelta(days=5),
                                ends_at=utcnow() + timedelta(days=9), status="active")
                s.add(sprint)
                await s.flush()

                for i, (title, prio, status, ac) in enumerate([
                    ("Собрать раму манипулятора", "must", "done",
                     ["Рама выдерживает 2 кг без деформации"]),
                    ("Реализовать захват детали", "must", "in_progress",
                     ["Захват удерживает деталь 50 г", "Отпускание по команде"]),
                    ("Калибровка камеры", "must", "blocked", []),
                    ("Распознавание цвета", "should", "new", []),
                    ("Веб-интерфейс оператора", "could", "new", []),
                ]):
                    item = BacklogItem(
                        project_id=project.id, title=title, priority=prio,
                        status=status, priority_order=i, sprint_id=sprint.id,
                        user_story_text=f"Как оператор, я хочу {title.lower()}, "
                                        f"чтобы сортировка работала надёжно",
                        created_by="user",
                        updated_at=utcnow() - timedelta(days=6 if status == "blocked" else 1),
                    )
                    s.add(item)
                    await s.flush()
                    await log_event(s, project_id=project.id,
                                    event_type="backlog_item_created",
                                    entity_type="backlog_item", entity_id=item.id,
                                    after=snapshot(item), actor="user")
                    for text in ac:
                        s.add(AcceptanceCriterion(backlog_item_id=item.id, content=text,
                                                  created_by="user"))
        await s.commit()

    print("Готово. Входы:")
    for code, name, members in DEMO:
        print(f"  {name}: код команды {code}, участники "
              + ", ".join(u[0] for u in members))


if __name__ == "__main__":
    asyncio.run(main())
