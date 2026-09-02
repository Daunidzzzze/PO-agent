import os
import tempfile

# Должно быть до импорта app.* — engine создаётся на импорте модуля.
_db = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_db}"
os.environ["RUN_SCHEDULER"] = "0"
os.environ["OPENAI_API_KEY"] = ""          # агент не ходит в сеть в тестах
os.environ["ADMIN_LOGIN"] = "admin"
os.environ["ADMIN_PASSWORD"] = "secret"
os.environ["SECRET_KEY"] = "test-secret"

import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.db import Session, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base, Project, Team, User  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def fresh_db():
    """Чистая схема на каждый тест — иначе фикстуры наступают друг другу на коды."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


@pytest_asyncio.fixture
async def db():
    async with Session() as s:
        yield s


@pytest_asyncio.fixture
async def teams(db):
    """Две команды с проектами — базовая сцена для проверки изоляции."""
    made = {}
    for code, name, users in [("A", "Альфа", [("a1", "Аня")]),
                              ("B", "Бета", [("b1", "Боря")])]:
        team = Team(code=code, name=name)
        db.add(team)
        await db.flush()
        project = Project(team_id=team.id, title=f"Проект {name}")
        db.add(project)
        for ucode, fname in users:
            db.add(User(user_code=ucode, full_name=fname, team_id=team.id))
        await db.flush()
        made[code] = (team, project)
    await db.commit()
    return made


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def login_as(client: AsyncClient, team_code: str, user_code: str) -> None:
    r = await client.post("/login", data={"team_code": team_code,
                                          "user_code": user_code})
    assert r.status_code == 303, r.text
    await client.post("/consent")
