"""Local, Python-native conversational assistant backend.

The module deliberately owns only the safe, generic chat substrate:

* SQLite-backed sessions, messages, and attachment metadata;
* one private workspace per session;
* bounded local previews for explicitly selected attachments; and
* an injected OpenAI-compatible transport.

It does not execute commands, submit jobs, or expose remote/HPC operations.  A
caller may add narrowly-scoped tools above this layer after applying its own
confirmation and authorization policy.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import sqlite3
import stat
import threading
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Sequence

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_SESSION_BYTES = 100 * 1024 * 1024
MAX_PREVIEW_BYTES = 64 * 1024
MAX_PREVIEW_CHARS = 16_000
MAX_MESSAGE_CHARS = 50_000
MAX_ATTACHMENTS_PER_MESSAGE = 10
MAX_REMOTE_ATTACHMENT_CHARS = 64_000

MAX_ZIP_MEMBERS = 1_000
MAX_ZIP_MEMBER_BYTES = 100 * 1024 * 1024
MAX_ZIP_UNCOMPRESSED_BYTES = 250 * 1024 * 1024
MAX_ZIP_COMPRESSION_RATIO = 200
MAX_ZIP_NAME_CHARS = 200_000
ZIP_PREVIEW_MEMBERS = 200

DEFAULT_TIMEOUT = 90
DEFAULT_HISTORY_MESSAGES = 40
DEFAULT_SYSTEM_PROMPT = (
    "You are the local conversational assistant for VASP Catalyst Studio. "
    "Use only the conversation and explicitly attached previews supplied by the user. "
    "Do not claim to have run calculations, shell commands, cluster jobs, or file changes. "
    "When evidence is insufficient, say so clearly."
)

HELP_TEXT = (
    "可用命令：\n"
    "/help — 显示本帮助\n"
    "/stop — 停止本会话中正在等待的 AI 回复\n\n"
    "附件会先在本地安全导入；只有发送消息时明确选择的附件预览才会提供给模型。"
)
STOPPED_TEXT = "已请求停止当前 AI 回复；迟到的模型结果不会保存。"
NOT_RUNNING_TEXT = "当前会话没有正在生成的 AI 回复。"

_TEXT_EXTENSIONS = {
    ".txt", ".md", ".rst", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".log", ".dat", ".vasp", ".xyz", ".cif", ".in", ".out", ".py",
}
_VASP_NAMES = {
    "INCAR", "POSCAR", "CONTCAR", "KPOINTS", "POTCAR", "OUTCAR", "OSZICAR",
    "XDATCAR", "EIGENVAL", "DOSCAR", "PROCAR", "IBZKPT", "CHGCAR", "LOCPOT",
}
_DRIVE_PATH = re.compile(r"^[A-Za-z]:")

Transport = Callable[[str, bytes, dict[str, str], int, threading.Event], Any]
ConfigLoader = Callable[[], dict[str, Any] | None]
KeyLoader = Callable[[], str | None]


class AssistantChatError(RuntimeError):
    """Base class for safe, user-presentable assistant backend failures."""


class SessionNotFound(AssistantChatError):
    """The requested session does not exist."""


class AttachmentRejected(AssistantChatError):
    """An attachment failed a local safety or quota check."""


class SessionBusy(AssistantChatError):
    """A session already has an in-flight model request."""


class ChatConfigurationError(AssistantChatError):
    """The injected model configuration is incomplete or invalid."""


class ChatTransportError(AssistantChatError):
    """The OpenAI-compatible endpoint failed or returned malformed output."""


class AssistantChat:
    """Persistent local chat service rooted in a caller-selected directory.

    ``transport`` receives ``(url, body, headers, timeout, cancel_event)``.
    It may return ``(status_code, bytes_or_json)``, a decoded OpenAI response
    dictionary, or a response-like object with ``status_code`` and ``json()``.
    The event lets cooperative transports abort promptly; the service also
    checks it before persisting a response, so non-cooperative late results are
    suppressed.
    """

    def __init__(
        self,
        workspace_root: str | os.PathLike[str],
        *,
        transport: Transport | None = None,
        config_loader: ConfigLoader | None = None,
        key_loader: KeyLoader | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        root = Path(workspace_root).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve()
        self.sessions_root = self.root / "sessions"
        self.db_path = self.root / "assistant_chat.sqlite3"
        self._ensure_private_dir(self.sessions_root)
        if self.db_path.is_symlink():
            raise AssistantChatError("Assistant database must not be a symbolic link")

        self._transport = transport or _default_transport
        self._transport_accepts_cancel = _accepts_cancel_event(self._transport)
        self._config_loader = config_loader or (lambda: {})
        self._key_loader = key_loader or (lambda: None)
        self._timeout = _bounded_int(timeout, 1, 600, DEFAULT_TIMEOUT)
        self._active: dict[str, tuple[str, threading.Event]] = {}
        self._active_lock = threading.RLock()
        self._attachment_lock = threading.RLock()
        self._initialize_database()

    # ── public session APIs ──────────────────────────────────────────────
    def create_session(self, title: str | None = None) -> dict[str, Any]:
        session_id = uuid.uuid4().hex
        workspace_name = session_id
        clean_title = _clean_title(title)
        now = _utc_now()
        workspace = self.sessions_root / workspace_name
        self._ensure_private_dir(workspace)
        self._ensure_private_dir(workspace / "attachments")
        try:
            with self._connection() as conn:
                conn.execute(
                    """
                    INSERT INTO sessions
                        (id, title, workspace_name, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (session_id, clean_title, workspace_name, now, now),
                )
        except Exception:
            # The newly-created directories are empty at this point.  Best-effort
            # cleanup avoids orphan workspaces while never touching broader paths.
            try:
                (workspace / "attachments").rmdir()
                workspace.rmdir()
            except OSError:
                pass
            raise
        return self._get_session(session_id)

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT s.id, s.title, s.created_at, s.updated_at,
                       COUNT(DISTINCT m.id) AS message_count,
                       COUNT(DISTINCT a.id) AS attachment_count,
                       COALESCE(MAX(m.created_at), s.updated_at) AS last_activity
                FROM sessions AS s
                LEFT JOIN messages AS m ON m.session_id = s.id
                LEFT JOIN attachments AS a ON a.session_id = s.id
                GROUP BY s.id
                ORDER BY last_activity DESC, s.created_at DESC
                """
            ).fetchall()
        return [
            {
                "id": row["id"],
                "title": row["title"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "message_count": int(row["message_count"]),
                "attachment_count": int(row["attachment_count"]),
            }
            for row in rows
        ]

    def history(
        self,
        session_id: str,
        *,
        include_previews: bool = True,
    ) -> list[dict[str, Any]]:
        self._get_session(session_id)
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT id, role, content, status, source, request_id, created_at
                FROM messages
                WHERE session_id = ?
                ORDER BY id
                """,
                (session_id,),
            ).fetchall()
            result = []
            for row in rows:
                attachments = self._message_attachments(
                    conn, int(row["id"]), include_preview=include_previews
                )
                result.append(
                    {
                        "id": int(row["id"]),
                        "role": row["role"],
                        "content": row["content"],
                        "status": row["status"],
                        "source": row["source"],
                        "request_id": row["request_id"],
                        "created_at": row["created_at"],
                        "attachments": attachments,
                    }
                )
        return result

    # ── public attachment APIs ───────────────────────────────────────────
    def attach(
        self,
        session_id: str,
        source: str | os.PathLike[str],
    ) -> dict[str, Any]:
        """Safely copy one local regular file into the session workspace.

        The source path is never persisted.  Copying and hashing use the same
        open file descriptor, and the byte limit is enforced while streaming
        as well as from the initial ``fstat`` result.
        """
        session = self._get_session(session_id)
        source_path = Path(source)
        original_name = source_path.name
        if not original_name or original_name in {".", ".."}:
            raise AttachmentRejected("Attachment must have a file name")

        with self._attachment_lock:
            source_file, source_size = self._open_regular_source(source_path)
            temp_path: Path | None = None
            final_path: Path | None = None
            try:
                with self._connection() as conn:
                    used = conn.execute(
                        "SELECT COALESCE(SUM(size_bytes), 0) FROM attachments WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()[0]
                if int(used) + source_size > MAX_SESSION_BYTES:
                    raise AttachmentRejected(
                        f"Session attachment quota exceeds {MAX_SESSION_BYTES // (1024 * 1024)} MB"
                    )

                attachment_dir = self._attachment_dir(session["workspace_name"])
                temp_path = attachment_dir / f".upload-{uuid.uuid4().hex}.tmp"
                digest, copied = self._stream_copy(source_file, temp_path)
                if int(used) + copied > MAX_SESSION_BYTES:
                    raise AttachmentRejected(
                        f"Session attachment quota exceeds {MAX_SESSION_BYTES // (1024 * 1024)} MB"
                    )

                kind, preview, preview_note = _build_preview(temp_path, original_name)
                stored_name = _stored_name(original_name, digest)
                final_path = attachment_dir / stored_name
                if final_path.exists() or final_path.is_symlink():
                    # UUID suffixes make this exceptionally unlikely, but never
                    # overwrite an existing workspace file.
                    stored_name = _stored_name(original_name, digest, token=uuid.uuid4().hex)
                    final_path = attachment_dir / stored_name
                os.replace(temp_path, final_path)
                temp_path = None
                _tighten_file_permissions(final_path)

                attachment_id = uuid.uuid4().hex
                now = _utc_now()
                try:
                    with self._connection() as conn:
                        conn.execute(
                            """
                            INSERT INTO attachments
                                (id, session_id, original_name, stored_name, sha256,
                                 size_bytes, kind, preview, preview_note, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                attachment_id, session_id, original_name, stored_name,
                                digest, copied, kind, preview, preview_note, now,
                            ),
                        )
                        conn.execute(
                            "UPDATE sessions SET updated_at = ? WHERE id = ?",
                            (now, session_id),
                        )
                except Exception:
                    try:
                        final_path.unlink()
                    except OSError:
                        pass
                    raise
            finally:
                source_file.close()
                if temp_path is not None:
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass

        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM attachments WHERE id = ? AND session_id = ?",
                (attachment_id, session_id),
            ).fetchone()
        return self._attachment_dict(row, include_preview=True)

    def list_attachments(
        self,
        session_id: str,
        *,
        include_previews: bool = True,
    ) -> list[dict[str, Any]]:
        self._get_session(session_id)
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM attachments
                WHERE session_id = ?
                ORDER BY created_at, id
                """,
                (session_id,),
            ).fetchall()
        return [
            self._attachment_dict(row, include_preview=include_previews)
            for row in rows
        ]

    # ── public chat APIs ─────────────────────────────────────────────────
    def outbound_preview(
        self,
        session_id: str,
        text: str,
        *,
        attachment_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Return the exact path-free message payload that a send would expose.

        This method is read-only: it does not insert a pending message, obtain a
        credential, or call the configured transport.  The UI can therefore show
        informed consent before the external operation begins.
        """
        self._get_session(session_id)
        if not isinstance(text, str) or not text.strip():
            raise AssistantChatError("Message must not be empty")
        if len(text) > MAX_MESSAGE_CHARS:
            raise AssistantChatError(
                f"Message exceeds the {MAX_MESSAGE_CHARS}-character limit"
            )
        selected = self._validate_attachment_ids(session_id, attachment_ids or ())
        config = self._load_chat_config()
        messages = self._model_messages_preview(
            session_id,
            text=text,
            attachment_ids=selected,
            history_limit=config["history_messages"],
            system_prompt=config["system_prompt"],
        )
        parsed = urllib.parse.urlsplit(config["url"])
        destination = f"{parsed.scheme}://{parsed.hostname or ''}"
        if parsed.port is not None:
            destination += f":{parsed.port}"
        return {
            "schema": "vcstudio.ai-outbound-preview/v1",
            "external": True,
            "destination": destination,
            "model": config["model"],
            "messages": messages,
            "message_count": len(messages),
            "character_count": sum(len(item["content"]) for item in messages),
            "selected_attachment_count": len(selected),
        }

    def send(
        self,
        session_id: str,
        text: str,
        *,
        attachment_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Persist a user turn and obtain one OpenAI-compatible response."""
        self._get_session(session_id)
        if not isinstance(text, str) or not text.strip():
            raise AssistantChatError("Message must not be empty")
        if len(text) > MAX_MESSAGE_CHARS:
            raise AssistantChatError(
                f"Message exceeds the {MAX_MESSAGE_CHARS}-character limit"
            )

        command = text.strip().casefold()
        if command == "/help":
            return self._local_exchange(session_id, text, HELP_TEXT)
        if command == "/stop":
            stopped = self.stop(session_id)
            content = STOPPED_TEXT if stopped["stopped"] else NOT_RUNNING_TEXT
            return self._local_exchange(session_id, text, content)

        selected = self._validate_attachment_ids(session_id, attachment_ids or ())
        request_id = uuid.uuid4().hex
        cancel_event = threading.Event()
        with self._active_lock:
            if session_id in self._active:
                raise SessionBusy("This session already has an active AI response")
            self._active[session_id] = (request_id, cancel_event)

        user_message_id: int | None = None
        try:
            user_message_id = self._insert_user_message(
                session_id, text, selected, request_id
            )
            config = self._load_chat_config()
            messages = self._model_messages(
                session_id,
                current_message_id=user_message_id,
                history_limit=config["history_messages"],
                system_prompt=config["system_prompt"],
            )
            if cancel_event.is_set():
                return self._finish_cancelled(
                    session_id, request_id, user_message_id
                )

            body: dict[str, Any] = {
                "model": config["model"],
                "messages": messages,
                "stream": False,
                "temperature": config["temperature"],
            }
            if config["max_tokens"] is not None:
                body["max_tokens"] = config["max_tokens"]

            key = self._key_loader()
            headers = {"Content-Type": "application/json"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            elif config["requires_api_key"]:
                raise ChatConfigurationError("No API key is configured")

            transport_args = (
                config["url"],
                json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers,
                config["timeout"],
            )
            if self._transport_accepts_cancel:
                raw_response = self._transport(*transport_args, cancel_event)
            else:
                # Compatibility with the repository's existing four-argument
                # HTTP seams.  Late output is still suppressed locally.
                raw_response = self._transport(*transport_args)
            if cancel_event.is_set():
                return self._finish_cancelled(
                    session_id, request_id, user_message_id
                )
            content = _response_content(raw_response)
            if not content.strip():
                raise ChatTransportError("The model returned an empty response")

            # Holding the active lock makes the final cancellation check and
            # persistence atomic with respect to stop().
            with self._active_lock:
                active = self._active.get(session_id)
                if (
                    active is None
                    or active[0] != request_id
                    or cancel_event.is_set()
                ):
                    return self._finish_cancelled_locked(
                        session_id, request_id, user_message_id
                    )
                assistant_message = self._persist_model_response(
                    session_id, request_id, user_message_id, content
                )
                self._active.pop(session_id, None)
            return {
                "status": "ok",
                "session_id": session_id,
                "request_id": request_id,
                "content": content,
                "message": assistant_message,
            }
        except Exception:
            with self._active_lock:
                active = self._active.get(session_id)
                was_cancelled = cancel_event.is_set()
                if active and active[0] == request_id:
                    self._active.pop(session_id, None)
            if user_message_id is not None:
                self._set_message_status(
                    user_message_id, "cancelled" if was_cancelled else "error"
                )
            if was_cancelled:
                return {
                    "status": "cancelled",
                    "session_id": session_id,
                    "request_id": request_id,
                    "content": "",
                    "message": None,
                }
            raise

    def stop(self, session_id: str) -> dict[str, Any]:
        """Signal the active request, if any, without waiting for transport."""
        self._get_session(session_id)
        with self._active_lock:
            active = self._active.get(session_id)
            if active is None:
                return {"session_id": session_id, "stopped": False, "request_id": None}
            request_id, event = active
            event.set()
            return {
                "session_id": session_id,
                "stopped": True,
                "request_id": request_id,
            }

    def local_reply(self, session_id: str, user_text: str, assistant_text: str) -> dict[str, Any]:
        """Persist one deterministic local tool exchange without contacting a model."""
        if not isinstance(user_text, str) or not user_text.strip():
            raise AssistantChatError("Message must not be empty")
        if not isinstance(assistant_text, str) or not assistant_text.strip():
            raise AssistantChatError("Local reply must not be empty")
        return self._local_exchange(session_id, user_text, assistant_text)

    # ── database and workspace internals ─────────────────────────────────
    def _initialize_database(self) -> None:
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    workspace_name TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS attachments (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    original_name TEXT NOT NULL,
                    stored_name TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
                    kind TEXT NOT NULL,
                    preview TEXT,
                    preview_note TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, stored_name)
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK(status IN ('pending', 'complete', 'cancelled', 'error')),
                    source TEXT NOT NULL CHECK(source IN ('model', 'local')),
                    request_id TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS message_attachments (
                    message_id INTEGER NOT NULL
                        REFERENCES messages(id) ON DELETE CASCADE,
                    attachment_id TEXT NOT NULL
                        REFERENCES attachments(id) ON DELETE RESTRICT,
                    PRIMARY KEY(message_id, attachment_id)
                );

                CREATE INDEX IF NOT EXISTS idx_messages_session
                    ON messages(session_id, id);
                CREATE INDEX IF NOT EXISTS idx_attachments_session
                    ON attachments(session_id, created_at);
                PRAGMA user_version = 1;
                """
            )
        _tighten_file_permissions(self.db_path)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _ensure_private_dir(path: Path) -> None:
        if path.is_symlink():
            raise AssistantChatError(f"Workspace directory must not be a symbolic link: {path.name}")
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir() or path.is_symlink():
            raise AssistantChatError(f"Unsafe workspace directory: {path.name}")
        try:
            path.chmod(0o700)
        except OSError:
            pass

    def _get_session(self, session_id: str) -> dict[str, Any]:
        if not isinstance(session_id, str) or not session_id:
            raise SessionNotFound("Session does not exist")
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT id, title, workspace_name, created_at, updated_at
                FROM sessions WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            raise SessionNotFound("Session does not exist")
        return dict(row)

    def _attachment_dir(self, workspace_name: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", workspace_name):
            raise AssistantChatError("Invalid session workspace identifier")
        workspace = self.sessions_root / workspace_name
        attachment_dir = workspace / "attachments"
        expected = self.sessions_root.resolve()
        if expected not in attachment_dir.resolve().parents:
            raise AssistantChatError("Session workspace escaped its configured root")
        self._ensure_private_dir(workspace)
        self._ensure_private_dir(attachment_dir)
        return attachment_dir

    @staticmethod
    def _open_regular_source(source: Path):
        flags = os.O_RDONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(source, flags)
        except OSError as exc:
            raise AttachmentRejected(
                "Attachment must be a readable regular file and not a symbolic link"
            ) from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise AttachmentRejected("Attachment must be a regular file")
            if source.is_symlink():
                raise AttachmentRejected("Symbolic-link attachments are not allowed")
            if info.st_size > MAX_FILE_BYTES:
                raise AttachmentRejected(
                    f"Attachment exceeds {MAX_FILE_BYTES // (1024 * 1024)} MB"
                )
            return os.fdopen(fd, "rb", closefd=True), int(info.st_size)
        except Exception:
            os.close(fd)
            raise

    @staticmethod
    def _stream_copy(source_file, destination: Path) -> tuple[str, int]:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(destination, flags, 0o600)
        digest = hashlib.sha256()
        total = 0
        try:
            with os.fdopen(fd, "wb", closefd=True) as output:
                while True:
                    chunk = source_file.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_FILE_BYTES:
                        raise AttachmentRejected(
                            f"Attachment exceeds {MAX_FILE_BYTES // (1024 * 1024)} MB"
                        )
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                try:
                    os.fsync(output.fileno())
                except OSError:
                    pass
        except Exception:
            try:
                destination.unlink()
            except OSError:
                pass
            raise
        return digest.hexdigest(), total

    def _attachment_dict(
        self,
        row: sqlite3.Row,
        *,
        include_preview: bool,
    ) -> dict[str, Any]:
        item = {
            "id": row["id"],
            "session_id": row["session_id"],
            "name": row["original_name"],
            "stored_name": row["stored_name"],
            "sha256": row["sha256"],
            "size_bytes": int(row["size_bytes"]),
            "kind": row["kind"],
            "preview_note": row["preview_note"],
            "created_at": row["created_at"],
        }
        if include_preview:
            item["preview"] = row["preview"]
        return item

    def _message_attachments(
        self,
        conn: sqlite3.Connection,
        message_id: int,
        *,
        include_preview: bool,
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT a.*
            FROM attachments AS a
            JOIN message_attachments AS ma ON ma.attachment_id = a.id
            WHERE ma.message_id = ?
            ORDER BY a.created_at, a.id
            """,
            (message_id,),
        ).fetchall()
        return [
            self._attachment_dict(row, include_preview=include_preview)
            for row in rows
        ]

    # ── message and request internals ────────────────────────────────────
    def _validate_attachment_ids(
        self,
        session_id: str,
        attachment_ids: Sequence[str],
    ) -> list[str]:
        selected = list(dict.fromkeys(attachment_ids))
        if len(selected) > MAX_ATTACHMENTS_PER_MESSAGE:
            raise AttachmentRejected(
                f"At most {MAX_ATTACHMENTS_PER_MESSAGE} attachments may accompany one message"
            )
        if not selected:
            return []
        if any(not isinstance(item, str) or not item for item in selected):
            raise AttachmentRejected("Invalid attachment identifier")
        placeholders = ",".join("?" for _ in selected)
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT id FROM attachments
                WHERE session_id = ? AND id IN ({placeholders})
                """,
                (session_id, *selected),
            ).fetchall()
        found = {row["id"] for row in rows}
        if found != set(selected):
            raise AttachmentRejected("An attachment does not belong to this session")
        return selected

    def _insert_user_message(
        self,
        session_id: str,
        text: str,
        attachment_ids: list[str],
        request_id: str,
    ) -> int:
        now = _utc_now()
        with self._connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO messages
                    (session_id, role, content, status, source, request_id, created_at)
                VALUES (?, 'user', ?, 'pending', 'model', ?, ?)
                """,
                (session_id, text, request_id, now),
            )
            message_id = int(cursor.lastrowid)
            conn.executemany(
                """
                INSERT INTO message_attachments(message_id, attachment_id)
                VALUES (?, ?)
                """,
                [(message_id, item) for item in attachment_ids],
            )
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
        return message_id

    def _local_exchange(
        self,
        session_id: str,
        user_text: str,
        assistant_text: str,
    ) -> dict[str, Any]:
        request_id = uuid.uuid4().hex
        now = _utc_now()
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO messages
                    (session_id, role, content, status, source, request_id, created_at)
                VALUES (?, 'user', ?, 'complete', 'local', ?, ?)
                """,
                (session_id, user_text, request_id, now),
            )
            cursor = conn.execute(
                """
                INSERT INTO messages
                    (session_id, role, content, status, source, request_id, created_at)
                VALUES (?, 'assistant', ?, 'complete', 'local', ?, ?)
                """,
                (session_id, assistant_text, request_id, now),
            )
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
            message_id = int(cursor.lastrowid)
        return {
            "status": "local",
            "session_id": session_id,
            "request_id": request_id,
            "content": assistant_text,
            "message": self._message_by_id(message_id),
        }

    def _model_messages(
        self,
        session_id: str,
        *,
        current_message_id: int,
        history_limit: int,
        system_prompt: str,
    ) -> list[dict[str, str]]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT id, role, content
                FROM messages
                WHERE session_id = ?
                  AND source = 'model'
                  AND (
                      status = 'complete'
                      OR (id = ? AND status = 'pending')
                  )
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, current_message_id, history_limit),
            ).fetchall()
            rows = list(reversed(rows))
            remote_attachment_chars = 0
            result: list[dict[str, str]] = [
                {"role": "system", "content": system_prompt}
            ]
            for row in rows:
                content = row["content"]
                if row["role"] == "user":
                    attachments = self._message_attachments(
                        conn, int(row["id"]), include_preview=True
                    )
                    content, remote_attachment_chars = _content_with_attachments(
                        content, attachments, remote_attachment_chars
                    )
                result.append({"role": row["role"], "content": content})
        return result

    def _model_messages_preview(
        self,
        session_id: str,
        *,
        text: str,
        attachment_ids: list[str],
        history_limit: int,
        system_prompt: str,
    ) -> list[dict[str, str]]:
        """Mirror ``_model_messages`` with one virtual, unpersisted user turn."""
        prior_limit = max(0, history_limit - 1)
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT id, role, content
                FROM messages
                WHERE session_id = ? AND source = 'model' AND status = 'complete'
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, prior_limit),
            ).fetchall()
            rows = list(reversed(rows))
            result: list[dict[str, str]] = [
                {"role": "system", "content": system_prompt}
            ]
            remote_attachment_chars = 0
            for row in rows:
                content = row["content"]
                if row["role"] == "user":
                    attachments = self._message_attachments(
                        conn, int(row["id"]), include_preview=True
                    )
                    content, remote_attachment_chars = _content_with_attachments(
                        content, attachments, remote_attachment_chars
                    )
                result.append({"role": row["role"], "content": content})

            attachments: list[dict[str, Any]] = []
            if attachment_ids:
                placeholders = ",".join("?" for _ in attachment_ids)
                attachment_rows = conn.execute(
                    f"SELECT * FROM attachments WHERE session_id = ? "
                    f"AND id IN ({placeholders})",
                    (session_id, *attachment_ids),
                ).fetchall()
                by_id = {row["id"]: row for row in attachment_rows}
                attachments = [
                    self._attachment_dict(by_id[item], include_preview=True)
                    for item in attachment_ids
                ]
            content, _remote_attachment_chars = _content_with_attachments(
                text, attachments, remote_attachment_chars
            )
            result.append({"role": "user", "content": content})
        return result

    def _load_chat_config(self) -> dict[str, Any]:
        try:
            raw = self._config_loader() or {}
        except Exception as exc:
            raise ChatConfigurationError("Unable to load chat configuration") from exc
        if not isinstance(raw, dict):
            raise ChatConfigurationError("Chat configuration must be a mapping")
        if isinstance(raw.get("llm"), dict):
            raw = raw["llm"]

        base_url = str(raw.get("base_url") or "").strip()
        model = str(raw.get("model") or "").strip()
        raw_system_prompt = raw.get("system_prompt")
        system_prompt = (
            raw_system_prompt.strip()
            if isinstance(raw_system_prompt, str) and raw_system_prompt.strip()
            else DEFAULT_SYSTEM_PROMPT
        )
        if not base_url:
            raise ChatConfigurationError("No OpenAI-compatible base URL is configured")
        if not model:
            raise ChatConfigurationError("No chat model is configured")
        temperature = raw.get("temperature", 0.2)
        if not isinstance(temperature, (int, float)) or not 0 <= temperature <= 2:
            temperature = 0.2
        max_tokens = raw.get("max_tokens")
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
            max_tokens = None
        elif max_tokens > 100_000:
            max_tokens = 100_000
        return {
            "url": _chat_url(base_url),
            "model": model,
            "temperature": float(temperature),
            "max_tokens": max_tokens,
            "timeout": _bounded_int(raw.get("timeout"), 1, 600, self._timeout),
            "history_messages": _bounded_int(
                raw.get("history_messages"), 1, 200, DEFAULT_HISTORY_MESSAGES
            ),
            "requires_api_key": raw.get("requires_api_key", True) is not False,
            "system_prompt": system_prompt,
        }

    def _persist_model_response(
        self,
        session_id: str,
        request_id: str,
        user_message_id: int,
        content: str,
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._connection() as conn:
            conn.execute(
                "UPDATE messages SET status = 'complete' WHERE id = ?",
                (user_message_id,),
            )
            cursor = conn.execute(
                """
                INSERT INTO messages
                    (session_id, role, content, status, source, request_id, created_at)
                VALUES (?, 'assistant', ?, 'complete', 'model', ?, ?)
                """,
                (session_id, content, request_id, now),
            )
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
            message_id = int(cursor.lastrowid)
        return self._message_by_id(message_id)

    def _finish_cancelled(
        self,
        session_id: str,
        request_id: str,
        user_message_id: int,
    ) -> dict[str, Any]:
        with self._active_lock:
            return self._finish_cancelled_locked(
                session_id, request_id, user_message_id
            )

    def _finish_cancelled_locked(
        self,
        session_id: str,
        request_id: str,
        user_message_id: int,
    ) -> dict[str, Any]:
        self._set_message_status(user_message_id, "cancelled")
        active = self._active.get(session_id)
        if active and active[0] == request_id:
            self._active.pop(session_id, None)
        return {
            "status": "cancelled",
            "session_id": session_id,
            "request_id": request_id,
            "content": "",
            "message": None,
        }

    def _set_message_status(self, message_id: int, status_value: str) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE messages SET status = ? WHERE id = ?",
                (status_value, message_id),
            )

    def _message_by_id(self, message_id: int) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT id, role, content, status, source, request_id, created_at
                FROM messages WHERE id = ?
                """,
                (message_id,),
            ).fetchone()
            if row is None:
                raise AssistantChatError("Message persistence failed")
            attachments = self._message_attachments(
                conn, message_id, include_preview=True
            )
        return {
            "id": int(row["id"]),
            "role": row["role"],
            "content": row["content"],
            "status": row["status"],
            "source": row["source"],
            "request_id": row["request_id"],
            "created_at": row["created_at"],
            "attachments": attachments,
        }


def _default_transport(
    url: str,
    body: bytes,
    headers: dict[str, str],
    timeout: int,
    cancel_event: threading.Event,
):
    if cancel_event.is_set():
        raise ChatTransportError("Chat request was cancelled")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise ChatTransportError("Unable to reach the chat service") from exc


def _response_content(response: Any) -> str:
    status = 200
    payload: Any = response
    if isinstance(response, tuple) and len(response) == 2:
        status, payload = response
    elif hasattr(response, "status_code"):
        status = int(response.status_code)
        try:
            payload = response.json()
        except Exception:
            payload = getattr(response, "content", b"")

    try:
        status = int(status)
    except (TypeError, ValueError) as exc:
        raise ChatTransportError("Chat service returned an invalid status") from exc
    if not 200 <= status < 300:
        # Do not surface or persist the response body: gateways occasionally
        # echo request headers, and those can contain the API key.
        raise ChatTransportError(f"Chat service returned HTTP {status}")

    if isinstance(payload, bytes):
        try:
            payload = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ChatTransportError("Chat service returned invalid JSON") from exc
    elif isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ChatTransportError("Chat service returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ChatTransportError("Chat service returned an invalid response")

    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        direct = payload.get("output_text")
        if isinstance(direct, str):
            return direct
        raise ChatTransportError("Chat service response has no assistant message") from exc
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        if parts:
            return "".join(parts)
    raise ChatTransportError("Chat service returned unsupported message content")


def _accepts_cancel_event(transport: Callable[..., Any]) -> bool:
    """Inspect once so a four-argument transport is never retried after I/O."""
    try:
        signature = inspect.signature(transport)
    except (TypeError, ValueError):
        return True
    try:
        signature.bind("", b"", {}, 1, threading.Event())
        return True
    except TypeError:
        try:
            signature.bind("", b"", {}, 1)
        except TypeError as exc:
            raise ChatConfigurationError(
                "Transport must accept (url, body, headers, timeout[, cancel_event])"
            ) from exc
        return False


def _build_preview(path: Path, original_name: str) -> tuple[str, str | None, str | None]:
    suffix = Path(original_name).suffix.casefold()
    with path.open("rb") as handle:
        signature = handle.read(8)
    if suffix == ".zip" or signature.startswith(b"PK\x03\x04"):
        return _zip_preview(path)
    if suffix == ".pdf" or signature.startswith(b"%PDF-"):
        return _pdf_preview(path)
    if original_name.upper() in _VASP_NAMES or suffix in _TEXT_EXTENSIONS:
        return _text_preview(path, "vasp" if original_name.upper() in _VASP_NAMES else "text")

    with path.open("rb") as handle:
        sample = handle.read(MAX_PREVIEW_BYTES)
    if _looks_like_text(sample):
        return _decoded_preview(sample, path.stat().st_size, "text")
    return "binary", None, "Binary content kept local; metadata only."


def _text_preview(path: Path, kind: str) -> tuple[str, str | None, str | None]:
    with path.open("rb") as handle:
        sample = handle.read(MAX_PREVIEW_BYTES)
    if b"\x00" in sample:
        return "binary", None, "File contains binary data; metadata only."
    return _decoded_preview(sample, path.stat().st_size, kind)


def _decoded_preview(
    sample: bytes,
    full_size: int,
    kind: str,
) -> tuple[str, str | None, str | None]:
    try:
        text = sample.decode("utf-8-sig")
        note = None
    except UnicodeDecodeError:
        text = sample.decode("utf-8", errors="replace")
        note = "Preview contains replacement characters for undecodable bytes."
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    truncated = full_size > len(sample) or len(text) > MAX_PREVIEW_CHARS
    text = text[:MAX_PREVIEW_CHARS]
    if truncated:
        note = (note + " " if note else "") + "Preview truncated locally."
    return kind, text, note


def _looks_like_text(sample: bytes) -> bool:
    if not sample:
        return True
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        printable = sum(
            byte in b"\n\r\t\f\b" or 32 <= byte <= 126
            for byte in sample
        )
        return printable / len(sample) >= 0.85


def _pdf_preview(path: Path) -> tuple[str, str | None, str | None]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return "pdf", None, "PDF metadata imported; install optional pypdf for text preview."
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            return "pdf", None, "Encrypted PDF imported; text preview unavailable."
        pages = []
        for page in reader.pages[:5]:
            pages.append(page.extract_text() or "")
            if sum(len(item) for item in pages) >= MAX_PREVIEW_CHARS:
                break
        text = "\n\n".join(pages)[:MAX_PREVIEW_CHARS]
        note_parts = []
        if len(reader.pages) > 5 or len("\n\n".join(pages)) > MAX_PREVIEW_CHARS:
            note_parts.append("PDF preview limited to the first pages/characters.")
        if not text.strip():
            note_parts.append("No extractable PDF text was found.")
        return "pdf", text or None, " ".join(note_parts) or None
    except Exception:
        return "pdf", None, "PDF imported, but its text preview could not be read safely."


def _zip_preview(path: Path) -> tuple[str, str | None, str | None]:
    try:
        with zipfile.ZipFile(path, "r") as archive:
            members = archive.infolist()
            if len(members) > MAX_ZIP_MEMBERS:
                raise AttachmentRejected(
                    f"ZIP contains more than {MAX_ZIP_MEMBERS} entries"
                )
            total_size = 0
            total_name_chars = 0
            inventory = []
            for member in members:
                name = member.filename
                total_name_chars += len(name)
                if total_name_chars > MAX_ZIP_NAME_CHARS:
                    raise AttachmentRejected("ZIP member names exceed the safe inventory limit")
                if not _safe_zip_name(name):
                    raise AttachmentRejected("ZIP contains an unsafe traversal or absolute path")
                mode = (member.external_attr >> 16) & 0o170000
                if mode and stat.S_ISLNK(mode):
                    raise AttachmentRejected("ZIP symbolic-link entries are not allowed")
                if member.file_size > MAX_ZIP_MEMBER_BYTES:
                    raise AttachmentRejected("ZIP member exceeds the safe expanded-size limit")
                total_size += member.file_size
                if total_size > MAX_ZIP_UNCOMPRESSED_BYTES:
                    raise AttachmentRejected("ZIP expanded size exceeds the safe limit")
                if member.file_size:
                    if member.compress_size <= 0:
                        raise AttachmentRejected("ZIP contains an unsafe compression ratio")
                    ratio = member.file_size / member.compress_size
                    if ratio > MAX_ZIP_COMPRESSION_RATIO:
                        raise AttachmentRejected("ZIP contains a possible compression bomb")
                if len(inventory) < ZIP_PREVIEW_MEMBERS:
                    inventory.append(
                        {
                            "name": name,
                            "size_bytes": int(member.file_size),
                            "compressed_bytes": int(member.compress_size),
                            "directory": member.is_dir(),
                            "encrypted": bool(member.flag_bits & 0x1),
                        }
                    )
    except AttachmentRejected:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise AttachmentRejected("Attachment is not a readable, safe ZIP archive") from exc

    preview = json.dumps(
        {
            "entry_count": len(members),
            "uncompressed_bytes": total_size,
            "entries": inventory,
        },
        ensure_ascii=False,
        indent=2,
    )
    note = None
    if len(members) > len(inventory):
        note = f"ZIP inventory preview limited to {len(inventory)} entries; nothing was extracted."
    else:
        note = "ZIP inventory only; nothing was extracted."
    return "zip", preview[:MAX_PREVIEW_CHARS], note


def _safe_zip_name(name: str) -> bool:
    if not name or "\x00" in name:
        return False
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("//") or _DRIVE_PATH.match(normalized):
        return False
    path = PurePosixPath(normalized)
    return ".." not in path.parts


def _attachment_for_model(attachment: dict[str, Any]) -> str:
    metadata = (
        f"Attachment: {attachment['name']}\n"
        f"Type: {attachment['kind']}; size: {attachment['size_bytes']} bytes; "
        f"SHA256: {attachment['sha256']}"
    )
    preview = attachment.get("preview")
    if isinstance(preview, str) and preview:
        return metadata + "\nLocal bounded preview:\n" + preview[:MAX_PREVIEW_CHARS]
    note = attachment.get("preview_note") or "No content preview is available."
    return metadata + "\n" + note


def _content_with_attachments(
    content: str,
    attachments: Sequence[dict[str, Any]],
    remote_attachment_chars: int,
) -> tuple[str, int]:
    if not attachments:
        return content, remote_attachment_chars
    sections = []
    for attachment in attachments:
        section = _attachment_for_model(attachment)
        remaining = MAX_REMOTE_ATTACHMENT_CHARS - remote_attachment_chars
        if remaining <= 0:
            sections.append("[其余附件预览因本地隐私/上下文上限未发送]")
            break
        section = section[:remaining]
        remote_attachment_chars += len(section)
        sections.append(section)
    return (
        content
        + "\n\n[用户明确选择的本地附件预览]\n"
        + "\n\n".join(sections),
        remote_attachment_chars,
    )


def _stored_name(original_name: str, digest: str, *, token: str | None = None) -> str:
    normalized = unicodedata.normalize("NFKC", original_name)
    path_name = Path(normalized).name
    suffix = Path(path_name).suffix.casefold()
    suffix = re.sub(r"[^a-z0-9.]+", "", suffix)[:12]
    stem = path_name[:-len(Path(path_name).suffix)] if Path(path_name).suffix else path_name
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-_.")[:60] or "attachment"
    unique = (token or uuid.uuid4().hex)[:8]
    return f"{stem}-{digest[:12]}-{unique}{suffix}"


def _clean_title(title: str | None) -> str:
    if not isinstance(title, str) or not title.strip():
        return "New chat"
    clean = " ".join(title.replace("\x00", "").split())
    return clean[:120] or "New chat"


def _chat_url(base_url: str) -> str:
    url = base_url.rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    return url + "/chat/completions"


def _bounded_int(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return number if minimum <= number <= maximum else fallback


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _tighten_file_permissions(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


__all__ = [
    "AssistantChat",
    "AssistantChatError",
    "AttachmentRejected",
    "ChatConfigurationError",
    "ChatTransportError",
    "HELP_TEXT",
    "MAX_FILE_BYTES",
    "MAX_SESSION_BYTES",
    "SessionBusy",
    "SessionNotFound",
]
