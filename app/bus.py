"""SSE-шина и сериализация обработки внутри команды (§4).

ponytail: состояние в памяти процесса. Одна реплика web — этого хватает
на десятки пользователей. Понадобится вторая — вынести в Redis pub/sub.
"""
import asyncio
import json
from collections import defaultdict

_subscribers: dict[int, set[asyncio.Queue]] = defaultdict(set)
_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_dirty: set[int] = set()


def subscribe(team_id: int) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subscribers[team_id].add(q)
    return q


def unsubscribe(team_id: int, q: asyncio.Queue) -> None:
    _subscribers[team_id].discard(q)


def publish(team_id: int, event: str, data: dict | str = "") -> None:
    payload = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else data
    for q in list(_subscribers[team_id]):
        try:
            q.put_nowait((event, payload))
        except asyncio.QueueFull:
            _subscribers[team_id].discard(q)


def busy(team_id: int) -> bool:
    return _locks[team_id].locked()


async def run_serialized(team_id: int, coro_factory) -> None:
    """Один ход агента на команду. Пришедшие во время генерации сообщения
    не запускают вторую генерацию — они попадут в контекст следующего хода."""
    if busy(team_id):
        _dirty.add(team_id)
        return
    async with _locks[team_id]:
        while True:
            _dirty.discard(team_id)
            await coro_factory()
            if team_id not in _dirty:
                break
