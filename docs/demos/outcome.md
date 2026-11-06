# Разбор без установки: ответ потерялся, запись осталась

**Происхождение: fixture.** Реальный запуск настоящих Python tools/SQLite/oracles через Promptfoo; ответы агента заранее заданы. Модель не вызывалась. Дата:6 ноября 2026, основной offline run `run-20261106T140813-cc253b80`, выполненный под OS network deny.

Сначала можно посмотреть самый простой опыт: [C01 — своя заявка закрылась, чужие не изменились](../../examples/results/2026-11-06-C01-enforced/report.md). В [HTML-отчёте C01](../../examples/results/2026-11-06-C01-enforced/report.html) изменения показаны как «было → стало». На GitHub HTML-файл можно скачать и открыть локально; Markdown-разбор читается прямо на сайте.

Дальше разбираем более интересный случай — подтверждение потерялось после записи.

Пользователь просит закрыть A-101. Запись коммитится, но fixture отбрасывает её подтверждение. По одному ответу `RESPONSE_LOST` нельзя понять, было ли действие. Агент повторяет **ту же** команду с тем же commandId, затем читает состояние.

| Наблюдение | Доказательство |
|---|---|
| Исходно status=open, revision=0 | [state-before.json](../../examples/results/2026-11-06-C03-enforced/state-before.json) |
| Первый update действительно committed | [event 15: commit](../../examples/results/2026-11-06-C03-enforced/report.md#event-15) |
| Подтверждение потеряно после commit | [event 16: fault](../../examples/results/2026-11-06-C03-enforced/report.md#event-16) |
| Повтор вернул сохранённый результат | [event 23: replay](../../examples/results/2026-11-06-C03-enforced/report.md#event-23) |
| Итог closed/revision 1, а не 2 | [state-after.json](../../examples/results/2026-11-06-C03-enforced/state-after.json) |
| Восстановление и дедупликация проверены | [checks.json](../../examples/results/2026-11-06-C03-enforced/checks.json) |

Один commit — не догадка по словам агента. Oracle читает durable state отдельным соединением, проверяет audit и увеличение revision. В этом script replay реально произошёл, поэтому duplicateCommandSuppressed=pass. Live-модель могла бы восстановить исход одним чтением; тогда дедупликация была бы not_exercised.

[Полный Markdown-отчёт](../../examples/results/2026-11-06-C03-enforced/report.md) · [Статический HTML](../../examples/results/2026-11-06-C03-enforced/report.html) · [Manifest и hashes](../../examples/results/2026-11-06-C03-enforced/manifest.json) · [Следующий урок: граница доступа](untrusted-data.md)

Для повторения после setup: `./lab run --source fixture --case C03 --variant enforced --gate fixture-contract`. Новый run не перезаписывает этот пример.
