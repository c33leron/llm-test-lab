# Live — отдельный эксперимент

**Проверенных моделей в поставке пока нет.** Два профиля — experimental templates. Transport tests с HTTP stubs подтверждают wire/ошибки/лимиты, а не качество или совместимость настоящей модели. Probe выполняет ограниченную проверку C05/C01/C10 (до 18 запросов) и не заменяет граничные и аппаратные проверки из ТЗ. Он сохраняет отдельный conformance.json, а непроверенные свойства не объявляет пройденными.

## Ollama

Установи Ollama и скачай tool-capable модель самостоятельно. Задай точный tag через переменную `LLM_LAB_LOCAL_MODEL`. У процесса daemon должны быть выключены облачные функции; `OLLAMA_NO_CLOUD=1` в одном только терминале lab ничего не доказывает. Нужны версия Ollama, artifact/digest, hardware, cold/warm время и проверка без upstream egress. Лаба не перезапускает daemon и не скачивает гигабайты автоматически.

```sh
export LLM_LAB_LOCAL_MODEL='your-downloaded-model-tag'
./lab plan --profile local-reference --case C01 --variant enforced
./lab probe --profile local-reference
```

Замени tag своим реально установленным значением. Указание модели — не обещание её tools-поддержки. До первого inference проверь plan. Отсутствие подтверждённой local-only конфигурации означает experimental, не «доказанно без облака».

## Cloud

`profiles/cloud-reference.json` описывает Chat Completions endpoint с native function tools и явным `outputTokenParameter=max_completion_tokens`. Для совместимого провайдера с прежним контрактом можно выбрать `max_tokens` в отдельном профиле; скрытого повторного запроса с другим параметром нет. Это **не обещание поддержки любой OpenAI-compatible модели**. Дополнительные generation parameters проверяются до запроса; для reasoning-моделей нужен отдельно проверенный профиль. Поле `think` допускается только для Ollama, `reasoning_effort` — только для Chat Completions. Их поддержка конкретной моделью должна быть измерена. Ключ экспортируй локально без записи в shell history; не отправляй его в чат или JSON-профиль. Имя переменной — `LLM_LAB_API_KEY`, модель — `LLM_LAB_CLOUD_MODEL`.

```sh
./lab plan --profile cloud-reference --case C01 --variant enforced --output plans/cloud-one.json
./lab run --plan plans/cloud-one.json --confirm-live
```

План может отправить до шести запросов на один attempt, не один HTTP на «один тест». Верхняя граница токенов не ограничивает input billing или внутренние особенности тарифа. Cost unknown; денежного hard cap нет. Никакого auto fallback и платного judge.

## Reference acceptance

До supported claim нужны текстовый ответ, native tool calls и продолжение после result, последовательные вызовы, final JSON, timeout/abort, output cap, usage, boundary контекста и отсутствие скрытого truncation. Консервативный byte-based context guard текущего transport — ограничитель experimental-пути, не проверенный tokenizer любого профиля.

Полная release-reference: 12 cases ×3 variants ×2 модели =72 attempts, ещё по два повтора C01/C08 =24; всего96, максимум576 inference calls. Установленный preset не исполняется автоматически:

```sh
./lab plan --suite release-reference --profiles local-reference,cloud-reference --variants all --max-inference-calls 576 --output plans/release.json
```

При неверной модели/неизвестном исходе/таймауте сохраняй исходный результат. Повтор — новый attempt с чистой БД, не стирание неудобной ячейки. Ни этот файл, ни зелёный fixture run не свидетельствуют, что reference matrix выполнена.

## Что означает supported

Одной строки `compatibility=supported` недостаточно. Ссылка `conformanceEvidence` должна вести на существующие локальные доказательства, связанные с точной моделью, endpoint, настройками, исходниками, runtime pins и lockfiles. Изменение этих входов требует новой проверки. Fixture не может подтвердить live support. Граничные проверки, отмена и local-only свойства требуют отдельных связанных артефактов; их нельзя получить из одной зелёной строки C01. Формат проверяет `llm_test_lab.conformance`, но хеши не являются подписью против владельца компьютера.

Транспорт читает ответ с ограничением размера во время получения, а не после неограниченного скачивания. Автоматического повтора или перехода на другой endpoint нет. Поддерживается UTF-8 JSON без сжатия ответа (Accept-Encoding: identity).

Wire-поля сверены с [документацией Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) и [Ollama chat](https://docs.ollama.com/api/chat); это основание реализации, не доказательство совместимости живой модели.
