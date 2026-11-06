# Где связь сломалась

| Признак | Что проверять | Следующее действие |
|---|---|---|
| DEPENDENCY_MISSING | `.venv`/локальный Promptfoo/Node | `./lab setup`, не глобальный npx latest |
| ENVIRONMENT_MISMATCH | версии и хеши lockfiles/fingerprint | повторить `./lab setup`; он проверит установку и запишет fingerprint |
| CASE_INVALID / INVALID_* | extra fields, типы, registry IDs, пары | `./lab validate`; строка вместо revision — ошибка, не автокоррекция |
| FIXTURE_STEP_MISMATCH | turnId и ожидаемый предыдущий tool result | сравнить script с events; не подменять verdict |
| BUDGET_EXCEEDED | верхняя граница плана против run budget | уменьшить матрицу или явно создать план с новым лимитом |
| MODEL_MISSING | переменная modelEnv | указать точный ID до plan |
| AUTH_FAILED / RATE_LIMITED | разрешённый endpoint/key/status | остановиться; не включать скрытые retries |
| CONTEXT_LIMIT | полный запрос + reserve | выбрать меньший case/явно проверенный профиль с большим окном |
| model_error | final JSON, finish reason, tool protocol | посмотреть raw response; не считать сбоем инфраструктуры |
| EVIDENCE_INVALID | отсутствующий/изменённый файл bundle | сохранять исходник, не переписывать hashes ради PASS |
| CAPABILITY_UNSUPPORTED у view | wildcard bind в Promptfoo 0.125.1 | открыть static report.html; сервер не запущен |

Ctrl+C не откатывает уже состоявшийся commit. Сначала inspect сохранённых данных, потом решай, нужен ли новый run. Не удаляй runs в качестве «починки». Частичный report и отсутствие финального envelope — техническая неполнота, не успешный тест.

При обращении за помощью приложи код ошибки, версии, case/variant/source, идентификатор run и очищенный фрагмент evidence. Не прикладывай `.env`, ключи и весь environment.

`./lab start` без интерактивного терминала ничего не запускает и показывает короткую команду `./lab start --case C01`. `--json` нужен только скриптам, для обычного просмотра оставь человеческий вывод. Если ./lab отсутствует, проверь, что терминал открыт именно в распакованной папке проекта.
