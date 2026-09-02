import html as _html

import markdown as _md
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from .config import ROOT

templates = Jinja2Templates(directory=str(ROOT / "templates"))


def md(text: str) -> Markup:
    """Экранируем ДО конвертации: сообщения пишут пользователи, сырой HTML
    из них рендерить нельзя. python-markdown сохраняет уже готовые сущности."""
    if not text:
        return Markup("")
    return Markup(_md.markdown(_html.escape(text),
                               extensions=["fenced_code", "tables", "nl2br"]))


def ru_dt(value) -> str:
    return value.strftime("%d.%m %H:%M") if value else ""


PROPOSAL_TITLES = {
    "create_user_story": "Создать User Stories",
    "create_task": "Создать задачи",
    "decompose_item": "Декомпозировать элемент",
    "update_priority": "Изменить приоритеты",
    "create_acceptance_criteria": "Добавить критерии приёмки",
    "update_requirement": "Изменить требование",
    "merge_duplicates": "Объединить дубликаты",
    "create_risk": "Зафиксировать риск",
    "assign_item": "Назначить исполнителей",
    "update_product_vision": "Обновить Product Vision",
    "sprint_plan": "План спринта",
}

EVENT_TITLES = {
    "backlog_item_created": "создан элемент бэклога",
    "backlog_item_updated": "изменён элемент",
    "backlog_item_status_changed": "изменён статус",
    "priority_changed": "изменён приоритет",
    "item_decomposed": "выполнена декомпозиция",
    "items_merged": "объединены дубликаты",
    "acceptance_criteria_created": "добавлен критерий приёмки",
    "acceptance_criteria_met": "отмечен критерий приёмки",
    "requirement_created": "создано требование",
    "requirement_changed": "изменено требование",
    "product_vision_updated": "обновлён Product Vision",
    "risk_detected": "выявлен риск",
    "risk_status_changed": "изменён статус риска",
    "dependency_detected": "найдена зависимость",
    "assignment_made": "назначен исполнитель",
    "sprint_created": "создан спринт",
    "standup_completed": "завершён стендап",
    "proposal_created": "создано предложение",
    "proposal_resolved": "решение по предложению",
}

STATUS_RU = {"new": "новая", "in_progress": "в работе", "blocked": "заблокирована",
             "done": "готова", "cancelled": "отменена"}

templates.env.filters["md"] = md
templates.env.filters["dt"] = ru_dt
templates.env.globals.update(
    PROPOSAL_TITLES=PROPOSAL_TITLES, EVENT_TITLES=EVENT_TITLES, STATUS_RU=STATUS_RU,
)
