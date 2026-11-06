"""Local-only lesson UI: exact routes, origin + capability checks, bounded bodies."""

from __future__ import annotations

import fcntl
import hmac
import json
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Annotated, cast
from urllib.parse import urlsplit

from pydantic import Field, JsonValue, ValidationError

from llm_test_lab.contracts import Contract, LearningPolicy, strict_json_loads

from .service import LessonService
from .state import LessonError, Prediction

MAX_BODY = 16384
CSP = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"


class Revision(Contract):
    revision: Annotated[int, Field(ge=0)]


class PredictionRequest(Revision):
    own_closed: bool = Field(alias="ownClosed")
    foreign_disclosed: bool = Field(alias="foreignDisclosed")


class EvidenceRequest(Revision):
    event_sequence: Annotated[int, Field(ge=1)] = Field(alias="eventSequence")


class PolicyRequest(Revision, LearningPolicy):
    pass


class ReflectionRequest(Revision):
    explanation: Annotated[str, Field(min_length=20, max_length=2000)]


class LessonHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, root: Path, *, port: int = 0, service: LessonService | None = None):
        if not 0 <= port <= 65535:
            raise ValueError("PORT_INVALID: port must be between 0 and 65535")
        self.root = root.resolve()
        self.capability = secrets.token_urlsafe(32)
        self.service = service or LessonService(self.root)
        self.static = Path(__file__).parent / "static"
        super().__init__(("127.0.0.1", port), Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"

    @property
    def launch_url(self) -> str:
        # Fragment is never sent in HTTP requests, referrers, server logs or exports.
        return self.origin + "/#token=" + self.capability


class Handler(BaseHTTPRequestHandler):
    server: LessonHTTPServer
    server_version = "LLMTestLab"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(4)

    def log_message(self, format: str, *args: object) -> None:
        # Request paths, capability headers and learner text never enter access logs.
        return

    def _send(
        self,
        status: int,
        data: bytes,
        mime: str = "application/json; charset=utf-8",
        *,
        attachment: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Connection", "close")
        if attachment:
            self.send_header(
                "Content-Disposition", 'attachment; filename="llm-test-lab-experiment.json"'
            )
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, value: object, status: int = 200, *, attachment: bool = False) -> None:
        self._send(
            status,
            (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode(),
            attachment=attachment,
        )

    def _error(self, code: str, message: str, status: int) -> None:
        self._json({"error": {"code": code, "message": message}}, status)

    def _host(self) -> bool:
        hosts = self.headers.get_all("Host", [])
        return hosts == [urlsplit(self.server.origin).netloc]

    def _auth(self, mutation: bool = False) -> bool:
        if not self._host():
            self._error(
                "HOST_REJECTED", "Доступ разрешён только через локальный адрес лаборатории.", 403
            )
            return False
        origins = self.headers.get_all("Origin", [])
        if (mutation and origins != [self.server.origin]) or (
            origins and origins != [self.server.origin]
        ):
            self._error("ORIGIN_REJECTED", "Запрос с другой страницы отклонён.", 403)
            return False
        tokens = self.headers.get_all("X-Lab-Token", [])
        if len(tokens) != 1 or not hmac.compare_digest(
            tokens[0].encode("utf-8"), self.server.capability.encode("ascii")
        ):
            self._error(
                "SESSION_LINK_REQUIRED",
                "Откройте ссылку, которую показал ./lab start. Никаких действий не выполнено.",
                403,
            )
            return False
        return True

    def _path(self) -> str:
        parsed = urlsplit(self.path)
        if parsed.query or parsed.fragment or parsed.scheme or parsed.netloc:
            return ""
        return parsed.path

    def do_GET(self) -> None:
        if not self._host():
            self._error("HOST_REJECTED", "Неверный локальный адрес.", 403)
            return
        path = self._path()
        assets = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/style.css": ("style.css", "text/css; charset=utf-8"),
        }
        if path in assets:
            name, mime = assets[path]
            try:
                self._send(200, (self.server.static / name).read_bytes(), mime)
            except OSError:
                self._error(
                    "UI_MISSING", "Файлы интерфейса отсутствуют. Повторите установку проекта.", 503
                )
            return
        if path not in {"/api/session", "/api/export"}:
            self._error("NOT_FOUND", "Такого действия в уроке нет.", 404)
            return
        if not self._auth():
            return
        try:
            if path == "/api/session":
                self._json(self.server.service.state())
            else:
                self._json(self.server.service.export(), attachment=True)
        except LessonError as exc:
            self._error(exc.code, exc.message, exc.status)
        except (ValueError, OSError):
            self._error(
                "EVIDENCE_INVALID",
                "Материалы нельзя проверить. Пройденный результат не подтверждён.",
                409,
            )

    def do_OPTIONS(self) -> None:
        self._error("CROSS_ORIGIN_DISABLED", "Внешним страницам доступ не предоставляется.", 403)

    def do_POST(self) -> None:
        if not self._auth(mutation=True):
            return
        lengths = self.headers.get_all("Content-Length", [])
        if (
            self.headers.get("Transfer-Encoding")
            or len(lengths) != 1
            or len(lengths[0]) > 5
            or not lengths[0].isascii()
            or not lengths[0].isdigit()
        ):
            self._error("BODY_INVALID", "Нужен один ограниченный JSON-запрос.", 400)
            return
        length = int(lengths[0])
        if not 0 < length <= MAX_BODY:
            self._error("BODY_TOO_LARGE", "Запрос слишком большой.", 413)
            return
        if self.headers.get_content_type() != "application/json":
            self._error("CONTENT_TYPE", "Разрешён только JSON.", 415)
            return
        try:
            raw = strict_json_loads(self.rfile.read(length).decode("utf-8"))
            path = self._path()
            service = self.server.service
            result: dict[str, JsonValue]
            if path == "/api/predict":
                request = PredictionRequest.model_validate(raw)
                result = service.predict(
                    request.revision,
                    Prediction(
                        own_closed=request.own_closed, foreign_disclosed=request.foreign_disclosed
                    ),
                )
            elif path == "/api/evidence":
                evidence = EvidenceRequest.model_validate(raw)
                result = service.evidence(evidence.revision, evidence.event_sequence)
            elif path == "/api/policy":
                policy = PolicyRequest.model_validate(raw)
                result = service.policy(
                    policy.revision,
                    LearningPolicy.model_validate(
                        policy.model_dump(by_alias=True, exclude={"revision"})
                    ),
                )
            elif path == "/api/finish":
                explanation = ReflectionRequest.model_validate(raw)
                if len(explanation.explanation.strip()) < 20:
                    raise LessonError(
                        "EXPLANATION_REQUIRED",
                        "Добавьте короткое объяснение своими словами: хотя бы 20 символов.",
                    )
                result = service.finish(explanation.revision, explanation.explanation)
            elif path in {"/api/verify", "/api/transfer", "/api/reset", "/api/cancel"}:
                version = Revision.model_validate(raw)
                operations = {
                    "/api/verify": service.verify,
                    "/api/transfer": service.transfer,
                    "/api/reset": service.reset,
                    "/api/cancel": service.cancel,
                }
                result = operations[path](version.revision)
            else:
                self._error("NOT_FOUND", "Такого действия в уроке нет.", 404)
                return
            self._json(
                result, 202 if cast(dict[str, JsonValue], result["job"]).get("running") else 200
            )
        except LessonError as exc:
            self._error(exc.code, exc.message, exc.status)
        except (ValidationError, ValueError, UnicodeError):
            self._error(
                "REQUEST_INVALID",
                "Проверьте поля этого шага. Дополнительные команды, пути и настройки модели не разрешены.",
                400,
            )
        except (OSError, TimeoutError):
            self._error(
                "LOCAL_IO_ERROR",
                "Локальная операция не завершена. Обновите страницу и проверьте состояние перед повтором.",
                503,
            )


def serve(root: Path, *, port: int = 0, open_browser: bool = True) -> int:
    root = root.resolve()
    lock_path = root / ".lab-state/learning/server.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError(
                "LESSON_ALREADY_OPEN: лаборатория уже запущена для этого проекта; используйте её окно или завершите прежний процесс"
            ) from exc
        server = LessonHTTPServer(root, port=port)
        try:
            print(
                "Учебная лаборатория запущена только на этом компьютере. Модели и ключи не нужны.",
                flush=True,
            )
            print(server.launch_url, flush=True)
            print(
                "Сохранение прогресса включено. Ctrl+C завершит сервер и отменит активный опыт.",
                flush=True,
            )
            if open_browser:
                webbrowser.open(server.launch_url, new=2)
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            print("\nЗавершаю лабораторию; сохранённые эксперименты не удаляются.", flush=True)
        finally:
            server.server_close()
            server.service.close()
        return 0
