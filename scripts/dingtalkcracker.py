#!/usr/bin/env python3
"""DingTalk adapter for WeComCracker.

Collects messages delivered to an explicitly authorized DingTalk Stream bot and
stores only normalized evidence fields in a local SQLite vault. Credentials,
session webhooks, download codes, and raw callback payloads are never persisted.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from importlib import metadata as importlib_metadata
import json
import os
from pathlib import Path
import platform
import re
import sqlite3
import sys
import threading
from typing import Iterable, Optional
from urllib.parse import urlparse
import uuid


MANIFEST_FORMAT = "dingtalk-chat-vault-v1"
SDK_DISTRIBUTION = "dingtalk-stream"
SDK_VERSION = "0.24.3"
STREAM_TOPIC = "/v1.0/im/bot/messages/get"
CLIENT_ID_ENV = "DINGTALK_CLIENT_ID"
CLIENT_SECRET_ENV = "DINGTALK_CLIENT_SECRET"
DATABASE_NAME = "dingtalk.db"
MANIFEST_NAME = "snapshot_manifest.json"
MAX_TIMESTAMP_SECONDS = 32_503_680_000


SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_table (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    conversation_type TEXT,
    last_message_time INTEGER,
    last_message_id TEXT
);

CREATE TABLE IF NOT EXISTS user_table (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_table (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL UNIQUE,
    conversation_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    content_type TEXT NOT NULL,
    send_time INTEGER NOT NULL,
    content TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversation_table(id),
    FOREIGN KEY (sender_id) REFERENCES user_table(id)
);

CREATE INDEX IF NOT EXISTS idx_message_conversation_sequence
ON message_table(conversation_id, sequence);

CREATE INDEX IF NOT EXISTS idx_message_send_time
ON message_table(send_time);
"""


@dataclass(frozen=True)
class NormalizedMessage:
    message_id: str
    conversation_id: str
    conversation_name: str
    conversation_type: str
    sender_id: str
    sender_name: str
    content_type: str
    send_time: int
    content: str
    source_digest: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp_to_utc_iso(value: object) -> Optional[str]:
    if value in (None, "", 0):
        return None
    numeric = float(value)
    while abs(numeric) > MAX_TIMESTAMP_SECONDS:
        numeric /= 1000
    return datetime.fromtimestamp(numeric, timezone.utc).isoformat()


def _normalize_timestamp(value: object) -> int:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("钉钉消息缺少有效的 createAt 时间戳") from error
    while abs(numeric) > MAX_TIMESTAMP_SECONDS:
        numeric /= 1000
    if numeric < 0 or numeric > MAX_TIMESTAMP_SECONDS:
        raise ValueError("钉钉消息 createAt 时间戳超出支持范围")
    return int(numeric)


def _redact_text(value: object) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.split()).strip()
    text = re.sub(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
        r"secret|password|cookie|authorization|signature|sessionwebhook|"
        r"downloadcode)\b(?:\s*[:=]\s*|\s+)[^\s,;]{8,}",
        lambda match: f"{match.group(1)}:[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)([?&](?:token|access_token|auth|key|signature|sig)(?:=|\s+))"
        r"[^&#\s]+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?<![A-Za-z0-9])(?:[A-Za-z0-9+/_-]{64,}={0,2})(?![A-Za-z0-9])",
        "[LONG_TOKEN_REDACTED]",
        text,
    )
    return text


def _message_content(payload: dict[str, object], content_type: str) -> str:
    if content_type == "text":
        text = payload.get("text")
        if isinstance(text, dict):
            return _redact_text(text.get("content")) or "[空文本消息]"

    if content_type == "richText":
        content = payload.get("content")
        rich_text = content.get("richText") if isinstance(content, dict) else None
        parts: list[str] = []
        if isinstance(rich_text, list):
            for item in rich_text:
                if not isinstance(item, dict):
                    continue
                if item.get("text") is not None:
                    cleaned = _redact_text(item.get("text"))
                    if cleaned:
                        parts.append(cleaned)
                elif item.get("downloadCode") is not None:
                    parts.append("[图片]")
        return " ".join(parts).strip() or "[富文本消息]"

    placeholders = {
        "picture": "[图片]",
        "audio": "[语音]",
        "video": "[视频]",
        "file": "[文件]",
    }
    return placeholders.get(content_type, f"[{content_type or 'unknown'} 消息]")


def normalize_dingtalk_message(payload: dict[str, object]) -> NormalizedMessage:
    if not isinstance(payload, dict):
        raise TypeError("钉钉回调 data 必须是 JSON 对象")

    message_id = str(payload.get("msgId") or payload.get("messageId") or "").strip()
    if not message_id:
        raise ValueError("钉钉消息缺少 msgId")

    sender_id = str(
        payload.get("senderStaffId") or payload.get("senderId") or ""
    ).strip()
    if not sender_id:
        raise ValueError(f"钉钉消息 {message_id} 缺少发送者 ID")

    sender_name = _redact_text(
        payload.get("senderNick") or payload.get("sender") or sender_id
    ) or sender_id
    conversation_type = str(payload.get("conversationType") or "unknown").strip()
    conversation_id = str(payload.get("conversationId") or "").strip()
    if not conversation_id and conversation_type == "1":
        conversation_id = f"private:{sender_id}"
    if not conversation_id:
        raise ValueError(f"钉钉消息 {message_id} 缺少 conversationId")

    default_name = sender_name if conversation_type == "1" else conversation_id
    conversation_name = _redact_text(
        payload.get("conversationTitle") or default_name
    ) or default_name
    content_type = str(payload.get("msgtype") or "unknown").strip()
    send_time = _normalize_timestamp(payload.get("createAt"))
    content = _message_content(payload, content_type)

    digest_source = {
        "message_id": message_id,
        "conversation_id": conversation_id,
        "sender_id": sender_id,
        "content_type": content_type,
        "send_time": send_time,
        "content": content,
    }
    source_digest = hashlib.sha256(
        json.dumps(
            digest_source,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    return NormalizedMessage(
        message_id=message_id,
        conversation_id=conversation_id,
        conversation_name=conversation_name,
        conversation_type=conversation_type,
        sender_id=sender_id,
        sender_name=sender_name,
        content_type=content_type,
        send_time=send_time,
        content=content,
        source_digest=source_digest,
    )


def _connect_writable(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def _connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    uri = f"{path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def _write_json_atomic(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _initial_manifest() -> dict[str, object]:
    created = _utc_now()
    return {
        "format": MANIFEST_FORMAT,
        "source": {
            "system": "dingtalk",
            "mode": "bot_stream",
            "topic": STREAM_TOPIC,
        },
        "created_utc": created,
        "updated_utc": created,
        "complete": False,
        "coverage": {
            "scope": "messages_delivered_to_this_bot",
            "history_backfill": False,
            "warning": (
                "仅包含钉钉实际投递给此机器人的消息；不代表账号或群聊的全部历史"
            ),
        },
        "credentials_persisted": False,
        "raw_callbacks_persisted": False,
        "sdk": {"distribution": SDK_DISTRIBUTION, "tested_version": SDK_VERSION},
        "message_count": 0,
        "conversation_count": 0,
        "user_count": 0,
        "first_send_time_utc": None,
        "last_send_time_utc": None,
    }


def _initialize_vault(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    connection = _connect_writable(directory / DATABASE_NAME)
    try:
        connection.executescript(SCHEMA)
        connection.commit()
    finally:
        connection.close()
    try:
        os.chmod(directory / DATABASE_NAME, 0o600)
    except OSError:
        pass
    _write_json_atomic(directory / MANIFEST_NAME, _initial_manifest())


def _validate_vault(directory: Path) -> None:
    manifest_path = directory / MANIFEST_NAME
    database_path = directory / DATABASE_NAME
    if not manifest_path.is_file() or not database_path.is_file():
        raise ValueError(f"目录不是有效的钉钉消息库：{directory}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取钉钉消息库清单：{directory}") from error
    if manifest.get("format") != MANIFEST_FORMAT:
        raise ValueError(f"钉钉消息库格式不受支持：{manifest.get('format')!r}")
    source = manifest.get("source")
    if not isinstance(source, dict) or source.get("system") != "dingtalk":
        raise ValueError("消息库来源不是 dingtalk")


def ensure_vault(path_value: str) -> Path:
    requested = Path(path_value).expanduser()
    if requested.is_symlink():
        raise ValueError("拒绝将符号链接作为钉钉消息库目录")
    directory = requested.resolve()
    if directory.exists():
        if not directory.is_dir():
            raise NotADirectoryError(directory)
        if not any(directory.iterdir()):
            directory.rmdir()
            _initialize_vault(directory)
        else:
            _validate_vault(directory)
    else:
        directory.parent.mkdir(parents=True, exist_ok=True)
        _initialize_vault(directory)
    return directory


class VaultWriter:
    def __init__(self, directory: Path) -> None:
        _validate_vault(directory)
        self.directory = directory
        self.connection = _connect_writable(directory / DATABASE_NAME)

    def ingest(self, payload: dict[str, object]) -> bool:
        message = normalize_dingtalk_message(payload)
        existing = self.connection.execute(
            "SELECT source_digest FROM message_table WHERE message_id = ?",
            (message.message_id,),
        ).fetchone()
        if existing is not None:
            if existing["source_digest"] != message.source_digest:
                raise ValueError(
                    f"同一 msgId 的内容发生冲突：{message.message_id}"
                )
            return False

        self.connection.execute(
            """
            INSERT INTO user_table(id, name) VALUES (?, ?)
            ON CONFLICT(id) DO UPDATE SET name = excluded.name
            """,
            (message.sender_id, message.sender_name),
        )
        self.connection.execute(
            """
            INSERT INTO conversation_table(
                id, name, conversation_type, last_message_time, last_message_id
            ) VALUES (?, ?, ?, NULL, NULL)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                conversation_type = excluded.conversation_type
            """,
            (
                message.conversation_id,
                message.conversation_name,
                message.conversation_type,
            ),
        )
        self.connection.execute(
            """
            INSERT INTO message_table(
                message_id, conversation_id, sender_id, content_type,
                send_time, content, source_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.message_id,
                message.conversation_id,
                message.sender_id,
                message.content_type,
                message.send_time,
                message.content,
                message.source_digest,
            ),
        )
        self.connection.execute(
            """
            UPDATE conversation_table
            SET last_message_time = ?, last_message_id = ?
            WHERE id = ?
              AND (last_message_time IS NULL OR last_message_time <= ?)
            """,
            (
                message.send_time,
                message.message_id,
                message.conversation_id,
                message.send_time,
            ),
        )
        return True

    def commit(self) -> None:
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()

    def close(self) -> None:
        self.connection.close()


def _refresh_manifest(directory: Path) -> dict[str, object]:
    manifest_path = directory / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    connection = _connect_readonly(directory / DATABASE_NAME)
    try:
        message_count, first, last = connection.execute(
            "SELECT COUNT(*), MIN(send_time), MAX(send_time) FROM message_table"
        ).fetchone()
        conversation_count = connection.execute(
            "SELECT COUNT(*) FROM conversation_table"
        ).fetchone()[0]
        user_count = connection.execute("SELECT COUNT(*) FROM user_table").fetchone()[0]
    finally:
        connection.close()
    manifest.update(
        {
            "updated_utc": _utc_now(),
            "message_count": int(message_count),
            "conversation_count": int(conversation_count),
            "user_count": int(user_count),
            "first_send_time_utc": _timestamp_to_utc_iso(first),
            "last_send_time_utc": _timestamp_to_utc_iso(last),
        }
    )
    _write_json_atomic(manifest_path, manifest)
    return manifest


def _snapshot_status(directory: Path) -> dict[str, object]:
    _validate_vault(directory)
    manifest = json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8"))
    return {
        "format": manifest.get("format"),
        "source": manifest.get("source"),
        "complete": False,
        "coverage": manifest.get("coverage"),
        "created_utc": manifest.get("created_utc"),
        "updated_utc": manifest.get("updated_utc"),
        "message_count": manifest.get("message_count"),
        "warning": "钉钉消息库只覆盖实际投递给此机器人的消息",
    }


def _extract_callback_payload(record: dict[str, object]) -> dict[str, object]:
    if record.get("type") == "CALLBACK" or (
        isinstance(record.get("headers"), dict) and "data" in record
    ):
        headers = record.get("headers")
        if isinstance(headers, dict):
            topic = headers.get("topic")
            if topic and topic != STREAM_TOPIC:
                raise ValueError(f"不支持的钉钉 Stream topic：{topic}")
        data = record.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError as error:
                raise ValueError("钉钉回调 data 不是有效 JSON") from error
        if not isinstance(data, dict):
            raise ValueError("钉钉回调 data 必须是 JSON 对象")
        return data
    return record


def _iter_jsonl(path: Path) -> Iterable[tuple[int, dict[str, object]]]:
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"第 {line_number} 行不是有效 JSON") from error
            if not isinstance(value, dict):
                raise ValueError(f"第 {line_number} 行必须是 JSON 对象")
            yield line_number, _extract_callback_payload(value)


def import_jsonl(args: argparse.Namespace) -> int:
    input_path = Path(args.input).expanduser().resolve()
    if not input_path.is_file():
        raise FileNotFoundError(input_path)
    directory = ensure_vault(args.vault_dir)
    writer = VaultWriter(directory)
    inserted = 0
    duplicates = 0
    try:
        for line_number, payload in _iter_jsonl(input_path):
            try:
                created = writer.ingest(payload)
            except Exception as error:
                raise ValueError(
                    f"导入第 {line_number} 行失败：{type(error).__name__}: {error}"
                ) from error
            if created:
                inserted += 1
            else:
                duplicates += 1
        writer.commit()
    except Exception:
        writer.rollback()
        raise
    finally:
        writer.close()
    manifest = _refresh_manifest(directory)
    print(
        json.dumps(
            {
                "success": True,
                "source": "dingtalk",
                "inserted": inserted,
                "duplicates": duplicates,
                "message_count": manifest["message_count"],
                "vault_dir": str(directory),
                "manifest": str(directory / MANIFEST_NAME),
                "complete": False,
                "coverage": "messages_delivered_to_this_bot",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _sdk_version() -> Optional[str]:
    try:
        return importlib_metadata.version(SDK_DISTRIBUTION)
    except importlib_metadata.PackageNotFoundError:
        return None


def _load_sdk():
    try:
        import dingtalk_stream  # type: ignore
    except ImportError as error:
        raise RuntimeError(
            "缺少钉钉官方 SDK；请运行 pip install -r requirements-dingtalk.txt"
        ) from error
    return dingtalk_stream


def _credentials() -> tuple[str, str]:
    client_id = os.environ.get(CLIENT_ID_ENV, "").strip()
    client_secret = os.environ.get(CLIENT_SECRET_ENV, "").strip()
    if not client_id or not client_secret:
        raise RuntimeError(
            f"请通过环境变量 {CLIENT_ID_ENV} 和 {CLIENT_SECRET_ENV} 提供钉钉应用凭据"
        )
    return client_id, client_secret


def doctor(args: argparse.Namespace) -> int:
    installed_version = _sdk_version()
    client_id_present = bool(os.environ.get(CLIENT_ID_ENV, "").strip())
    client_secret_present = bool(os.environ.get(CLIENT_SECRET_ENV, "").strip())
    result: dict[str, object] = {
        "success": bool(
            installed_version and client_id_present and client_secret_present
        ),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "sdk": {
            "distribution": SDK_DISTRIBUTION,
            "installed": installed_version,
            "tested": SDK_VERSION,
        },
        "credentials": {
            CLIENT_ID_ENV: client_id_present,
            CLIENT_SECRET_ENV: client_secret_present,
            "persisted": False,
        },
        "live_connection": None,
    }
    if args.live:
        sdk = _load_sdk()
        client_id, client_secret = _credentials()
        credential = sdk.Credential(client_id, client_secret)
        client = sdk.DingTalkStreamClient(credential)
        connection = client.open_connection()
        if not connection:
            raise RuntimeError("钉钉 Stream 凭据或网络验证失败")
        endpoint = str(connection.get("endpoint") or "")
        result["live_connection"] = {
            "success": True,
            "endpoint_host": urlparse(endpoint).hostname,
            "ticket_persisted": False,
        }
        result["success"] = True
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["success"] else 1


def build_stream_handler(sdk, writer: VaultWriter, directory: Path):
    lock = threading.Lock()

    class CaptureHandler(sdk.CallbackHandler):
        async def process(self, callback):
            with lock:
                try:
                    inserted = writer.ingest(callback.data)
                    writer.commit()
                    manifest = _refresh_manifest(directory)
                    if inserted:
                        self.logger.info(
                            "captured DingTalk message; total=%s",
                            manifest.get("message_count"),
                        )
                    return sdk.AckMessage.STATUS_OK, "OK"
                except Exception:
                    writer.rollback()
                    self.logger.exception("failed to persist DingTalk message")
                    return sdk.AckMessage.STATUS_SYSTEM_EXCEPTION, "storage error"

    return CaptureHandler()


def collect_stream(args: argparse.Namespace) -> int:
    sdk = _load_sdk()
    client_id, client_secret = _credentials()
    directory = ensure_vault(args.vault_dir)
    writer = VaultWriter(directory)
    try:
        credential = sdk.Credential(client_id, client_secret)
        client = sdk.DingTalkStreamClient(credential)
        handler = build_stream_handler(sdk, writer, directory)
        client.register_callback_handler(STREAM_TOPIC, handler)
        print(
            json.dumps(
                {
                    "success": True,
                    "status": "starting",
                    "source": "dingtalk",
                    "topic": STREAM_TOPIC,
                    "vault_dir": str(directory),
                    "coverage": "messages_delivered_to_this_bot",
                    "connection_verified": False,
                    "credentials_persisted": False,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        client.start_forever()
    finally:
        writer.commit()
        writer.close()
        _refresh_manifest(directory)
    return 0


def inspect_vault(args: argparse.Namespace) -> int:
    directory = Path(args.vault_dir).expanduser().resolve()
    _validate_vault(directory)
    connection = _connect_readonly(directory / DATABASE_NAME)
    try:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        message_count, first, last = connection.execute(
            "SELECT COUNT(*), MIN(send_time), MAX(send_time) FROM message_table"
        ).fetchone()
        conversation_count = connection.execute(
            "SELECT COUNT(*) FROM conversation_table"
        ).fetchone()[0]
        user_count = connection.execute("SELECT COUNT(*) FROM user_table").fetchone()[0]
    finally:
        connection.close()
    print(
        json.dumps(
            {
                "success": quick_check == "ok",
                "directory": str(directory),
                "snapshot": _snapshot_status(directory),
                "database": {
                    "quick_check": quick_check,
                    "message_count": message_count,
                    "conversation_count": conversation_count,
                    "user_count": user_count,
                    "first_send_time_utc": _timestamp_to_utc_iso(first),
                    "last_send_time_utc": _timestamp_to_utc_iso(last),
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if quick_check == "ok" else 1


def list_sessions(args: argparse.Namespace) -> int:
    directory = Path(args.vault_dir).expanduser().resolve()
    _validate_vault(directory)
    connection = _connect_readonly(directory / DATABASE_NAME)
    try:
        query = (
            "SELECT id, name, conversation_type, last_message_time, "
            "last_message_id FROM conversation_table"
        )
        parameters: list[object] = []
        if args.keyword:
            query += " WHERE name LIKE ?"
            parameters.append(f"%{args.keyword}%")
        query += " ORDER BY last_message_time DESC LIMIT ?"
        parameters.append(args.limit)
        sessions = []
        for row in connection.execute(query, parameters):
            item = dict(row)
            item["last_message_time_utc"] = _timestamp_to_utc_iso(
                item.get("last_message_time")
            )
            sessions.append(item)
    finally:
        connection.close()
    print(
        json.dumps(
            {
                "snapshot": _snapshot_status(directory),
                "count": len(sessions),
                "sessions": sessions,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _message_row_to_dict(row: sqlite3.Row) -> dict[str, object]:
    item = dict(row)
    item["send_time_utc"] = _timestamp_to_utc_iso(item.get("send_time"))
    return item


def _message_time_clauses(args: argparse.Namespace) -> tuple[list[str], list[object]]:
    clauses: list[str] = []
    parameters: list[object] = []
    if args.since is not None:
        clauses.append("m.send_time >= ?")
        parameters.append(args.since)
    if args.until is not None:
        clauses.append("m.send_time < ?")
        parameters.append(args.until)
    return clauses, parameters


def list_messages(args: argparse.Namespace) -> int:
    directory = Path(args.vault_dir).expanduser().resolve()
    _validate_vault(directory)
    connection = _connect_readonly(directory / DATABASE_NAME)
    try:
        query = (
            "SELECT m.message_id, m.sequence, m.conversation_id, m.sender_id, "
            "u.name AS sender_name, m.content_type, m.send_time, m.content "
            "FROM message_table m JOIN user_table u ON u.id = m.sender_id "
            "WHERE m.conversation_id = ?"
        )
        parameters: list[object] = [args.conversation_id]
        clauses, time_parameters = _message_time_clauses(args)
        for clause in clauses:
            query += f" AND {clause}"
        parameters.extend(time_parameters)
        query += " ORDER BY m.sequence DESC LIMIT ?"
        parameters.append(args.limit)
        messages = [
            _message_row_to_dict(row)
            for row in connection.execute(query, parameters)
        ]
        messages.reverse()
    finally:
        connection.close()
    print(
        json.dumps(
            {
                "snapshot": _snapshot_status(directory),
                "count": len(messages),
                "messages": messages,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def search_messages(args: argparse.Namespace) -> int:
    directory = Path(args.vault_dir).expanduser().resolve()
    _validate_vault(directory)
    connection = _connect_readonly(directory / DATABASE_NAME)
    try:
        query = (
            "SELECT m.message_id, m.sequence, m.conversation_id, m.sender_id, "
            "u.name AS sender_name, m.content_type, m.send_time, m.content "
            "FROM message_table m JOIN user_table u ON u.id = m.sender_id"
        )
        clauses, parameters = _message_time_clauses(args)
        if args.conversation_id:
            clauses.insert(0, "m.conversation_id = ?")
            parameters.insert(0, args.conversation_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY m.sequence DESC"
        keyword = args.keyword.casefold()
        messages = []
        for row in connection.execute(query, parameters):
            item = _message_row_to_dict(row)
            if keyword not in str(item["content"]).casefold():
                continue
            messages.append(item)
            if len(messages) >= args.limit:
                break
        messages.reverse()
    finally:
        connection.close()
    print(
        json.dumps(
            {
                "snapshot": _snapshot_status(directory),
                "count": len(messages),
                "messages": messages,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def bounded_positive_int(value: str, maximum: int = 10_000) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("必须是整数") from error
    if parsed < 1 or parsed > maximum:
        raise argparse.ArgumentTypeError(f"必须介于 1 和 {maximum} 之间")
    return parsed


def nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("必须是整数") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("必须大于或等于 0")
    return parsed


class ChineseArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs) -> None:
        kwargs["add_help"] = False
        super().__init__(*args, **kwargs)
        self._positionals.title = "位置参数"
        self._optionals.title = "选项"
        self.add_argument("-h", "--help", action="help", help="显示此帮助信息并退出")

    def format_usage(self) -> str:
        return super().format_usage().replace("usage: ", "用法：", 1)

    def format_help(self) -> str:
        return super().format_help().replace("usage: ", "用法：", 1)


def _add_query_time_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--since", type=nonnegative_int, help="起始 Unix 秒时间戳（包含）")
    parser.add_argument("--until", type=nonnegative_int, help="结束 Unix 秒时间戳（不包含）")


def build_parser() -> argparse.ArgumentParser:
    parser = ChineseArgumentParser(
        description=(
            "DingTalkCracker：通过钉钉官方 Stream SDK 采集明确授权机器人实际收到的消息，"
            "并保存为本地证据库。"
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor_parser = subparsers.add_parser("doctor", help="检查 SDK、凭据和可选的实时连接")
    doctor_parser.add_argument(
        "--live", action="store_true", help="使用环境变量中的凭据验证 Stream 连接票据"
    )
    doctor_parser.set_defaults(handler=doctor)

    collect_parser = subparsers.add_parser("collect", help="持续采集钉钉机器人收到的消息")
    collect_parser.add_argument("--vault-dir", required=True, help="钉钉本地消息库目录")
    collect_parser.set_defaults(handler=collect_stream)

    import_parser = subparsers.add_parser(
        "import-jsonl", help="离线导入已授权保存的钉钉 Stream 回调 JSONL"
    )
    import_parser.add_argument("--input", required=True, help="每行一个回调或 data 对象的 JSONL 文件")
    import_parser.add_argument("--vault-dir", required=True, help="钉钉本地消息库目录")
    import_parser.set_defaults(handler=import_jsonl)

    inspect_parser = subparsers.add_parser("inspect", help="只读检查钉钉本地消息库")
    inspect_parser.add_argument("--vault-dir", required=True, help="钉钉本地消息库目录")
    inspect_parser.set_defaults(handler=inspect_vault)

    sessions_parser = subparsers.add_parser("sessions", help="列出钉钉消息库中的会话")
    sessions_parser.add_argument("--vault-dir", required=True, help="钉钉本地消息库目录")
    sessions_parser.add_argument("--keyword", help="按会话名称筛选的关键词")
    sessions_parser.add_argument(
        "--limit", type=bounded_positive_int, default=50, help="最多返回的会话数（默认：50）"
    )
    sessions_parser.set_defaults(handler=list_sessions)

    messages_parser = subparsers.add_parser("messages", help="读取单个钉钉会话中的消息")
    messages_parser.add_argument("--vault-dir", required=True, help="钉钉本地消息库目录")
    messages_parser.add_argument("--conversation-id", required=True, help="会话 ID")
    messages_parser.add_argument(
        "--limit", type=bounded_positive_int, default=50, help="最多返回的消息数（默认：50）"
    )
    _add_query_time_arguments(messages_parser)
    messages_parser.set_defaults(handler=list_messages)

    search_parser = subparsers.add_parser("search", help="搜索钉钉消息库中的规范化文本")
    search_parser.add_argument("--vault-dir", required=True, help="钉钉本地消息库目录")
    search_parser.add_argument("--keyword", required=True, help="要搜索的关键词")
    search_parser.add_argument("--conversation-id", help="可选：只搜索指定会话 ID")
    search_parser.add_argument(
        "--limit", type=bounded_positive_int, default=50, help="最多返回的匹配数（默认：50）"
    )
    _add_query_time_arguments(search_parser)
    search_parser.set_defaults(handler=search_messages)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.handler(args))
    except Exception as error:
        print(
            json.dumps(
                {"success": False, "error": f"{type(error).__name__}: {error}"},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
