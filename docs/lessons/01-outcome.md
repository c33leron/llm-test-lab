# Урок 1. «Готово» не значит «сохранено»

Предположи, какими станут статус и revision A-101 в каждом случае. Когда безопасно повторить запрос?

```sh
./lab run --source fixture --case C01 --variant enforced --gate fixture-contract
./lab run --source fixture --case C02 --variant enforced --gate fixture-contract
./lab run --source fixture --case C03 --variant enforced --gate fixture-contract
./lab run --source fixture --case C04 --variant enforced --gate fixture-contract
```

C01 — обычное закрытие. В C02 ошибка происходит до сохранения. C03 теряет подтверждение **после** сохранения: повтор той же команды должен вернуть прежний результат, не записать второй раз. В C04 между чтением и записью меняется приоритет: устаревший запрос должен получить `VERSION_CONFLICT`, не затерев новую правку.

Сравни состояние до и после с журналом: `commit`, `fault`, `replay`, `fixture_intervention`, `version_conflict`. Сколько записей было в C03? Почему два вызова не означают два изменения?

Теперь создай копию C03. В повторном вызове измени `patch`, сохранив тот же ход и `commandId`. Предскажи результат, затем проверь `COMMAND_CONFLICT`. Исходный C03 и его ожидания оставь без изменений.

Похожая ситуация: платёжный API вернул таймаут. Что нужно проверить перед повтором? [Разбор](../solutions/01-outcome.md)
