"""A guided fixture route, not a second scheduler or a hidden live-model path."""

from __future__ import annotations

import sys
from pathlib import Path

from llm_test_lab.run_control.planning import make_plan

from .output import open_report
from .runtime import execute_plan

HELP = """LLM Test Lab — испытай агента и проверь результат по фактам.

Первый вход:
  ./lab setup              Установить закреплённые зависимости только в проект.
  ./lab start              Открыть локальную учебную миссию в браузере.
  ./lab start --terminal   Старое меню опытов прямо в терминале.
  ./lab start --web --no-open
                           Запустить экран явно, не открывая браузер автоматически.
  ./lab start --case C01    Первый опыт без меню, модели и ключа.
  ./lab start --case F01 --variants all
                           Сравнить запрещённое действие и серверную защиту.

Дальше:
  ./lab doctor             Проверить установку без обращения к модели.
  ./lab run --source fixture --suite core --variants all --gate fixture-contract
  ./lab run --source fixture --case C03 --variant enforced --open
  ./lab new-case --from C01 --id MY01
  ./lab --help             Все команды и продвинутые настройки.
  ./lab run --help         Профили, лимиты, сохранённые планы.

Fixture использует заданные ответы, настоящие инструменты и SQLite.
Это проверка лаборатории, а не устойчивости нейросети. Live выбирается отдельно
через run --profile; setup/start сами не скачивают модели и не используют ключи.
--json включает машинный вывод запусков. --open открывает полученный HTML-отчёт.
Учебный экран доступен только на loopback; его запуск сам не выполняет опыт.
"""

LESSONS: dict[str, tuple[str, list[str]]] = {
    "1": ("Ответ и действие: запись, сбой, повтор, конфликт", ["C01", "C02", "C03", "C04"]),
    "2": ("Данные и инструкции: чтение, чужой доступ, инъекция", ["C05", "C06", "C07", "C08"]),
    "3": ("Полезность защиты: цитата, исправление, уточнение", ["C09", "C10", "C11", "C12"]),
}


def latest_report(root: Path) -> Path:
    candidates = [path for path in (root / "runs").glob("*/report.html") if path.is_file()]
    if not candidates:
        raise ValueError("REPORT_MISSING: отчётов пока нет; начни с ./lab start --case C01")
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def run_fixture(
    root: Path,
    case_id: str,
    variants: list[str],
    *,
    open_html: bool = False,
    json_output: bool = False,
) -> int:
    gate = (
        "fixture-contract"
        if case_id in {f"C{i:02d}" for i in range(1, 13)} | {"F01", "F02"}
        else "none"
    )
    plan = make_plan(root, cases=[case_id], source="fixture", variants=variants, gate=gate)
    if not json_output:
        print(
            f"Запускаю {case_id}: {len(plan.cells)} вариант(а), синтетические данные, вызовов модели: 0.",
            flush=True,
        )
    code, run_dir = execute_plan(root, plan, json_output=json_output)
    if open_html and (run_dir / "report.html").exists():
        open_report(root, run_dir / "report.html")
    return code


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return "0"


def _serve_learning(root: Path, *, port: int, open_browser: bool) -> int:
    from llm_test_lab.learning.server import serve

    return serve(root, port=port, open_browser=open_browser)


def start(
    root: Path,
    *,
    case_id: str | None = None,
    variants: list[str] | None = None,
    open_html: bool = False,
    json_output: bool = False,
    terminal: bool = False,
    web: bool = False,
    no_open: bool = False,
    port: int = 0,
) -> int:
    if terminal and web or case_id is not None and (terminal or web):
        raise ValueError("START_MODE_CONFLICT: выбери --case, --terminal или --web")
    if open_html and no_open:
        raise ValueError("START_MODE_CONFLICT: --open и --no-open несовместимы")
    if not 0 <= port <= 65535:
        raise ValueError("PORT_INVALID: порт должен быть от 0 до 65535")
    if (terminal or case_id is not None) and port:
        raise ValueError("START_MODE_CONFLICT: --port применяется только к учебному веб-экрану")
    if case_id:
        return run_fixture(
            root, case_id, variants or ["enforced"], open_html=open_html, json_output=json_output
        )
    if json_output:
        raise ValueError("CASE_REQUIRED: для ./lab start --json укажи --case C01")
    if not sys.stdin.isatty():
        if web:
            return _serve_learning(root, port=port, open_browser=not no_open)
        print(
            "Нет интерактивного терминала. Ничего не запущено.\n"
            "Учебный экран явно: ./lab start --web --no-open\n"
            "Первый опыт: ./lab start --case C01\n"
            "Проверка защиты: ./lab start --case F01 --variants all\n"
            "Меню в терминале: ./lab start --terminal\n"
            "Справка: ./lab help"
        )
        return 0
    if not terminal:
        return _serve_learning(root, port=port, open_browser=not no_open)
    while True:
        print(
            "\nLLM Test Lab · учебные опыты без модели и ключа\n"
            "1. Первый опыт: закрыть свою заявку и проверить запись\n"
            "2. Проверить защиту: запрещённый вызов в трёх вариантах\n"
            "3. Выбрать урок и другой сценарий\n"
            "4. Открыть последний локальный отчёт\n"
            "0. Выход"
        )
        choice = _ask("Выбор [0–4]: ")
        if choice == "0":
            return 0
        if choice == "4":
            try:
                open_report(root, latest_report(root))
            except ValueError as exc:
                print(str(exc))
            continue
        selected, selected_variants = "", ["enforced"]
        if choice == "1":
            selected = "C01"
        elif choice == "2":
            selected, selected_variants = "F01", ["all"]
        elif choice == "3":
            for key, (title, cases) in LESSONS.items():
                print(f"{key}. {title} ({', '.join(cases)})")
            lesson = _ask("Урок [1–3], 0 — назад: ")
            if lesson not in LESSONS:
                continue
            allowed = LESSONS[lesson][1]
            selected = _ask(f"Сценарий [{', '.join(allowed)}], 0 — назад: ").upper()
            if selected not in allowed:
                print("Выбери ID из указанного урока; запуск не выполнен.")
                continue
        else:
            print("Нужна цифра от 0 до 4; запуск не выполнен.")
            continue
        code = run_fixture(root, selected, selected_variants, open_html=open_html)
        if not open_html and _ask("Открыть полученный HTML-отчёт? [д/н]: ").lower() in {
            "д",
            "да",
            "y",
            "yes",
        }:
            open_report(root, latest_report(root))
        if code in {2, 3, 130}:
            return code
