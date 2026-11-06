"""Static, human-first report presentation; no scripts or remote resources."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from llm_test_lab.contracts import Case, Event, LabAttemptEnvelope

SOURCES = {
    "fixture": "Учебный сценарий — без нейросети",
    "recorded": "Сохранённый эксперимент — без нового запуска",
    "live-local": "Живой запуск локальной модели",
    "live-cloud": "Живой запуск облачной модели",
}
VARIANTS = {
    "learning": "Учебное правило доступа",
    "vulnerable": "Без проверки владельца",
    "prompt_only": "Запрет только в промпте",
    "enforced": "Проверка доступа в коде",
}
TERMINALS = {
    "completed": "Диалог завершён",
    "model_error": "Модель нарушила формат ответа",
    "provider_error": "Ошибка сервиса модели",
    "executor_error": "Ошибка исполнения стенда",
    "timeout": "Истекло время ожидания",
    "cancelled": "Запуск отменён",
    "limit_reached": "Достигнут лимит действий",
    "interrupted": "Процесс был прерван",
    "created": "Попытка подготовлена",
    "running": "Попытка ещё выполняется",
}
TASKS = {
    "pass": "Контракт выполнен",
    "fail": "Контракт не выполнен",
    "inconclusive": "Недостаточно данных",
    "oracle_error": "Не сработала проверка",
}
SECURITY = {
    "violation": "Нарушение обнаружено",
    "no_observed_violation": "Нарушений не обнаружено",
    "inconclusive": "Вывод о безопасности неполон",
    "oracle_error": "Проверка доступа не завершена",
}
INTEGRITY = {
    "complete": "Материалы полные",
    "partial": "Материалы неполные",
    "invalid": "Целостность нарушена",
}
GROUPS = {
    "normal_benign_usefulness": "Обычные и безвредные задачи",
    "attack_usefulness": "Полезная задача под атакой",
    "direct_forbidden_request": "Прямой запрос чужих данных",
    "fault_recovery": "Обработка сбоев",
    "unclassified": "Назначение не классифицировано",
}
FIELDS = {
    "title": "Название",
    "status": "Статус",
    "priority": "Приоритет",
    "revision": "Версия записи",
    "ownerId": "Владелец",
    "note": "Заметка",
    "id": "ID",
}

CSS = """
:root{color-scheme:dark;--bg:#0b1119;--panel:#121d2b;--line:#26364a;--text:#e9eef7;--muted:#a6b7cc;--accent:#80d9cb;--good:#78d7b5;--bad:#ff9a9e;--warn:#f1ca80}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.65 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}a{color:#9fc9ff;text-underline-offset:.22em}a:hover{color:#d6e8ff}a:focus-visible,summary:focus-visible{outline:3px solid var(--accent);outline-offset:5px;border-radius:3px}.shell{max-width:1180px;margin:auto;padding:32px 28px 72px}.topbar{display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line);padding-bottom:20px;margin-bottom:36px}.brand{font-weight:750;letter-spacing:-.04em;font-size:20px}.brand span{color:var(--accent)}.eyebrow,.kicker{font-size:12px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}.hero{max-width:900px;margin-bottom:28px}h1{font-size:clamp(28px,4vw,43px);line-height:1.16;letter-spacing:-.045em;margin:18px 0 16px}h2{font-size:24px;letter-spacing:-.025em;line-height:1.3;margin:0 0 12px}h1,h2,h3,p,summary,.step-title{overflow-wrap:anywhere}h3{font-size:17px;line-height:1.4;margin:0 0 10px}p{margin:0 0 14px}.lead{font-size:19px;color:#c4d1e2;max-width:850px}.muted,.help{color:var(--muted);font-size:14px}.badge{display:inline-block;padding:5px 11px;border:1px solid #326458;border-radius:99px;background:#15332f;color:#a5e6d9;font-size:13px;font-weight:650}.badge.neutral{border-color:var(--line);background:#182536;color:var(--muted)}.chips{display:flex;gap:8px;flex-wrap:wrap}.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:24px 0}.card{padding:18px;background:var(--panel);border:1px solid var(--line);border-radius:12px;border-top:3px solid #526881;min-width:0}.card.good{border-top-color:var(--good)}.card.bad{border-top-color:var(--bad)}.card.warn{border-top-color:var(--warn)}.card-label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.07em;font-weight:700}.card-value{font-size:20px;line-height:1.3;font-weight:700;margin:9px 0}.card-number{font-size:34px}.card .help{font-size:12px;line-height:1.5;margin:0}.section{margin:34px 0}.panel{padding:22px;border:1px solid var(--line);background:var(--panel);border-radius:12px}.notice{padding:15px 18px;border-left:3px solid var(--warn);background:#29251c;color:#f3dfb5;border-radius:0 8px 8px 0;margin:18px 0}.notice.bad{background:#2b1c25;border-color:var(--bad);color:#ffd0d1}.notice.good{background:#142c29;border-color:var(--good);color:#b8e9d7}.split{display:grid;grid-template-columns:1fr 1fr;gap:18px}.table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:12px}table{border-collapse:collapse;width:100%;text-align:left;font-size:14px}th{font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);background:#152131}th,td{padding:14px 16px;border-bottom:1px solid var(--line);vertical-align:top}tr:last-child td{border-bottom:0}td{overflow-wrap:anywhere}tbody tr:hover{background:#132031}.case-cell{min-width:240px;max-width:420px}.case-title{display:block;font-weight:700;margin-bottom:4px}.small{font-size:12px;color:var(--muted)}.status{font-weight:650;font-size:13px;white-space:normal}.status.good{color:var(--good)}.status.bad{color:var(--bad)}.status.warn{color:var(--warn)}code{font:12px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;color:#bad0ea;overflow-wrap:anywhere}pre{margin:14px 0 0;padding:16px;background:#091019;border:1px solid var(--line);border-radius:8px;white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.65 ui-monospace,SFMono-Regular,Consolas,monospace;color:#b9cee6;max-height:520px;overflow:auto}details{margin:12px 0;border:1px solid var(--line);border-radius:9px;padding:13px 16px;background:#111b28}summary{cursor:pointer;font-size:14px;color:#c1d2e8;font-weight:600}details[open]>summary{margin-bottom:12px}blockquote{margin:0;padding:14px 18px;border-left:3px solid #59738f;color:#d9e4f2;background:#0e1825;white-space:pre-wrap;overflow-wrap:anywhere}.timeline{list-style:none;padding:0;margin:18px 0}.step{position:relative;display:grid;grid-template-columns:45px 1fr;gap:12px;margin-bottom:14px}.step-number{font:12px/28px ui-monospace,monospace;width:36px;height:30px;text-align:center;border:1px solid var(--line);border-radius:7px;color:var(--accent);margin-top:2px}.step-body{border-bottom:1px solid var(--line);padding-bottom:14px;min-width:0}.step-title{font-weight:650;margin-bottom:3px}.step-body details{margin:9px 0 0;padding:9px 12px}.facts{padding-left:20px}.facts li{margin-bottom:6px}.empty{padding:18px;color:var(--muted)}.evidence-nav{display:flex;gap:10px 18px;flex-wrap:wrap;padding:14px 0}.footer{border-top:1px solid var(--line);padding-top:20px;margin-top:38px;font-size:12px;color:var(--muted)}.chart{width:100%;height:auto;display:block;border-radius:8px}.value{max-width:330px;white-space:pre-wrap;overflow-wrap:anywhere}.arrow{color:var(--accent)}@media(max-width:850px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}.split{grid-template-columns:1fr}.shell{padding:22px 18px 50px}.topbar{margin-bottom:26px}}@media(max-width:480px){.cards{grid-template-columns:1fr}.card{padding:15px}.card-value{font-size:19px}.topbar .eyebrow{display:none}th,td{padding:12px}.panel{padding:16px}}
"""


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _md_text(value: object) -> str:
    text = esc(value).replace("\r", " ").replace("\n", " ")
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", text)


def _visible_security(envelope: LabAttemptEnvelope) -> str:
    if (
        envelope.evidence_integrity != "complete"
        and envelope.checks_summary.security == "no_observed_violation"
    ):
        return "Безопасный исход не подтверждён"
    return SECURITY[envelope.checks_summary.security]


def _pre(value: object) -> str:
    return (
        "<pre>"
        + html.escape(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
        + "</pre>"
    )


def details(label: str, content: str) -> str:
    return "<details><summary>" + esc(label) + "</summary>" + content + "</details>"


def _document(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; base-uri 'none'; form-action 'none'; object-src 'none'\">"
        "<title>"
        + esc(title)
        + "</title><style>"
        + CSS
        + '</style></head><body><main class="shell">'
        '<header class="topbar"><div class="brand">LLM <span>Test Lab</span></div><div class="eyebrow">Результат → доказательства → вывод</div></header>'
        + body
        + '<footer class="footer">Локальный статический отчёт. Без JavaScript, внешних шрифтов и запросов к модели. '
        "Наблюдение в этой лаборатории не является гарантией безопасности другой системы.</footer></main></body></html>"
    )


def safe_attempt_id(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", value) is not None and value not in {
        ".",
        "..",
    }


def evidence_href(envelope: LabAttemptEnvelope, *, html_report: bool = True) -> str | None:
    if not safe_attempt_id(envelope.attempt_id):
        return None
    prefix = f"attempts/{envelope.attempt_id}/"
    allowed = {prefix + "report.md", prefix + "recovered/report.md"}
    if envelope.evidence_ref not in allowed:
        return None
    return envelope.evidence_ref[:-3] + ".html" if html_report else envelope.evidence_ref


def _state_changes(
    before: list[dict[str, JsonValue]], after: list[dict[str, JsonValue]]
) -> list[tuple[str, str, JsonValue, JsonValue]]:
    initial = {str(ticket["id"]): ticket for ticket in before}
    final = {str(ticket["id"]): ticket for ticket in after}
    changes = []
    for ticket_id in sorted(set(initial) | set(final)):
        left, right = initial.get(ticket_id), final.get(ticket_id)
        if left is None or right is None:
            changes.append(
                (ticket_id, "запись целиком", cast(JsonValue, left), cast(JsonValue, right))
            )
            continue
        for field in sorted(
            set(left) | set(right),
            key=lambda key: (key not in {"status", "priority", "revision"}, key),
        ):
            if left.get(field) != right.get(field):
                changes.append((ticket_id, field, left.get(field), right.get(field)))
    return changes


def _value(value: JsonValue) -> str:
    if value is None:
        return "нет значения"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _display_value(value: JsonValue) -> str:
    text = _value(value)
    if len(text) > 180:
        return esc(text[:160] + "…") + details(
            "Полное значение", '<div class="value">' + esc(text) + "</div>"
        )
    return esc(text)


def _tone(status: str) -> str:
    return (
        "bad"
        if status in {"fail", "violation", "invalid", "oracle_error"}
        else "good"
        if status in {"pass", "no_observed_violation", "complete"}
        else "warn"
    )


def _card(label: str, value: str, description: str, tone: str = "", *, number: bool = False) -> str:
    return (
        '<article class="card '
        + tone
        + '"><div class="card-label">'
        + esc(label)
        + '</div><div class="card-value'
        + (" card-number" if number else "")
        + '">'
        + esc(value)
        + '</div><p class="help">'
        + esc(description)
        + "</p></article>"
    )


def _security_detail(envelope: LabAttemptEnvelope) -> dict[str, JsonValue]:
    return next(
        (
            detail
            for detail in envelope.checks_summary.details
            if detail.get("check") == "security_boundary"
        ),
        {},
    )


def _actual(envelope: LabAttemptEnvelope, events: list[Event]) -> list[str]:
    commits = [event for event in events if event.type == "commit"]
    lines = [
        f"Сохранённых изменений агентом: {len(commits)}."
        if commits
        else "Агент не сохранил ни одного изменения по доступному журналу."
    ]
    read_ids = _security_detail(envelope).get("toolPayloadReturned")
    if isinstance(read_ids, list) and read_ids:
        lines.append(
            "Инструмент вернул чужие данные: " + ", ".join(str(value) for value in read_ids) + "."
        )
    denials = sum(event.type == "access_denied" for event in events)
    if denials:
        lines.append(f"Проверка доступа заблокировала обращений: {denials}.")
    replays = sum(event.type == "replay" for event in events)
    if replays:
        lines.append(
            f"Повтор команды распознан: {replays}; сохранённый результат возвращён без нового изменения."
        )
    if any(
        event.type == "fault" and event.payload.get("fault") == "lost_response" for event in events
    ):
        lines.append(
            "Подтверждение записи было намеренно потеряно. Итог проверен по базе, а не по ответу инструмента."
        )
    if any(
        event.type == "fault" and event.payload.get("fault") == "save_failed" for event in events
    ):
        lines.append(
            "Учебный сбой остановил запись до сохранения. Это заданное условие сценария, не сбой установки."
        )
    if any(event.type == "fixture_intervention" for event in events):
        lines.append("Учебная среда внесла отдельную правку. Она не считается действием агента.")
    if envelope.evidence_integrity != "complete":
        lines.append(
            "Материалы неполные или повреждены: отсутствие события не доказывает, что его не было."
        )
    return lines


def _event_text(event: Event) -> tuple[str, str]:
    payload = event.payload
    arguments = payload.get("arguments")
    ticket = arguments.get("ticketId") if isinstance(arguments, dict) else payload.get("ticketId")
    target = str(ticket) if ticket is not None else "без ID"
    tool = str(payload.get("tool", "инструмент"))
    labels = {
        "user_turn": ("Пользователь поставил задачу", str(payload.get("user", ""))),
        "tool_attempt": (
            f"Запрошен {tool}: {target}",
            "Это попытка действия. Сама по себе она ещё ничего не прочитала и не изменила.",
        ),
        "read": (
            f"Инструмент прочитал {target}",
            "В журнале сохранена фактически возвращённая запись.",
        ),
        "access_denied": (
            f"Доступ к {target} запрещён кодом",
            "Серверная проверка владельца остановила обращение. Это не просто отказ модели.",
        ),
        "commit": (
            f"Изменение {target} сохранено",
            "Транзакция записи завершена; действие отражено в базе и журнале.",
        ),
        "noop": (
            f"Новая запись не потребовалась: {target}",
            "Значения уже совпадали. Команда запомнена, версия записи не увеличилась.",
        ),
        "replay": (
            f"Повтор команды для {target} распознан",
            "Возвращён прежний результат, второе изменение не выполнялось.",
        ),
        "version_conflict": (
            f"Устаревшая версия {target} отклонена",
            "Данные изменились после чтения; перезапись по старой версии запрещена.",
        ),
        "command_conflict": (
            "Повтор с другим содержимым отклонён",
            "Один пользовательский ход не может незаметно превратиться в другую команду записи.",
        ),
        "invalid_arguments": (
            "Аргументы инструмента отклонены",
            "Неверный формат или неизвестное имя инструмента не исполняются.",
        ),
        "fixture_intervention": (
            f"Среда изменила {target}",
            "Контролируемая внешняя правка учебного сценария, не действие агента.",
        ),
        "model_dispatch": (
            "Запрос отправлен модели",
            "Зафиксирована отправка, но не доказана внутренняя обработка на стороне сервиса.",
        ),
        "model_response": (
            "Получен ответ модели",
            "Следующие события покажут, был ли формат принят и исполнены ли вызовы инструментов.",
        ),
        "turn_complete": (
            "Агент завершил пользовательский ход",
            "Ответ не заменяет проверку фактического состояния базы.",
        ),
        "terminal": (
            TERMINALS.get(str(payload.get("state")), "Попытка остановлена"),
            str(payload.get("state", "")),
        ),
    }
    if event.type == "fault":
        fault = payload.get("fault")
        return (
            (
                "Учебный сбой: ответ о записи потерян",
                "Запись могла состояться. Нужны чтение состояния и защита от дублирования.",
            )
            if fault == "lost_response"
            else (
                "Учебный сбой: запись не сохранена",
                "Ошибка возникла до commit; проверяем отсутствие изменения и ложного подтверждения.",
            )
        )
    return labels.get(event.type, (event.type, "Техническое событие; подробности сохранены ниже."))


def _diff_table(
    before: list[dict[str, JsonValue]], after: list[dict[str, JsonValue]], events: list[Event]
) -> str:
    changes = _state_changes(before, after)
    if not changes:
        return '<div class="panel"><p>Состояние базы не изменилось.</p><p class="help">Это не доказывает безопасность чтения: чужие данные могли быть возвращены без единой записи.</p></div>'
    rows = []
    for ticket, field, old, new in changes:
        links = []
        for event in events:
            if (
                event.type in {"commit", "fixture_intervention"}
                and event.payload.get("ticketId") == ticket
            ):
                patch = event.payload.get("patch")
                if field == "revision" or (isinstance(patch, dict) and field in patch):
                    source = "агент" if event.type == "commit" else "учебная среда"
                    links.append(
                        f'<a href="#event-{event.sequence}">#{event.sequence} · {source}</a>'
                    )
        rows.append(
            "<tr><td><code>"
            + esc(ticket)
            + "</code></td><td>"
            + esc(FIELDS.get(field, field))
            + '<div class="small">'
            + esc(field)
            + '</div></td><td class="value">'
            + _display_value(old)
            + '</td><td class="value"><span class="arrow">→ </span>'
            + _display_value(new)
            + "</td><td>"
            + ("<br>".join(links) or "Источник не установлен")
            + "</td></tr>"
        )
    return (
        '<div class="table-wrap"><table><thead><tr><th>Заявка</th><th>Поле</th><th>Было</th><th>Стало</th><th>Подтверждение</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></div>"
    )


def render_attempt_html(
    path: Path,
    envelope: LabAttemptEnvelope,
    case: Case,
    before: list[dict[str, JsonValue]],
    after: list[dict[str, JsonValue]],
    events: list[Event],
) -> Path:
    check = envelope.checks_summary
    source = SOURCES[envelope.source_mode]
    body = (
        '<section class="hero"><span class="badge">'
        + esc(source)
        + "</span><h1>"
        + esc(case.goal)
        + '</h1><p class="lead">Сначала — что произошло на самом деле. Потом — что сообщил агент и чем подтверждён вывод.</p><div class="chips"><span class="badge neutral">'
        + esc(case.id + " · " + VARIANTS[envelope.variant])
        + '</span><span class="badge neutral">'
        + esc(TERMINALS[envelope.terminal_state])
        + "</span></div></section>"
    )
    task_label = TASKS[check.task]
    task_help = "Выполнены ожидания этого сценария. Правильным исходом может быть запись, отказ или уточнение."
    if check.task != "pass":
        task_help = (
            "Ожидания сценария не выполнены."
            if check.task == "fail"
            else "Завершить проверку задачи не удалось. Это не доказанный успех или безопасный отказ."
        )
    if envelope.evidence_integrity == "invalid":
        task_label = "Оценку нельзя подтвердить"
        task_help = (
            "Сохранённый task=" + check.task + "; текущие материалы не прошли проверку целостности."
        )
    claim_labels = {
        "pass": "Слова совпали с фактами",
        "fail": "Слова расходятся с фактами",
        "not_assessed": "Утверждения не оценены",
        "oracle_error": "Проверка утверждений не завершена",
    }
    body += '<section class="cards" aria-label="Основные результаты">'
    body += _card(
        "Задача",
        task_label,
        task_help,
        _tone(check.task) if envelope.evidence_integrity == "complete" else "warn",
    )
    body += _card(
        "Граница доступа",
        _visible_security(envelope),
        "Нарушение не исчезает после отказа, таймаута или отката."
        if check.security == "violation"
        else "Вывод относится только к проверенной границе владельцев и доступным материалам.",
        _tone(check.security)
        if envelope.evidence_integrity == "complete" or check.security == "violation"
        else "warn",
    )
    body += _card(
        "Слова и действия",
        "Оценку нельзя подтвердить"
        if envelope.evidence_integrity == "invalid"
        else claim_labels[check.claim_consistency],
        "Проверены структурированные поля ответа. Свободный текст не прошёл полную проверку истинности.",
        _tone(check.claim_consistency) if envelope.evidence_integrity == "complete" else "warn",
    )
    body += (
        _card(
            "Доказательства",
            INTEGRITY[envelope.evidence_integrity],
            f"{TERMINALS[envelope.terminal_state]} · {envelope.terminal_state} / {envelope.evidence_integrity}",
            _tone(envelope.evidence_integrity),
        )
        + "</section>"
    )
    if envelope.evidence_integrity != "complete":
        body += (
            '<aside class="notice bad">'
            + esc(
                "Внимание: материалы повреждены. Положительные выводы нельзя считать подтверждёнными."
                if envelope.evidence_integrity == "invalid"
                else "Внимание: попытка не дала полного набора наблюдений. Уже обнаруженное нарушение сохраняется; отсутствие других событий не означает безопасность."
            )
            + "</aside>"
        )
    if envelope.source_mode == "fixture":
        body += '<aside class="notice">Ответы агента здесь заданы заранее. Мы проверяем инструменты, защиту и измеритель, а не устойчивость нейросети. Красный результат безопасности может быть целью учебного примера; сам по себе он не означает сломанную установку. Совпадение с эталоном проверяет отдельный <code>fixture-contract</code>.</aside>'
    body += (
        '<section class="section panel"><h2>Что произошло на самом деле</h2><ul class="facts">'
        + "".join("<li>" + esc(line) + "</li>" for line in _actual(envelope, events))
        + '</ul><a href="#state-diff">Посмотреть изменения в базе</a> · <a href="#timeline">Разобрать действия по порядку</a></section>'
    )
    body += (
        '<section class="section" id="state-diff"><h2>Что изменилось в базе</h2><p class="help">Сравнение отдельных чтений состояния до и после попытки. История действий ниже сохраняет и изменения, которые затем были отменены.</p>'
        + _diff_table(before, after, events)
        + "</section>"
    )
    answer = envelope.final_answer
    body += '<section class="section"><h2>Что сказал агент — и что проверил стенд</h2><div class="split"><article class="panel"><h3>Ответ агента</h3>'
    if answer is None:
        body += '<p class="muted">Валидный итоговый ответ не получен.</p>'
    else:
        outcome_labels = {
            "completed": "Сообщил о завершении",
            "refused": "Отказался выполнять просьбу",
            "failed": "Сообщил, что задача не выполнена",
            "needs_clarification": "Попросил уточнение",
        }
        body += (
            "<p><strong>"
            + outcome_labels[answer.outcome]
            + "</strong> <code>"
            + answer.outcome
            + "</code></p><blockquote>"
            + esc(answer.message)
            + "</blockquote>"
        )
        if answer.claimed_changes:
            body += (
                '<p class="help">Заявленные изменения: '
                + esc(
                    ", ".join(
                        f"{field} = {_value(value)}"
                        for field, value in answer.claimed_changes.items()
                    )
                )
                + "</p>"
            )
        else:
            body += '<p class="help">Структурированных утверждений об изменениях нет.</p>'
    body += (
        '</article><article class="panel"><h3>Независимая проверка</h3><p class="status '
        + (_tone(check.claim_consistency) if envelope.evidence_integrity == "complete" else "warn")
        + '">'
        + (
            "Оценку нельзя подтвердить"
            if envelope.evidence_integrity == "invalid"
            else claim_labels[check.claim_consistency]
        )
        + '</p><p>Ответ «готово» не является доказательством записи. Проверка сопоставляет структурированные факты и заявленные изменения с базой и журналом действий.</p><p class="help">Отказ модели без вызова инструмента не проверяет серверную защиту. Для неё нужен фактический запрещённый вызов и событие отказа.</p></article></div></section>'
    )
    visible = {
        "user_turn",
        "tool_attempt",
        "read",
        "access_denied",
        "commit",
        "noop",
        "replay",
        "version_conflict",
        "command_conflict",
        "invalid_arguments",
        "fixture_intervention",
        "model_dispatch",
        "model_response",
        "turn_complete",
        "fault",
        "terminal",
    }
    body += '<section class="section" id="timeline"><h2>Действия по порядку</h2><p class="help">Попытка вызвать инструмент ≠ разрешённое действие ≠ сохранённое изменение. Номер связывает шаг с исходным событием.</p><ol class="timeline">'
    for event in events:
        if event.type not in visible:
            continue
        title, explanation = _event_text(event)
        body += (
            f'<li class="step" id="event-{event.sequence}"><span class="step-number">{event.sequence:02d}</span><div class="step-body"><div class="step-title">'
            + esc(title)
            + '</div><div class="help">'
            + esc(explanation)
            + "</div>"
            + details(f"Событие #{event.sequence} · {event.type}", _pre(event.to_dict()))
            + "</div></li>"
        )
    body += '</ol></section><section class="section"><h2>Доказательства для разбора</h2><p class="help">Исходные данные раскрываются по необходимости. Они отображаются как текст и не исполняются.</p>'
    body += details("Проверки и точные статусы", _pre(envelope.checks_summary.to_dict()))
    body += details("Структурированный ответ агента", _pre(answer.to_dict() if answer else None))
    body += details(
        f"Полная трасса — {len(events)} событий", _pre([event.to_dict() for event in events])
    )
    body += details(
        "Полное состояние до и после", "<h3>До</h3>" + _pre(before) + "<h3>После</h3>" + _pre(after)
    )
    body += '<nav class="evidence-nav" aria-label="Исходные материалы"><a href="manifest.json">Описание прогона</a><a href="events.jsonl">Журнал событий</a><a href="conversation.json">Диалог</a><a href="checks.json">Проверки</a><a href="state.sqlite">База SQLite</a></nav></section>'
    output = path / "report.html"
    output.write_text(_document(case.id + " — " + case.goal, body), encoding="utf-8")
    return output


def render_attempt_report(
    path: Path,
    envelope: LabAttemptEnvelope,
    case: Case,
    before: list[dict[str, JsonValue]],
    after: list[dict[str, JsonValue]],
    events: list[Event],
) -> Path:
    render_attempt_html(path, envelope, case, before, after, events)
    check = envelope.checks_summary
    lines = [
        "# " + _md_text(case.id) + " · " + _md_text(case.goal),
        "",
        "**" + SOURCES[envelope.source_mode] + "**",
        "",
        "Защита: "
        + VARIANTS[envelope.variant]
        + ". Итог: "
        + TERMINALS[envelope.terminal_state]
        + f" (`{envelope.terminal_state}`).",
        "",
        "| Проверка | Результат |",
        "| --- | --- |",
        "| Контракт задачи | "
        + (
            "Оценка не подтверждена"
            if envelope.evidence_integrity == "invalid"
            else TASKS[check.task]
        )
        + f" (`{check.task}`) |",
        "| Граница доступа | " + _visible_security(envelope) + f" (`{check.security}`) |",
        "| Полнота материалов | "
        + INTEGRITY[envelope.evidence_integrity]
        + f" (`{envelope.evidence_integrity}`) |",
        "",
        "## Что произошло на самом деле",
        "",
        *["- " + _md_text(line) for line in _actual(envelope, events)],
        "",
        "Учебный сценарий проверяет стенд, не нейросеть. Красный результат может быть ожидаемой демонстрацией; это проверяется отдельно через `fixture-contract`."
        if envelope.source_mode == "fixture"
        else "Результат относится только к этому сценарию, модели и конфигурации.",
        "",
        "## Что изменилось в базе",
        "",
        _diff_table(before, after, events),
        "",
        "## Действия по порядку",
        "",
    ]
    for event in events:
        if event.type in {
            "user_turn",
            "tool_attempt",
            "read",
            "access_denied",
            "commit",
            "noop",
            "replay",
            "version_conflict",
            "command_conflict",
            "fault",
            "fixture_intervention",
            "turn_complete",
            "terminal",
        }:
            title, explanation = _event_text(event)
            lines.extend(
                [
                    f'<a id="event-{event.sequence}"></a>',
                    f"**#{event.sequence} · {_md_text(title)}** — {_md_text(explanation)}",
                    details("Исходное событие", _pre(event.to_dict())),
                    "",
                ]
            )
    lines.extend(
        [
            "## Что сообщил агент",
            details(
                "Структурированный ответ",
                _pre(envelope.final_answer.to_dict() if envelope.final_answer else None),
            ),
            "",
            "Слова не заменяют проверку записи. Свободный текст не объявляется полностью проверенным.",
            "",
            "## Исходные материалы",
            details("Все проверки и статусы", _pre(check.to_dict())),
            details("Полная трасса", _pre([e.to_dict() for e in events])),
            details("Состояние до и после", _pre({"before": before, "after": after})),
            "",
            "[Открыть удобный HTML-отчёт](report.html) · [Журнал](events.jsonl) · [Диалог](conversation.json) · [SQLite](state.sqlite) · [Описание прогона](manifest.json)",
            "",
        ]
    )
    output = path / "report.md"
    output.write_text("\n".join(lines), encoding="utf-8")
    return output


def _count(summary: dict[str, JsonValue], key: str) -> str:
    value = summary.get(key)
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else "неизвестно"


def _gate(summary: dict[str, JsonValue]) -> str:
    result = summary.get("gateResult")
    if not isinstance(result, dict):
        return '<aside class="notice">Итог проверки стенда здесь не указан. Не считайте красную ячейку автоматически ошибкой установки: учебный режим может намеренно показывать нарушение или таймаут.</aside>'
    gate, code = result.get("gate"), result.get("exitCode")
    if gate == "fixture-contract" and type(code) is int and code == 0:
        title = "Стенд отработал по учебному эталону"
        explanation = "Результаты совпали с ожидаемыми, включая намеренные нарушения и сбои. Это проверка лаборатории, а не сертификат безопасности агента."
        tone = "good"
    elif type(code) is int and code == 0:
        title = "Эксперимент выполнен и сохранён"
        explanation = "Это не означает, что модель выполнила задачу или выдержала атаку. Результаты каждого сценария — в таблице ниже."
        tone = "good"
    else:
        title = "Прогон требует внимания"
        explanation = "Проверка стенда не пройдена, запуск отменён или данные неполны. Сохранённые нарушения остаются видимыми; причину смотрите в техническом итоге."
        tone = "bad"
    return (
        '<aside class="notice '
        + tone
        + '"><strong>'
        + title
        + "</strong><br>"
        + explanation
        + details("Технический итог запуска", _pre(result))
        + "</aside>"
    )


def _metrics(summary: dict[str, JsonValue]) -> str:
    configs = summary.get("configurationGroups")
    rows = []
    if isinstance(configs, list):
        for configuration in configs:
            if not isinstance(configuration, dict):
                continue
            task_groups = configuration.get("taskGroups")
            if not isinstance(task_groups, list):
                continue
            for group in task_groups:
                if not isinstance(group, dict) or not group.get("started"):
                    continue
                metric = group.get("taskPass")
                if not isinstance(metric, dict):
                    continue
                denominator = metric.get("denominator")
                observed = (
                    f"{metric.get('numerator')} из {denominator}"
                    if isinstance(denominator, int) and denominator > 0
                    else "Нет оценённых попыток"
                )
                source = str(configuration.get("sourceMode", "неизвестно"))
                variant = str(configuration.get("variant", "неизвестно"))
                name = str(group.get("group", "unclassified"))
                rows.append(
                    "<tr><td>"
                    + esc(GROUPS.get(name, name))
                    + "</td><td>"
                    + esc(SOURCES.get(source, source))
                    + '<br><span class="small">'
                    + esc(VARIANTS.get(variant, variant))
                    + " · "
                    + esc(configuration.get("profileId") or "без профиля модели")
                    + "</span></td><td>"
                    + esc(observed)
                    + "</td><td>"
                    + esc(group.get("excluded", "неизвестно"))
                    + "</td></tr>"
                )
    if not rows:
        return '<p class="muted">Сопоставимых оценённых групп нет. Отсутствующие данные не превращаются в нулевой или стопроцентный результат.</p>'
    return (
        '<div class="table-wrap"><table><thead><tr><th>Что проверяем</th><th>Источник и защита</th><th>Контракт выполнен</th><th>Не вошли в оценку</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></div>"
    )


def run_presentation(
    envelopes: list[LabAttemptEnvelope], cases: dict[str, Case], summary: dict[str, JsonValue]
) -> tuple[str, str]:
    modes = sorted({envelope.source_mode for envelope in envelopes})
    source_labels = "".join(
        '<span class="badge">' + esc(SOURCES[mode]) + "</span>" for mode in modes
    )
    body = (
        '<section class="hero"><div class="chips">'
        + (source_labels or '<span class="badge neutral">Нет завершённых материалов попыток</span>')
        + '</div><h1>Результаты эксперимента</h1><p class="lead">Каждая строка — отдельный сценарий. Откройте его, чтобы увидеть задачу, реальные изменения и причину вывода.</p></section>'
    )
    violations = sum(envelope.checks_summary.security == "violation" for envelope in envelopes)
    incomplete = sum(envelope.evidence_integrity != "complete" for envelope in envelopes)
    body += '<section class="cards" aria-label="Сводка запуска">'
    body += _card(
        "Запланировано",
        _count(summary, "planned"),
        "Начато: "
        + _count(summary, "started")
        + ". Не запускались: "
        + _count(summary, "notRun")
        + ".",
        number=True,
    )
    body += _card(
        "Дошли до конца",
        _count(summary, "completed"),
        "Диалог завершён. Это ещё не означает успех задачи или защиту от атаки.",
        number=True,
    )
    body += _card(
        "Нарушений обнаружено",
        str(violations),
        "Число попыток с нарушением границы доступа; не число всех событий.",
        "bad" if violations else "",
        number=True,
    )
    body += (
        _card(
            "Технически не завершены",
            _count(summary, "technicallyIncomplete"),
            f"Неполных или повреждённых наборов материалов: {incomplete}. Неизвестное не считается безопасным.",
            "warn" if incomplete or summary.get("technicallyIncomplete") else "",
            number=True,
        )
        + "</section>"
    )
    if any(envelope.evidence_integrity == "invalid" for envelope in envelopes):
        body += '<aside class="notice bad"><strong>Есть повреждённые материалы.</strong> Положительные оценки таких попыток нельзя подтвердить. Сохранённые выводы о нарушениях не скрыты.</aside>'
    body += _gate(summary)
    body += '<section class="section" id="attempts"><h2>Сценарии и результаты</h2><p class="help">«Контракт выполнен» означает, что получен ожидаемый исход сценария. Например, корректный отказ в чужом доступе — полезный результат.</p><div class="table-wrap"><table><thead><tr><th>Сценарий</th><th>Защита</th><th>Задача</th><th>Доступ</th><th>Материалы</th></tr></thead><tbody>'
    markdown_rows = []
    for envelope in envelopes:
        case = cases.get(envelope.attempt_id)
        goal = case.goal if case else envelope.case_id
        href = evidence_href(envelope)
        label = esc(envelope.case_id + " · " + goal)
        link = (
            '<a class="case-title" href="' + esc(href) + '">' + label + " →</a>"
            if href
            else '<span class="case-title">'
            + label
            + '</span><span class="status warn">Ссылка на материалы недоступна</span>'
        )
        task = (
            TASKS[envelope.checks_summary.task]
            if envelope.evidence_integrity != "invalid"
            else "Оценка не подтверждена"
        )
        body += (
            '<tr><td class="case-cell">'
            + link
            + '<div class="small">'
            + esc(SOURCES[envelope.source_mode])
            + "<br>"
            + esc(TERMINALS[envelope.terminal_state])
            + " · <code>"
            + envelope.terminal_state
            + "</code></div></td><td>"
            + VARIANTS[envelope.variant]
            + '<div class="small"><code>'
            + envelope.variant
            + '</code></div></td><td><span class="status '
            + (
                _tone(envelope.checks_summary.task)
                if envelope.evidence_integrity == "complete"
                else "warn"
            )
            + '">'
            + task
            + '</span></td><td><span class="status '
            + (
                _tone(envelope.checks_summary.security)
                if envelope.evidence_integrity == "complete"
                or envelope.checks_summary.security == "violation"
                else "warn"
            )
            + '">'
            + _visible_security(envelope)
            + '</span></td><td><span class="status '
            + _tone(envelope.evidence_integrity)
            + '">'
            + INTEGRITY[envelope.evidence_integrity]
            + "</span></td></tr>"
        )
        markdown_rows.append(
            "| "
            + (
                "[" + _md_text(envelope.case_id) + "](" + href + ")"
                if href
                else _md_text(envelope.case_id)
            )
            + " | "
            + VARIANTS[envelope.variant]
            + " | "
            + task
            + " | "
            + _visible_security(envelope)
            + " | "
            + INTEGRITY[envelope.evidence_integrity]
            + " |"
        )
    if not envelopes:
        body += '<tr><td colspan="5" class="empty">Пока нет материалов, из которых можно восстановить результат. Смотрите количество начатых и не запущенных попыток выше.</td></tr>'
    body += '</tbody></table></div></section><section class="section panel"><h2>Как читать этот результат</h2><div class="split"><div><h3>Отделяйте задачу от защиты</h3><p>Агент может выполнить просьбу и одновременно раскрыть чужие данные. Поэтому качество задачи и граница доступа — разные колонки.</p></div><div><h3>Отделяйте проверку стенда от проверки модели</h3><p>В учебном режиме ответы заданы заранее. Ожидаемое нарушение доказывает работу измерителя, а не устойчивость нейросети.</p></div></div></section>'
    body += (
        '<section class="section"><h2>Сравнимые группы</h2><p class="help">Обычные задачи, работа под атакой, запрет чужого доступа и обработка сбоев не смешиваются в общий «балл ИИ». В знаменателе только полноценно оценённые попытки; незавершённые показаны отдельно.</p>'
        + _metrics(summary)
        + "</section>"
    )
    body += (
        '<section class="section"><h2>Технические материалы</h2>'
        + details(
            "График доступных оценок",
            '<img class="chart" src="outcomes.svg" alt="Распределение доступных оценок по источникам">',
        )
        + details("Машинная сводка — JSON", _pre(summary))
        + '<nav class="evidence-nav"><a href="summary.json">Сводка JSON</a><a href="manifest.json">Описание запуска</a><a href="report.md">Markdown-отчёт</a></nav></section>'
    )
    markdown = "\n".join(
        [
            "# Результаты эксперимента",
            "",
            " · ".join(SOURCES[mode] for mode in modes),
            "",
            "Запланировано: "
            + _count(summary, "planned")
            + "; начато: "
            + _count(summary, "started")
            + "; дошли до конца: "
            + _count(summary, "completed")
            + "; не запускались: "
            + _count(summary, "notRun")
            + ".",
            "",
            f"Нарушение обнаружено в {violations} попытках. Технически незавершённых: "
            + _count(summary, "technicallyIncomplete")
            + ".",
            "",
            _gate(summary),
            "",
            "## Сценарии и результаты",
            "",
            "| Сценарий | Защита | Задача | Доступ | Материалы |",
            "| --- | --- | --- | --- | --- |",
            *markdown_rows,
            "",
            "Контракт задачи и граница доступа — разные измерения. Таймаут не отменяет уже обнаруженное нарушение. Учебный сценарий проверяет стенд, не модель.",
            "",
            "## Сравнимые группы",
            _metrics(summary),
            "",
            details("Машинная сводка", _pre(summary)),
            "",
            "[Открыть удобный HTML-отчёт](report.html) · [Сводка JSON](summary.json)",
            "",
        ]
    )
    return body, markdown


def comparison_presentation(payload: dict[str, JsonValue]) -> str:
    body = '<section class="hero"><span class="badge">Сохранённые результаты — без новых запросов к модели</span><h1>Сравнение экспериментов</h1><p class="lead">Сравниваем одинаковые группы задач, не собираем разные условия в один рейтинг моделей.</p></section><aside class="notice">Если одновременно менялись модель, промпт и защита, это сравнение конфигураций. По нему нельзя приписать разницу одной причине.</aside>'
    records = payload.get("runs")
    if isinstance(records, list):
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("summary"), dict):
                continue
            summary = cast(dict[str, JsonValue], record["summary"])
            body += (
                '<section class="section"><h2>'
                + esc(record.get("runId", "Эксперимент"))
                + '</h2><p class="help">Начато: '
                + _count(summary, "started")
                + "; не запускались: "
                + _count(summary, "notRun")
                + "; технически не завершены: "
                + _count(summary, "technicallyIncomplete")
                + ".</p>"
                + _metrics(summary)
                + "</section>"
            )
    body += (
        '<section class="section"><h2>График и исходные числа</h2>'
        + details(
            "Открыть график сравнения",
            '<img class="chart" src="comparison.svg" alt="Сравнение по источнику, конфигурации и назначению задачи">',
        )
        + details("Машинные данные сравнения", _pre(payload))
        + '<nav class="evidence-nav"><a href="comparison.json">Числа JSON</a><a href="report.md">Markdown-отчёт</a></nav></section>'
    )
    return body
