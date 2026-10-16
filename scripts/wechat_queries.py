"""Personal WeChat discovery and immutable snapshot queries (standard library)."""
from __future__ import annotations

from contextlib import closing
import hashlib
import heapq
import json
import os
from pathlib import Path
import platform
import re
import sqlite3
import struct
import winreg

import wechat_backend as backend

TYPE_NAMES = {1: '文本', 3: '图片', 34: '语音', 42: '名片', 43: '视频',
              47: '表情', 48: '位置', 49: '文件/链接/卡片', 50: '音视频通话',
              10000: '系统消息', 11000: '表情'}
# Credential-like keyword cores. An optional bounded ``[a-z0-9]{1,32}[_-]`` prefix
# (access_, cdn_, file_, client_, …) may precede them, so prefixed forms such as
# access_token, refresh_token and my_api_key are covered without matching ordinary
# prose words. Every quantifier here is bounded so redact() stays linear-time.
_SENSITIVE_CORE = (r'(?:cdn[a-z_]{0,30}key|aeskey|authkey|filekey|file_key|session_key|encrykey|'
                   r'encryptkey|appsecret|app_secret|secret|access_token|refresh_token|'
                   r'pass_ticket|token|ticket|key|password|passwd|pwd)')
SENSITIVE = r'(?:[a-z0-9]{1,32}[_-]){0,6}' + _SENSITIVE_CORE

# Upper bound on a single message's decompressed body; caps a zstd decompression bomb.
ZSTD_MAX_OUTPUT = 32 * 1024 * 1024


def as_text(value):
    """Decode non-UTF-8 TEXT/BLOB columns for display without raising."""
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return value


def username_key(value):
    """Raw bytes a WeChat username hashes to, matching ``Msg_<MD5(username)>``."""
    if isinstance(value, str):
        return value.encode('utf-8')
    if isinstance(value, bytes):
        return value
    return str(value).encode('utf-8')


def redact(text):
    # Quoted values are terminated by their own opening quote, with escapes handled. The
    # two alternatives are disjoint on backslash, so matching stays linear (no ReDoS), and
    # crossing the other quote / an embedded newline cannot leak a tail.
    text = re.sub(r'(?i)(\b' + SENSITIVE + r'\s*=\s*(["\']))(?:\\.|(?!\2)[^\\])*(\2)', r'\1[REDACTED]\3', text)
    text = re.sub(r'(?i)(["\']' + SENSITIVE + r'["\']\s*:\s*(["\']))(?:\\.|(?!\2)[^\\])*(\2)', r'\1[REDACTED]\3', text)
    # Redact the token run right after an opening sensitive tag, allowing an optional
    # CDATA wrapper. Matching the value (not the closing tag) keeps it linear — an
    # unmatched <token> run cannot rescan the text — and still reaches keys inside CDATA.
    # Sensitive XML tags, tolerating attributes/whitespace in the opening tag. CDATA bodies
    # may legally contain '<', so they are matched up to ']]>'; a plain body stops at the
    # next tag. Both match the value (not the closing tag), which keeps them linear.
    text = re.sub(r'(?i)(<' + SENSITIVE + r'\b[^>]*>\s*<!\[CDATA\[)([^\]]*(?:\](?!\]>)[^\]]*)*)', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)(<' + SENSITIVE + r'\b[^>]*>\s*)([^<]+)', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)([?&](?:amp;)?' + SENSITIVE + r'=)[^&\s<>"\']+', r'\1[REDACTED]', text)
    # Fallbacks for unquoted, numeric, or unterminated-quoted values (e.g. token=abc,
    # {"password":123456}, aeskey="unterminated, password=p@ssw0rd!). The value runs to the
    # next structural delimiter (whitespace, quote, markup, list/brace separators), so the
    # whole value is masked including punctuation, while matching stays linear and never
    # crosses a delimiter (no over-redaction of surrounding text). A left word boundary
    # keeps a sensitive suffix (hotkey, monkey) from matching.
    text = re.sub(r'(?i)(["\']' + SENSITIVE + r'["\']\s*:\s*["\']?)([^\s,;{}\[\]"\']+)', r'\1[REDACTED]', text)
    return re.sub(r'(?i)(\b' + SENSITIVE + r'\s*[:=]\s*["\']?)([^\s,;&"\'<>]+)', r'\1[REDACTED]', text)


def decode_content(value):
    if value is None or value == '' or value == b'':
        return '', 'empty'
    if isinstance(value, str):
        return redact(value), 'utf8'
    if not isinstance(value, bytes):
        return '', 'unsupported_value'
    status = 'utf8'
    if value.startswith(b'\x28\xb5\x2f\xfd'):
        try:
            from compression.zstd import ZstdDecompressor
        except ImportError as error:
            raise RuntimeError('个人微信压缩消息需要 Python 3.14 的 compression.zstd') from error
        try:
            # Decode every concatenated frame (standard library semantics) within a
            # cumulative output cap, so multi-frame bodies are not silently truncated
            # while a decompression bomb is still refused.
            chunks, total, remaining = [], 0, value
            while remaining:
                decompressor = ZstdDecompressor()
                chunk = decompressor.decompress(remaining, max_length=ZSTD_MAX_OUTPUT - total + 1)
                chunks.append(chunk)
                total += len(chunk)
                if total > ZSTD_MAX_OUTPUT or not decompressor.eof:
                    return '', 'zstd_unresolved'  # exceeds the cap; refuse a bomb
                remaining = decompressor.unused_data
            value = b''.join(chunks)
            status = 'zstd'
        except Exception:
            return '', 'zstd_unresolved'
    try:
        return redact(value.decode('utf-8')), status
    except UnicodeDecodeError:
        return '', 'binary_unresolved'


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def is_account_db(path):
    return (path / 'contact/contact.db').is_file() and (path / 'session/session.db').is_file() and (path / 'message').is_dir()


def configured_roots():
    roots = [Path.home() / 'Documents', Path.home()]
    for subkey in (r'Software\Tencent\WeChat', r'Software\Tencent\Weixin', r'Software\Tencent\xwechat'):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey) as key:
                for name in ('FileSavePath', 'DataPath', 'DataDir'):
                    try:
                        value, _ = winreg.QueryValueEx(key, name)
                        if isinstance(value, str) and value:
                            roots.append(Path(value))
                    except OSError:
                        pass
        except OSError:
            pass
    for env in ('APPDATA', 'LOCALAPPDATA'):
        base = os.environ.get(env)
        if not base:
            continue
        for relative in ('Tencent/xwechat', 'Tencent/xwechat/config', 'Tencent/WeChat/All Users/config'):
            folder = Path(base) / relative
            if not folder.is_dir():
                continue
            try:
                entries = list(folder.glob('*'))
            except OSError:
                continue
            for config in entries:
                try:
                    if not config.is_file() or config.stat().st_size > 65536:
                        continue
                    raw = config.read_bytes()
                except OSError:
                    continue  # a config file locked or removed mid-scan must not abort discovery
                for encoding in ('utf-8-sig', 'utf-16', 'gbk'):
                    try:
                        text = raw.decode(encoding).strip().strip('\x00')
                    except UnicodeError:
                        continue
                    try:
                        obj = json.loads(text)
                    except ValueError:
                        obj = None
                    if isinstance(obj, dict):
                        roots.extend(Path(obj[k]) for k in ('dataDir', 'fileSavePath', 'savePath', 'path') if isinstance(obj.get(k), str) and obj.get(k))
                    for line in text.splitlines():
                        match = re.search(r'(?:^|=)\s*["\']?([A-Za-z]:[\\/][^\x00\r\n"\']+)', line)
                        if match:
                            roots.append(Path(match.group(1).strip()))
    return roots


def discover(data_root=None):
    found = set()
    for root in ([Path(data_root)] if data_root else configured_roots()):
        for parent in (root, root / 'xwechat_files'):
            if not parent.is_dir():
                continue
            if is_account_db(parent):
                found.add(parent.resolve())
            if is_account_db(parent / 'db_storage'):
                found.add((parent / 'db_storage').resolve())
            try:
                children = list(parent.iterdir())
            except OSError:
                continue  # an unreadable root must not abort the whole discovery
            for account in children:
                if account.is_dir() and is_account_db(account / 'db_storage'):
                    found.add((account / 'db_storage').resolve())
    return sorted(found)


def snapshot_status(directory):
    path = directory / 'snapshot_manifest.json'
    if not path.is_file():
        raise ValueError('缺少 snapshot_manifest.json；请先创建经过校验的个人微信快照')
    manifest = json.loads(path.read_text('utf-8'))
    if manifest.get('format') != 'wechat-local-snapshot-v1' or not manifest.get('success') or manifest.get('failed'):
        raise ValueError('拒绝查询失败或不匹配的个人微信快照')
    return {key: manifest.get(key) for key in ('complete', 'wal_processed', 'finished_at', 'cross_database_atomic')}


def db_root(directory):
    root = directory / 'db'
    if not is_account_db(root):
        raise ValueError('个人微信快照缺少 db/contact、db/session 或 db/message')
    # The snapshot is immutable; the set of decrypted databases must still match the
    # manifest. A dropped shard would otherwise be silently read as missing history
    # while the snapshot still reports complete, and an added file as genuine data.
    manifest = json.loads((directory / 'snapshot_manifest.json').read_text('utf-8'))
    resolved_root = root.resolve()
    listed = set()
    for item in manifest.get('databases', []):
        path = (root / item['relative_path']).resolve()
        if resolved_root not in path.parents:
            raise ValueError('快照清单中的数据库路径超出 db 目录')
        listed.add(path)
    present = {path.resolve() for path in root.rglob('*.db')}
    if listed != present:
        rel = lambda paths: sorted(str(p.relative_to(resolved_root)) for p in paths)
        raise RuntimeError('快照与清单记录的数据库集合不一致；缺失=' + repr(rel(listed - present))
                           + '，多余=' + repr(rel(present - listed)))
    # Verify each database's content against the manifest SHA-256, so a query never
    # returns results from a file modified after the snapshot was validated.
    for item in manifest.get('databases', []):
        recorded = item.get('snapshot_sha256')
        path = root / item['relative_path']
        if recorded and path.is_file():
            with path.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != recorded:
                    raise RuntimeError('快照数据库内容与清单 SHA-256 不一致：' + item['relative_path'])
    return root


def shards(root):
    return sorted(path for path in (root / 'message').glob('*.db') if re.fullmatch(r'(?:message|biz_message)_\d+\.db', path.name))


def identities(root):
    names, users = {}, set()
    with closing(backend.ro_open(root / 'contact/contact.db')) as conn:
        for user, nick, remark in conn.execute('SELECT username,nick_name,remark FROM contact'):
            if user:
                names[as_text(user)] = as_text(remark) or as_text(nick) or as_text(user)
                users.add(user)
    with closing(backend.ro_open(root / 'session/session.db')) as conn:
        users.update(row[0] for row in conn.execute('SELECT username FROM SessionTable') if row[0])
    for shard in shards(root):
        with closing(backend.ro_open(shard)) as conn:
            users.update(row[0] for row in conn.execute('SELECT user_name FROM Name2Id') if row[0])
    return names, {hashlib.md5(username_key(user)).hexdigest(): as_text(user) for user in users}


def query_sessions(args, directory):
    status = snapshot_status(directory)
    root = db_root(directory)
    names, hashes = identities(root)
    conversations = {}
    for shard in shards(root):
        with closing(backend.ro_open(shard)) as conn:
            for table, in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'Msg_*'").fetchall():
                if not re.fullmatch(r'Msg_[0-9a-f]{32}', table):
                    continue
                count, latest = conn.execute('SELECT count(*),max(create_time) FROM ' + quote(table)).fetchone()
                user = hashes.get(table[4:], '')
                name = names.get(user, user) or table[4:]
                if args.keyword and args.keyword.casefold() not in (name + ' ' + user).casefold():
                    continue
                record = conversations.setdefault(table[4:], {'conversation_id': user or table[4:], 'name': name, 'message_count': 0, 'last_message_time': 0})
                record['message_count'] += count
                record['last_message_time'] = max(record['last_message_time'], latest or 0)
    records = sorted(conversations.values(), key=lambda r: r['last_message_time'], reverse=True)
    backend.emit(success=True, app='wechat', **status, sessions=records[:args.limit], count=min(len(records), args.limit))
    return 0


def query_messages(args, directory):
    status = snapshot_status(directory)
    root = db_root(directory)
    names, hashes = identities(root)
    conversation = getattr(args, 'conversation_id', None)
    chat_hash = conversation if conversation and re.fullmatch(r'[a-f0-9]{32}', conversation) else (hashlib.md5(conversation.encode()).hexdigest() if conversation else None)
    keyword = getattr(args, 'keyword', None)
    if args.since is not None and args.until is not None and args.since > args.until:
        raise ValueError('--since 不能大于 --until')
    heap = []
    unresolved = 0
    serial = 0
    for shard in shards(root):
        with closing(backend.ro_open(shard)) as conn:
            conn.row_factory = sqlite3.Row
            sender_map = {row[0]: as_text(row[1]) for row in conn.execute('SELECT rowid,user_name FROM Name2Id')}
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'Msg_*'") if re.fullmatch(r'Msg_[a-f0-9]{32}', r[0])]
            for table in tables:
                if chat_hash and table[4:] != chat_hash:
                    continue
                filters, parameters = [], []
                lower_bound = args.since
                if len(heap) == args.limit:
                    lower_bound = max(lower_bound or 0, heap[0][0])
                if lower_bound is not None:
                    filters.append('create_time>=?'); parameters.append(lower_bound)
                if args.until is not None:
                    filters.append('create_time<=?'); parameters.append(args.until)
                sql = 'SELECT * FROM ' + quote(table) + (' WHERE ' + ' AND '.join(filters) if filters else '') + ' ORDER BY create_time DESC,sort_seq DESC,local_id DESC'
                for row in conn.execute(sql, parameters):
                    if len(heap) == args.limit and (row['create_time'] or 0) < heap[0][0]:
                        break
                    text, parse_status = decode_content(row['message_content'])
                    if not text and row['compress_content']:
                        fallback, fallback_status = decode_content(row['compress_content'])
                        if fallback or parse_status == 'empty':
                            text, parse_status = fallback, 'compress_content:' + fallback_status
                        else:
                            parse_status += ';compress_content:' + fallback_status
                    if 'unresolved' in parse_status:
                        unresolved += 1
                    if keyword and keyword.casefold() not in text.casefold():
                        continue
                    user = hashes.get(table[4:], '')
                    sender = sender_map.get(row['real_sender_id'], '')
                    raw_type = row['local_type']
                    base_type = (raw_type & 0xffffffff) if isinstance(raw_type, int) else raw_type
                    record = {
                        'message_id': f'{shard.name}:{table}:{row["local_id"]}',
                        'local_id': row['local_id'], 'server_id': row['server_id'],
                        'conversation_id': user or table[4:], 'conversation_name': names.get(user, user),
                        'sender_id': sender, 'sender_name': names.get(sender, sender),
                        'send_time': row['create_time'], 'send_time_iso_utc': backend.wc._timestamp_to_utc_iso(row['create_time']),
                        'sequence': row['sort_seq'], 'content_type': raw_type,
                        'type_name': TYPE_NAMES.get(base_type, str(raw_type)),
                        'text': text, 'parse_status': parse_status,
                        'source_db': shard.name, 'source_table': table,
                    }
                    serial += 1
                    heapq.heappush(heap, (row['create_time'] or 0, row['sort_seq'] or 0,
                                          row['local_id'] or 0, shard.name, table, serial, record))
                    if len(heap) > args.limit:
                        heapq.heappop(heap)
    records = [item[-1] for item in sorted(heap, reverse=True)]
    backend.emit(success=True, app='wechat', **status, messages=records, count=len(records), scanned_unresolved_messages=unresolved)
    return 0


def dispatch(args):
    if args.command in ('discover', 'doctor'):
        accounts = discover(args.data_root)
        # Count only databases decrypt would process; the reserved db is always skipped.
        sources = [{'db_dir': str(p), 'account': p.parent.name,
                    'database_count': sum(1 for db in p.rglob('*.db')
                                          if db.relative_to(p).as_posix().casefold() != 'migrate/unspportmsg.db')}
                   for p in accounts]
        if args.command == 'discover':
            backend.emit(success=True, app='wechat', count=len(sources), sources=sources)
        else:
            try:
                from compression import zstd
                zstd_available = True
            except ImportError:
                zstd_available = False
            with backend.wc.WindowsAesCbc() as aes:
                backend.cng256_test(aes)
            # An elevated shell cannot scan same-session processes; report it instead of
            # aborting the whole diagnostic (as the WeCom doctor does).
            elevated = bool(backend.wc.shell32.IsUserAnAdmin())
            weixin_count = None if elevated else len(backend.pids())
            backend.emit(success=True, app='wechat', windows=platform.platform(), python=platform.python_version(), python_64_bit=struct.calcsize('P') == 8, stdlib_zstd=zstd_available, cng_aes256_self_test='ok', elevated=elevated, same_session_weixin_process_count=weixin_count, sources=sources)
        return 0
    if args.command == 'decrypt':
        return backend.snapshot(args)
    directory = Path(args.decrypted_dir).resolve()
    if args.command == 'sessions':
        return query_sessions(args, directory)
    if args.command in ('messages', 'search'):
        return query_messages(args, directory)
    status = snapshot_status(directory)
    root = db_root(directory)
    manifest = json.loads((directory / 'snapshot_manifest.json').read_text('utf-8'))
    records = []
    for item in manifest['databases']:
        relative = item['relative_path']
        path = (root / relative).resolve()
        if root.resolve() not in path.parents:
            raise ValueError('快照清单中的数据库路径超出 db 目录')
        try:
            current = backend.validate_database(path)
            if current['snapshot_sha256'] != item.get('snapshot_sha256'):
                raise RuntimeError('Snapshot SHA-256 does not match the validated manifest')
        except Exception as error:
            raise RuntimeError(f'{relative}: {error}') from error
        records.append({'relative_path': relative, 'quick_check': current['quick_check'],
                        'quick_check_scope': current['quick_check_scope'], 'bytes': current['bytes'],
                        'snapshot_sha256': current['snapshot_sha256'], 'hash_matches': True})
    backend.emit(success=True, app='wechat', **status, databases=records)
    return 0
