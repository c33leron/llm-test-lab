# Как связан результат

```text
JSON case + fixture + expectation + response script + variant/profile
  → strict registry validation + pair validation
  → immutable plan with input hashes and explicit upper bounds
  → Python CLI starts pinned Promptfoo in a clean run directory
  → Python provider claims exactly one approved cell in control.sqlite
  → bounded agent loop feeds one user turn at a time
     → scripted response OR explicit HTTP transport
     → strict tool schema → ACL → idempotency → revision → SQLite transaction
  → fresh read-only connection checks state and durable audit
  → envelope + JSON/Markdown/HTML evidence + lab gate
```

Promptfoo исполняет конечную матрицу. `run_control/planning.py` только раскрывает список ячеек; он не запускает их собственным циклом. Повторный callback одной ячейки не получает новое право на действие. Управляющий SQLite-файл отдельно от предметного `state.sqlite`; tools не умеют обращаться к нему, shell, сети, произвольному SQL или чужим attempts.

`contracts/` задаёт strict Pydantic-модели и JSON Schema. `registry.py` связывает локальные IDs. `tickets/` отвечает за бизнес-действия, а `oracles/` читает факты независимо от ответа модели. `agent/` ограничивает число вызовов, переходы ходов и deadline. `transports/` нормализует Ollama и один Chat Completions compatible протокол без stream/retry/redirect. `evidence/` сохраняет bundle и проверяет hashes; `reporting/` показывает их без исполнения содержимого. `promptfoo_adapter/` — только bridge и четыре именованные assertions. `cli/` управляет пользовательским запуском.

## Одна мутация

В транзакции вместе сохраняются ticket, новая revision, audit commit и результат commandId. При повторе той же команды сначала проверяется ACL, затем ранее сохранённый canonical payload. Exact replay возвращает старый результат даже со старой revision. Другой payload на том же commandId даёт COMMAND_CONFLICT. Неуспешный вызов до commit не закрепляет commandId. Noop не увеличивает revision, но закрепляет успешную команду.

Principal и commandId создаёт driver, а не модель. commandId новый на каждый пользовательский ход; после успешного commit этого хода вторая смысловая мутация не разрешена даже в vulnerable. Намеренно выключается **только ACL**, не остальные ограничения host/domain.

## Учебный маршрут

`learning/static/` отображает семь шагов и отправляет только ограниченные JSON-команды. `learning/state.py` хранит прогресс в отдельной SQLite-базе с монотонной revision, а `learning/service.py` проверяет порядок шагов и собирает факты из неизменяемых attempts. Браузер не выставляет verdict и не исполняет политику.

HTTP-поток не запускает второй scheduler. Он создаёт обычный immutable plan для L01–L05 и отдельный `learning.process`, который вызывает существующий `execute_plan()` из главного потока Python. Далее работают те же Promptfoo, bridge, tools и oracles. Child следит за HTTP-parent, а существующий watchdog — за управляемой группой Promptfoo.

`LearningPolicy` явно связывает источник личности, правило владения, охват чтения/записи и время проверки. Вариант `learning` разрешён только пяти fixture cases; он не добавляется к обычному `--variants all`. Сначала выполняется исходный L01, затем L01/L02/L03/L05 с выбранной политикой и отдельно L04 с другим пользователем. Критерии — сохранённая обычная задача и отсутствие наблюдаемого нарушения, не равенство политики готовому ответу. HTML/JS/CSS интерфейса тоже входят в хеши плана.

## Отмена

Run-wide reservations живут в SQLite. Mutation guard сериализует отмену и commit. Watchdog следит за владельцем и управляемой группой процессов; HTTP ожидание тоже периодически проверяет cancel/deadline. Уже committed действие сохраняется. Незачатые ячейки — not_run, а не искусственные успешные envelopes. Остановка локального HTTP-клиента не доказывает прекращения удалённого вычисления.

## Чего здесь нет

Своего matrix scheduler, SaaS, регистрации, ORM, очереди, agent framework, произвольных Python-выражений в cases, обязательного LLM-судьи, RAG/MCP/голоса. Сначала проверяем границу в маленьком домене, а не строим платформу, где невозможно объяснить ни один результат.
