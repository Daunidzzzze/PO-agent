import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    f = ROOT / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()


def _s(k: str, d: str = "") -> str:
    return os.getenv(k, d)


def _i(k: str, d: int) -> int:
    try:
        return int(os.getenv(k) or d)
    except ValueError:
        return d


def _b(k: str, d: bool) -> bool:
    return (os.getenv(k) or str(d)).strip().lower() in ("1", "true", "yes", "on")


OPENAI_API_KEY = _s("OPENAI_API_KEY")
OPENAI_MODEL = _s("OPENAI_MODEL", "gpt-5.4-mini")
OPENAI_UTILITY_MODEL = _s("OPENAI_UTILITY_MODEL", "gpt-5.4-nano")

# Пустой DATABASE_URL -> SQLite рядом с проектом. Позволяет запустить без Docker.
DATABASE_URL = _s("DATABASE_URL") or f"sqlite+aiosqlite:///{ROOT / 'po_agent.db'}"

# На Vercel и других serverless-платфорх SQLite не работает — файловая система эфемерна.
if os.getenv("VERCEL") and not _s("DATABASE_URL"):
    raise RuntimeError(
        "Vercel detected: DATABASE_URL must be set to an external Postgres URL. "
        "SQLite is not supported on serverless platforms."
    )

AUTH_COOKIE_DAYS = _i("AUTH_COOKIE_DAYS", 30)
CONTEXT_MESSAGES_LIMIT = _i("CONTEXT_MESSAGES_LIMIT", 25)
MAX_RESPONSE_TOKENS = _i("MAX_RESPONSE_TOKENS", 600)
PROPOSAL_EXPIRY_DAYS = _i("PROPOSAL_EXPIRY_DAYS", 5)
STANDUP_COLLECTION_HOURS = _i("STANDUP_COLLECTION_HOURS", 4)
PROACTIVE_ENABLED = _b("PROACTIVE_ENABLED", True)
PROACTIVE_MAX_PER_DAY = _i("PROACTIVE_MAX_PER_DAY", 2)
QUIET_HOURS_START = _i("QUIET_HOURS_START", 22)
QUIET_HOURS_END = _i("QUIET_HOURS_END", 9)
RISK_SCAN_CRON = _s("RISK_SCAN_CRON", "0 6 * * *")
DAILY_MESSAGE_LIMIT_PER_TEAM = _i("DAILY_MESSAGE_LIMIT_PER_TEAM", 200)
ADMIN_LOGIN = _s("ADMIN_LOGIN", "admin")
ADMIN_PASSWORD = _s("ADMIN_PASSWORD", "admin")
BASE_URL = _s("BASE_URL", "http://localhost:8000")
SECRET_KEY = _s("SECRET_KEY", "dev-secret-change-me")
TZNAME = _s("TZ", "Europe/Moscow")

# Цены $/1M токенов — только для строки расходов в панели (§14).
PRICE_IN_PER_M = float(_s("PRICE_IN_PER_M", "0.25"))
PRICE_OUT_PER_M = float(_s("PRICE_OUT_PER_M", "2.00"))

BLOCKED_DAYS_THRESHOLD = _i("BLOCKED_DAYS_THRESHOLD", 3)
STALE_MUST_DAYS = _i("STALE_MUST_DAYS", 5)
