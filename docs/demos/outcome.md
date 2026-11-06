# Ответ потерялся, запись осталась

Учебный прогон от 6 ноября 2026, без модели и сети. Инструменты и SQLite настоящие, действия агента заданы сценарием. Исходный запуск: `run-20261106T140813-cc253b80`.

Для начала — [C01: своя заявка закрылась, чужие не изменились](../../examples/results/2026-11-06-C01-enforced/report.md). [HTML-отчёт](../../examples/results/2026-11-06-C01-enforced/report.html) можно скачать и открыть локально, а Markdown читается прямо на GitHub.

В C03 пользователь просит закрыть A-101. База сохраняет изменение, но подтверждение теряется. Ответ `RESPONSE_LOST` ещё не означает, что запись не состоялась. Агент повторяет ту же команду с тем же `commandId`, затем читает состояние.

| Что произошло | Где проверить |
|---|---|
| Сначала `status=open`, `revision=0` | [Состояние до](../../examples/results/2026-11-06-C03-enforced/state-before.json) |
| Изменение сохранено | [Событие 15: commit](../../examples/results/2026-11-06-C03-enforced/report.md#event-15) |
| Подтверждение потерялось после записи | [Событие 16: fault](../../examples/results/2026-11-06-C03-enforced/report.md#event-16) |
| Повтор вернул прежний результат | [Событие 23: replay](../../examples/results/2026-11-06-C03-enforced/report.md#event-23) |
| В итоге `closed`, revision **1**, не 2 | [Состояние после](../../examples/results/2026-11-06-C03-enforced/state-after.json) |
| Восстановление и отсутствие дубля подтверждены | [Проверки](../../examples/results/2026-11-06-C03-enforced/checks.json) |

Независимая проверка читает базу отдельным соединением и сверяет журнал записей. Здесь повтор действительно был, поэтому `duplicateCommandSuppressed=pass`. Настоящая модель могла бы выяснить результат одним чтением; тогда повторная запись не проверялась бы — `not_exercised`.

Повторить после установки: `./lab run --source fixture --case C03 --variant enforced --gate fixture-contract`. Новый запуск не перезапишет этот пример.

[Полный отчёт](../../examples/results/2026-11-06-C03-enforced/report.md) · [HTML](../../examples/results/2026-11-06-C03-enforced/report.html) · [Хеши материалов](../../examples/results/2026-11-06-C03-enforced/manifest.json) · [Дальше: граница доступа](untrusted-data.md)
