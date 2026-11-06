# Подключить модель

Это пока экспериментальный **CLI-режим**: модель работает с нашими заявками и инструментами, а не с произвольным внешним приложением. Профили в поставке — шаблоны; совместимость с настоящими моделями ещё не подтверждена.

## Ollama

Установи Ollama и скачай модель с поддержкой tools самостоятельно. Подставь её точный tag:

```sh
export LLM_LAB_LOCAL_MODEL='your-downloaded-model-tag'
./lab plan --profile local-reference --case C01 --variant enforced --max-inference-calls 6 --output plans/local-one.json
./lab run --plan plans/local-one.json --confirm-live
```

Сначала прочитай созданный план, потом выполняй `run`. Лаба не скачивает модели и не меняет настройки daemon. Для действительно локального опыта проверь модель/digest, конфигурацию daemon и отсутствие внешних соединений. Переменная `OLLAMA_NO_CLOUD=1` только в терминале lab этого не доказывает.

## Облачный провайдер

В `profiles/cloud-reference.json` уже задан HTTPS endpoint и Chat Completions-протокол с native tools: UTF-8 JSON, без stream и сжатия. Укажи точную модель через `LLM_LAB_CLOUD_MODEL`, а ключ — через окружение `LLM_LAB_API_KEY`. Не записывай секрет в профиль, команду, shell history или GitHub.

```sh
./lab plan --profile cloud-reference --case C01 --variant enforced --max-inference-calls 6 --output plans/cloud-one.json
./lab run --plan plans/cloud-one.json --confirm-live
```

Для другого провайдера создай [отдельный профиль](extending.md). `outputTokenParameter` выбирается явно: `max_completion_tokens` или `max_tokens`; повтор с другим параметром не выполняется. `think` допустим только для Ollama, `reasoning_effort` — для Chat Completions. Название «compatible» не гарантирует поддержку конкретной модели.

**Один опыт может сделать несколько запросов.** В примерах лимит — шесть. Это ограничение числа вызовов, не расходов: денежного hard cap нет, входные токены тоже могут оплачиваться. Отмена локального ожидания не гарантирует остановку вычисления у провайдера. Автоматического fallback и платного judge нет.

## Проверить совместимость

`./lab probe --profile local-reference` **сразу вызывает модель**: C05/C01/C10, максимум 18 запросов. Результат сохраняется в `conformance.json`. Это короткая проверка, не сертификат поддержки.

Для `compatibility=supported` нужны связанные доказательства работы текста, native tools и их результатов, final JSON, timeout/abort, usage, лимитов вывода и контекста. Для Ollama дополнительно проверяется local-only работа. `conformanceEvidence` привязывает их к точной модели, endpoint, настройкам, коду и зависимостям; смена входов требует новой проверки. Fixture и одна успешная C01 этого не подтверждают.

Текущий context guard оценивает размер по байтам, а не универсальным tokenizer: точную границу контекста каждой модели нужно проверять отдельно.

Полную матрицу можно сначала спланировать, не запуская её:

```sh
./lab plan --suite release-reference --profiles local-reference,cloud-reference --variants all --max-inference-calls 576 --output plans/release.json
```

Это 96 опытов с бюджетом до 576 запросов, не выполненная оценка моделей. Сохраняй неудачные и неполные результаты; повтор создаёт новый опыт с чистой базой.
