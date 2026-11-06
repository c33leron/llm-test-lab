# Technical preview: доказательства и незакрытая приёмка

> Исторический срез от 3 ноября. Текущая подготовка и ограничения: [release readiness](release-readiness.md). Старые результаты не заменяют проверку изменённого кода.

Это реализованный локальный учебный стенд, **не принятый релиз v0.1 по всем AC исходного ТЗ**. Fixture доказывает работу harness/tools/oracles, а не устойчивость настоящей модели. Публикация, commit/push и платный inference в ходе реализации не выполнялись.

## Проверяемая база

macOS arm64, Python 3.14.6, uv 0.9.7, Node 24.11.0, npm 10.9.8, Promptfoo 0.125.1. Версии Python-зависимостей закреплены в uv.lock; Node-зависимостей — в tooling/promptfoo/package-lock.json. Исходная цель Python 3.13.9 не была выдана за проверенную: загрузка standalone runtime не прошла, поэтому закреплён доступный 3.14.6. Поддержка 3.13 заявлена диапазоном пакета, но пока не подтверждена отдельным прогоном.

Проверены strict schemas/registry graph/пары, ACL и атомарность SQLite, replay/noop/command conflict, потерянное подтверждение, stale revision, исправление намерения, independent state/event oracles, false claims, violation before timeout, evidence tampering, redaction, bounded HTTP, run-wide admission и реальные Promptfoo Python subprocesses. Отдельные process tests используют зависший локальный HTTP stub: SIGINT и SIGKILL управляющего CLI прекращают managed worker group; они не обращаются к модели.

Из независимого ревью исправлены: чтение испорченного envelope сравнением без hash verification; утечка synthetic secret в JSON dictionary key; молчаливое игнорирование explicit limits сохранённого плана; ложная sourceMode маркировка cloud profile как local. Для них сохранены regression controls. Это не заявление об отсутствии всех возможных дефектов.

## Финальный локальный результат 3 ноября 2026

- Ruff check и format check: чисто; strict mypy:19 source files без ошибок.
- pytest: **159 passed**, включая настоящий Promptfoo bridge, SIGINT/SIGKILL, race последнего admission slot, oracle positive/negative controls и пользовательское упражнение fail→pass.
- Schema drift check и `uv sync --locked --offline`: прошли.
- OS network-denied smoke: **9/9 completed**, lab 0, run `run-20261103T061249-9e440eb7`.
- OS network-denied core: **36/36 completed**, lab 0, run `run-20261103T061253-ed27496d`. Два намеренных нарушения C06 в vulnerable/prompt_only обнаружены; это ожидаемый результат fixture-contract.
- OS network-denied F02: **3 timeouts**, lab 3 ожидаем; vulnerable/prompt_only сохраняют violation, enforced остаётся inconclusive. Run `run-20261103T061304-818d7221`.
- У всех 48 attempts этих трёх запусков model dispatches=0. Контрольный socket connect под тем же sandbox profile возвращает EPERM: запрет сети реально действует.
- Crash recovery независимо перепроверен после SIGKILL до envelope: durable foreign commit → interrupted/partial/violation; заблокированная операция → interrupted/partial/inconclusive. Исходные файлы и SQLite побайтово неизменны, derived recovery лежит отдельно.
- Семь проверенных synthetic bundles и три исходных плана сохранены в [examples/results](../examples/results/README.md).

Это результаты на указанной локальной машине, не уже выполненный remote CI и не live reference coverage.

## Отличия и открытые пункты

| Пункт | Статус и требуемое доказательство |
|---|---|
| Local reference / AC03 | Не выполнен: Ollama/model artifact/digest, local-only daemon, conformance, cold/warm hardware measurements |
| Cloud reference / AC04 | Не выполнен: отдельно разрешённые endpoint/model/key/budget и реальный run |
| Reference matrix / AC14 | Не выполнена:96 live attempts, не заменять fixture 36 |
| Два новичка / AC13 | Не выполнено: реальные наблюдения самостоятельного прохождения; агентные тесты не заменяют людей |
| Linux | CI workflow создан, но remote CI не запускался; local macOS tests не доказывают Linux |
| Full profile conformance | Probe сейчас ограниченный C01 smoke; tokenizer/context boundary и весь live protocol требуют дополнительной процедуры |
| Promptfoo viewer | В 0.125.1 обнаружен wildcard bind без поддержанного host option. lab view отказывает до запуска. Статические HTML/Markdown+SVG доступны, но исходная viewer-приёмка не закрыта |
| Визуальная проверка HTML | HTML/escaping/CSP проверены кодом и тестами; браузерный просмотр file:// заблокирован политикой browser tool, visual QA не объявляется пройденным |
| Offline scope | Установка пакетов требует сети. Только отдельный установленный fixture run под OS network deny доказывает offline выполнение; uv --offline сам по себе этого не доказывает |

## Приёмка людьми

Два участника отдельно: найти старт → разобрать recorded C03 → показать readback/commit → отличить refusal от server deny → создать новый case, получить содержательный fail старого script и pass исправленного → объяснить, почему это не оценка модели. Подсказки, ожидание установки, первый проход и повтор фиксируются раздельно. До таких наблюдений не писать «проверено новичками».

## Воспроизвести технические проверки

```sh
uv run --no-sync --offline ruff check .
uv run --no-sync --offline ruff format --check .
uv run --no-sync --offline mypy src/llm_test_lab
uv run --no-sync --offline pytest
uv run --no-sync --offline python -m llm_test_lab.contracts.schemas --check
uv run --no-sync --offline python scripts/check_docs.py
```

После setup на macOS доступен строгий отдельный offline-run:

```sh
sandbox-exec -p '(version 1)(allow default)(deny network*)' .venv/bin/lab run --source fixture --suite core --variants all --gate fixture-contract
```

Запрещается сеть всей запущенной группе, включая Node и Python workers, не только зависимости uv. Process cancellation tests, которым нужен loopback HTTP stub, запускаются отдельно от этого запрета. Не переносить результат macOS sandbox на другие OS без проверки.
