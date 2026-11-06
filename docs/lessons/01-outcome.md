# Урок 1. «Готово» не равно commit

**Цель:** различить намерение, вызов, подтверждение и сохранённый эффект. Прежде чем запускать C01–C04, спрогнозируй статус и revision A-101. В каком случае повтор безопасен?

```sh
./lab run --source fixture --case C01 --variant enforced --gate fixture-contract
./lab run --source fixture --case C02 --variant enforced --gate fixture-contract
./lab run --source fixture --case C03 --variant enforced --gate fixture-contract
./lab run --source fixture --case C04 --variant enforced --gate fixture-contract
```

C01 — обычное закрытие. В C02 ошибка возникает до commit: статус и revision не должны измениться. C03 теряет ответ уже **после** commit: состояние изменилось, хотя подтверждения нет. Один и тот же commandId с идентичным payload возвращает сохранённый результат; прежняя revision не превращает exact replay в конфликт. В C04 другая контролируемая правка меняет приоритет после чтения. Это не нагрузка и не тест гонки: устаревшая запись должна получить VERSION_CONFLICT и не затереть правку.

Открой report, before/after и журнал. Найди `commit`, `fault`, `replay`, `fixture_intervention`, `version_conflict`. Сколько реальных commit было в C03? Почему два вызова не означают две записи? Каким отдельным соединением проверяется итог?

**Изменение:** создай свой case по C03. В повторном update измени patch, сохранив тот же ход. Сначала предскажи COMMAND_CONFLICT, потом проверь. Не меняй официальные C03/expectation, чтобы не потерять исходный контроль.

**Перенос:** платёжный API вернул timeout. Какие два факта тебе нужны перед повторной оплатой? [Разбор после опыта](../solutions/01-outcome.md).
