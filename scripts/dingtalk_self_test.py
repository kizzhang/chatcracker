#!/usr/bin/env python3
"""Synthetic, credential-free tests for the DingTalk adapter."""

from __future__ import annotations

import asyncio
from argparse import Namespace
from contextlib import redirect_stdout
import importlib.util
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace


SCRIPT = Path(__file__).with_name("dingtalkcracker.py")
SPEC = importlib.util.spec_from_file_location("dingtalkcracker", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not import {SCRIPT}")
vault = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = vault
SPEC.loader.exec_module(vault)


def capture_json(operation) -> tuple[int, dict[str, object]]:
    output = StringIO()
    with redirect_stdout(output):
        code = operation()
    return code, json.loads(output.getvalue())


def expect_error(error_type: type[Exception], operation) -> None:
    try:
        operation()
    except error_type:
        return
    raise AssertionError(f"Expected {error_type.__name__}")


def fixture_records() -> list[dict[str, object]]:
    secret = "abcdefghijklmnop"
    direct_text = {
        "msgId": "msg-001",
        "conversationId": "cid-private",
        "conversationType": "1",
        "senderStaffId": "staff-001",
        "senderNick": "林杰",
        "createAt": 1_720_000_000_000,
        "msgtype": "text",
        "text": {"content": f"请跟进方案 access_token={secret}"},
        "sessionWebhook": f"https://oapi.dingtalk.test/?access_token={secret}",
    }
    rich_text_data = {
        "msgId": "msg-002",
        "conversationId": "cid-group",
        "conversationType": "2",
        "conversationTitle": "AI 项目群",
        "senderId": "encrypted-sender-002",
        "senderNick": "同事甲",
        "createAt": 1_720_000_001_000,
        "msgtype": "richText",
        "content": {
            "richText": [
                {"text": "先整理证据"},
                {"downloadCode": "download-code-must-not-persist"},
            ]
        },
        "sessionWebhook": "https://oapi.dingtalk.test/session-secret",
    }
    callback = {
        "specVersion": "1.0",
        "type": "CALLBACK",
        "headers": {
            "topic": vault.STREAM_TOPIC,
            "messageId": "frame-002",
        },
        "data": json.dumps(rich_text_data, ensure_ascii=False),
    }
    picture = {
        "msgId": "msg-003",
        "conversationId": "cid-group",
        "conversationType": "2",
        "conversationTitle": "AI 项目群",
        "senderStaffId": "staff-003",
        "senderNick": "同事乙",
        "createAt": 1_720_000_002_000,
        "msgtype": "picture",
        "content": {"downloadCode": "another-download-code-must-not-persist"},
    }
    return [direct_text, callback, picture]


def main() -> int:
    checks: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="dingtalkcracker-test-") as raw_root:
        root = Path(raw_root)
        input_path = root / "callbacks.jsonl"
        input_path.write_text(
            "\n".join(
                json.dumps(record, ensure_ascii=False)
                for record in fixture_records()
            )
            + "\n",
            encoding="utf-8",
        )
        vault_dir = root / "vault"

        code, first_import = capture_json(
            lambda: vault.import_jsonl(
                Namespace(input=str(input_path), vault_dir=str(vault_dir))
            )
        )
        assert code == 0
        assert first_import["inserted"] == 3
        assert first_import["duplicates"] == 0
        assert first_import["message_count"] == 3
        checks["jsonl_import"] = "3 messages"

        manifest = json.loads(
            (vault_dir / vault.MANIFEST_NAME).read_text(encoding="utf-8")
        )
        assert manifest["format"] == vault.MANIFEST_FORMAT
        assert manifest["source"]["system"] == "dingtalk"
        assert manifest["complete"] is False
        assert manifest["coverage"]["history_backfill"] is False
        assert manifest["credentials_persisted"] is False
        assert manifest["raw_callbacks_persisted"] is False
        checks["coverage_is_explicitly_incomplete"] = True

        database_bytes = (vault_dir / vault.DATABASE_NAME).read_bytes()
        for forbidden in (
            b"abcdefghijklmnop",
            b"session-secret",
            b"download-code-must-not-persist",
            b"another-download-code-must-not-persist",
        ):
            assert forbidden not in database_bytes
        checks["callback_secrets_not_persisted"] = True

        connection = sqlite3.connect(vault_dir / vault.DATABASE_NAME)
        try:
            contents = [
                row[0]
                for row in connection.execute(
                    "SELECT content FROM message_table ORDER BY sequence"
                )
            ]
            assert contents == [
                "请跟进方案 access_token:[REDACTED]",
                "先整理证据 [图片]",
                "[图片]",
            ]
        finally:
            connection.close()
        checks["content_normalization_and_redaction"] = True

        code, second_import = capture_json(
            lambda: vault.import_jsonl(
                Namespace(input=str(input_path), vault_dir=str(vault_dir))
            )
        )
        assert code == 0
        assert second_import["inserted"] == 0
        assert second_import["duplicates"] == 3
        assert second_import["message_count"] == 3
        checks["idempotent_replay"] = True

        writer = vault.VaultWriter(vault_dir)
        conflicting = fixture_records()[0]
        conflicting["text"] = {"content": "冲突内容"}
        try:
            expect_error(ValueError, lambda: writer.ingest(conflicting))
            writer.rollback()
        finally:
            writer.close()
        checks["conflicting_duplicate_failed_closed"] = True

        code, sessions = capture_json(
            lambda: vault.list_sessions(
                Namespace(vault_dir=str(vault_dir), keyword="AI", limit=10)
            )
        )
        assert code == 0
        assert sessions["count"] == 1
        assert sessions["sessions"][0]["id"] == "cid-group"

        code, messages = capture_json(
            lambda: vault.list_messages(
                Namespace(
                    vault_dir=str(vault_dir),
                    conversation_id="cid-group",
                    since=None,
                    until=None,
                    limit=10,
                )
            )
        )
        assert code == 0
        assert [item["message_id"] for item in messages["messages"]] == [
            "msg-002",
            "msg-003",
        ]

        code, search = capture_json(
            lambda: vault.search_messages(
                Namespace(
                    vault_dir=str(vault_dir),
                    keyword="证据",
                    conversation_id=None,
                    since=None,
                    until=None,
                    limit=10,
                )
            )
        )
        assert code == 0
        assert search["count"] == 1
        assert search["messages"][0]["message_id"] == "msg-002"
        checks["sessions_messages_search"] = True

        code, inspection = capture_json(
            lambda: vault.inspect_vault(Namespace(vault_dir=str(vault_dir)))
        )
        assert code == 0
        assert inspection["database"]["quick_check"] == "ok"
        assert inspection["snapshot"]["complete"] is False
        checks["sqlite_quick_check"] = "ok"

        assert not (vault_dir / f"{vault.DATABASE_NAME}-wal").exists()
        assert not (vault_dir / f"{vault.DATABASE_NAME}-shm").exists()
        checks["readonly_queries_left_no_sidecars"] = True

        unsafe = root / "unsafe"
        unsafe.mkdir()
        (unsafe / "unrelated.txt").write_text("keep", encoding="utf-8")
        expect_error(ValueError, lambda: vault.ensure_vault(str(unsafe)))
        assert (unsafe / "unrelated.txt").read_text(encoding="utf-8") == "keep"
        checks["unrelated_directory_rejected"] = True

        class FakeCredential:
            def __init__(self, client_id: str, client_secret: str) -> None:
                assert client_id == "synthetic-client-id"
                assert client_secret == "synthetic-client-secret"

        class FakeClient:
            def __init__(self, credential: FakeCredential) -> None:
                assert isinstance(credential, FakeCredential)

            def open_connection(self) -> dict[str, str]:
                return {
                    "endpoint": "wss://stream.dingtalk.test/connect",
                    "ticket": "synthetic-ticket-must-not-print",
                }

        fake_sdk = SimpleNamespace(
            Credential=FakeCredential,
            DingTalkStreamClient=FakeClient,
        )
        original_load_sdk = vault._load_sdk
        previous_client_id = os.environ.get(vault.CLIENT_ID_ENV)
        previous_client_secret = os.environ.get(vault.CLIENT_SECRET_ENV)
        try:
            vault._load_sdk = lambda: fake_sdk
            os.environ[vault.CLIENT_ID_ENV] = "synthetic-client-id"
            os.environ[vault.CLIENT_SECRET_ENV] = "synthetic-client-secret"
            code, live_doctor = capture_json(
                lambda: vault.doctor(Namespace(live=True))
            )
        finally:
            vault._load_sdk = original_load_sdk
            if previous_client_id is None:
                os.environ.pop(vault.CLIENT_ID_ENV, None)
            else:
                os.environ[vault.CLIENT_ID_ENV] = previous_client_id
            if previous_client_secret is None:
                os.environ.pop(vault.CLIENT_SECRET_ENV, None)
            else:
                os.environ[vault.CLIENT_SECRET_ENV] = previous_client_secret
        assert code == 0
        assert live_doctor["live_connection"] == {
            "success": True,
            "endpoint_host": "stream.dingtalk.test",
            "ticket_persisted": False,
        }
        serialized_doctor = json.dumps(live_doctor)
        assert "synthetic-client-id" not in serialized_doctor
        assert "synthetic-client-secret" not in serialized_doctor
        assert "synthetic-ticket-must-not-print" not in serialized_doctor
        checks["live_doctor_redacts_credentials_and_ticket"] = True

        try:
            import dingtalk_stream
        except ImportError:
            checks["official_sdk_handler"] = "skipped (SDK not installed)"
        else:
            writer = vault.VaultWriter(vault_dir)
            try:
                handler = vault.build_stream_handler(
                    dingtalk_stream, writer, vault_dir
                )
                live_shape = {
                    "msgId": "msg-004",
                    "conversationId": "cid-group",
                    "conversationType": "2",
                    "conversationTitle": "AI 项目群",
                    "senderStaffId": "staff-004",
                    "senderNick": "同事丙",
                    "createAt": 1_720_000_003_000,
                    "msgtype": "text",
                    "text": {"content": "SDK 形状测试"},
                }
                code, message = asyncio.run(
                    handler.process(SimpleNamespace(data=live_shape))
                )
                assert code == dingtalk_stream.AckMessage.STATUS_OK
                assert message == "OK"
            finally:
                writer.close()
            manifest = json.loads(
                (vault_dir / vault.MANIFEST_NAME).read_text(encoding="utf-8")
            )
            assert manifest["message_count"] == 4
            checks["official_sdk_handler"] = "ok"

    print(json.dumps({"success": True, "checks": checks}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
