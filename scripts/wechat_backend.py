"""Read the current user's WeChat 4.x data; never persist keys or memory.

Reuses WeComCracker's Windows APIs and CNG backend. SQLCipher4 layout and
WCDB Config.Cipher discovery checked against wechatauto-replica/db.py
(Apache-2.0). No third-party Python runtime dependencies.
"""
from __future__ import annotations

from contextlib import closing
import csv
import ctypes
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import time

import wecomcracker as wc

PAGE = 4096
RESERVE = 80
MAGIC = b'SQLite format 3\x00'
ANCHOR = b'com.Tencent.WCDB.Config.Cipher'
MASK = bytes.fromhex('d2c7442458020000004889442450488b450048844c2448488944254048584c24')
LITERAL = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")
TZ = timezone.utc

def now():
    return datetime.now(TZ).isoformat()

def _json_default(value):
    # Backstop so a stray bytes/BLOB in any column never aborts output with TypeError.
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode('utf-8', 'replace')
    return repr(value)

def emit(**data):
    print(json.dumps(data, ensure_ascii=False, default=_json_default), flush=True)

def verify(key, page, salt=None):
    if len(key) != 32 or len(page) != PAGE:
        return False
    msalt = bytes(x ^ 0x3a for x in (salt or page[:16]))
    mkey = hashlib.pbkdf2_hmac('sha512', key, msalt, 2, dklen=32)
    computed = hmac.new(mkey, page[16:4032] + struct.pack('<I', 1), hashlib.sha512).digest()
    return hmac.compare_digest(computed, page[-64:])

def pids():
    if wc.shell32.IsUserAnAdmin():
        raise RuntimeError('Refusing elevated process access')
    cur = wc.wt.DWORD()
    if not wc.kernel32.ProcessIdToSessionId(wc.kernel32.GetCurrentProcessId(), ctypes.byref(cur)):
        raise ctypes.WinError(ctypes.get_last_error())
    result = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq Weixin.exe', '/FO', 'CSV', '/NH'], capture_output=True, encoding='gbk', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
    found = []
    for row in csv.reader(result.stdout.splitlines()):
        if len(row) > 1 and row[0].lower() == 'weixin.exe' and row[1].isdigit():
            pid = int(row[1])
            sid = wc.wt.DWORD()
            if wc.kernel32.ProcessIdToSessionId(pid, ctypes.byref(sid)) and sid.value == cur.value:
                found.append(pid)
    return found

def blocks(handle, regions, deadline=None):
    for base, size in regions:
        for offset in range(0, size, 8 * 1024 * 1024):
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError('Weixin.exe memory scan timed out')
            n = min(8 * 1024 * 1024 + 256, size-offset)
            data = wc.read_process_memory(handle, base+offset, n)
            if data:
                yield base+offset, data

def scan_candidates(blob, pages, keys, tested):
    for match in LITERAL.finditer(blob):
        text = match.group(1).decode('ascii')
        # An in-memory hex run may have an odd length (e.g. a trailing nibble);
        # drop it so bytes.fromhex never aborts the whole scan. No whole byte is
        # lost, so every aligned 32-byte key candidate is still examined.
        material = bytes.fromhex(text[:len(text) & ~1])
        for offset in range(0, max(1, len(material)-31), 16):
            key = material[offset:offset+32]
            explicit = material[offset+32:offset+48]
            token = (key, explicit)
            if len(key) != 32 or token in tested:
                continue
            tested.add(token)
            for rel, page in pages.items():
                if rel in keys:
                    continue
                if verify(key, page):
                    keys[rel] = (key, page[:16])
                elif len(explicit) == 16 and verify(key, page, explicit):
                    keys[rel] = (key, explicit)


def recover(files, timeout=120, verbose=False):
    if not files:
        raise ValueError('No WeChat databases found')
    pages = {}
    for rel, path in files.items():
        with path.open('rb') as f:
            pages[rel] = f.read(PAGE)
    keys = {}
    tested = set()
    for pid in pids():
        handle = wc.kernel32.OpenProcess(wc.PROCESS_VM_READ | wc.PROCESS_QUERY_INFORMATION, False, pid)
        if not handle:
            if verbose:
                emit(event='process_unavailable', pid=pid)
            continue
        try:
            deadline = time.monotonic() + timeout
            regions = list(wc.iter_readable_regions(handle))
            anchors = set()
            for base, data in blocks(handle, regions, deadline):
                scan_candidates(data, pages, keys, tested)
                pos = 0
                while (pos := data.find(ANCHOR, pos)) >= 0:
                    anchors.add(base+pos)
                    pos += 1
            if verbose:
                emit(event='memory_pass', pid=pid, validated_databases=len(keys), total=len(files))
            if len(keys) == len(files):
                return keys
            if anchors:
                pairs = [struct.pack('<QQ', a, len(ANCHOR)) for a in anchors]
                pattern = re.compile(b'|'.join(re.escape(p) for p in pairs))
                visited = set()
                for base, data in blocks(handle, regions, deadline):
                    for match in pattern.finditer(data):
                        address = base + match.start()
                        if address in visited:
                            continue
                        visited.add(address)
                        node = wc.read_process_memory(handle, address-0x10, 0x50)
                        if not node or len(node) < 0x40:
                            continue
                        ptr = struct.unpack_from('<Q', node, 0x28)[0]
                        if not 0x10000 <= ptr < 0x800000000000:
                            continue
                        obj = wc.read_process_memory(handle, ptr+0x88, 0x28)
                        if not obj or len(obj) < 0x18:
                            continue
                        bptr, length = struct.unpack_from('<QQ', obj, 8)
                        if not (0 < length <= 1024 and 0x10000 <= bptr < 0x800000000000):
                            continue
                        blob = wc.read_process_memory(handle, bptr, length)
                        if blob and len(blob) == length:
                            scan_candidates(bytes(v ^ MASK[i % len(MASK)] for i, v in enumerate(blob)), pages, keys, tested)
                if verbose:
                    emit(event='config_pass', pid=pid, validated_databases=len(keys), total=len(files))
            if len(keys) == len(files):
                return keys
        except TimeoutError:
            if verbose:
                emit(event='scan_timeout', pid=pid)
        finally:
            wc.kernel32.CloseHandle(handle)
    return keys

class Aes256:
    def __init__(self, backend, key):
        self.handle = ctypes.c_void_p()
        self.obj = ctypes.create_string_buffer(backend._key_object_length)
        material = ctypes.create_string_buffer(key, len(key))
        wc._check_ntstatus(wc.bcrypt.BCryptGenerateSymmetricKey(backend._algorithm, ctypes.byref(self.handle), self.obj, len(self.obj), material, len(key), 0), 'AES256 key')
    def decrypt(self, iv, data):
        source = ctypes.create_string_buffer(data, len(data))
        output = ctypes.create_string_buffer(len(data))
        ivbuf = ctypes.create_string_buffer(iv, len(iv))
        written = wc.wt.ULONG()
        wc._check_ntstatus(wc.bcrypt.BCryptDecrypt(self.handle, source, len(data), None, ivbuf, 16, output, len(data), ctypes.byref(written), 0), 'AES256 decrypt')
        if written.value != len(data):
            raise RuntimeError('Unexpected AES256 length')
        return output.raw
    def close(self):
        wc.bcrypt.BCryptDestroyKey(self.handle)

def cng256_test(backend):
    aes = Aes256(backend, bytes.fromhex('603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4'))
    try:
        actual = aes.decrypt(bytes.fromhex('000102030405060708090a0b0c0d0e0f'), bytes.fromhex('f58c4c04d6e5f1ba779eabfb5f7bfbd6'))
        if actual != bytes.fromhex('6bc1bee22e409f96e93d7e117393172a'):
            raise RuntimeError('AES256 NIST vector failed')
    finally:
        aes.close()

def page_auth(page, number, mkey):
    start = 16 if number == 1 else 0
    computed = hmac.new(mkey, page[start:4032] + struct.pack('<I', number), hashlib.sha512).digest()
    return hmac.compare_digest(computed, page[-64:])

def page_decrypt(aes, page, number):
    if len(page) != PAGE:
        raise RuntimeError('Partial database page')
    start = 16 if number == 1 else 0
    plain = aes.decrypt(page[4016:4032], page[start:4016])
    return (MAGIC if number == 1 else b'') + plain + bytes(80)

def stat_signature(path):
    st = path.stat()
    return (st.st_size, st.st_mtime_ns)

def stable_capture(source, dest):
    wal_path = Path(str(source) + '-wal')
    shm_path = Path(str(source) + '-shm')
    for attempt in range(12):
        before = stat_signature(source)
        try:
            shm_before = read_shm_header(shm_path)
            wal_before = wal_path.read_bytes() if wal_path.exists() else b''
            shutil.copyfile(source, dest)
            wal_after = wal_path.read_bytes() if wal_path.exists() else b''
            shm_after = read_shm_header(shm_path)
        except FileNotFoundError:
            # SQLite may unlink/recreate a sidecar after the existence check.
            time.sleep(0.15)
            continue
        after = stat_signature(source)
        if before == after and wal_before == wal_after and shm_before == shm_after:
            wal_index = wal_index_state(shm_before)
            with dest.open('rb') as f:
                digest = hashlib.file_digest(f, 'sha256').hexdigest()
            return wal_before, {'captured_at': now(), 'source_bytes': before[0], 'source_mtime_ns': before[1], 'wal_bytes': len(wal_before), 'wal_index_frames': wal_index['frames'] if wal_index else None, 'wal_index': wal_index, 'capture_attempts': attempt+1, 'encrypted_copy_sha256': digest}
        time.sleep(0.15)
    raise RuntimeError('Source remained busy during stable snapshot capture: ' + source.name)

def read_shm_header(path):
    if not path.exists():
        return b''
    with path.open('rb') as f:
        return f.read(96)

def wal_index_state(header):
    if not header:
        return None
    if len(header) != 96 or header[:48] != header[48:96]:
        raise RuntimeError('WAL index is changing or truncated; retry snapshot')
    version, _, _, initialized, _, page_size, frames, _ = struct.unpack_from('<IIIBBHII', header)
    if version != 3007000 or initialized != 1 or not (page_size == PAGE or (page_size == 0 and frames == 0)):
        raise RuntimeError('Unsupported WAL index header')
    if checksum(header[:40], '<') != struct.unpack_from('<II', header, 40):
        raise RuntimeError('WAL index checksum failed')
    # aSalt is an exact byte copy of the corresponding WAL header salt.
    return {'frames': frames, 'salt': header[32:40].hex()}

def checksum(data, endian, state=(0, 0)):
    values = struct.unpack(endian + str(len(data)//4) + 'I', data)
    a, b = state
    for index in range(0, len(values), 2):
        a = (a + values[index] + b) & 0xffffffff
        b = (b + values[index+1] + a) & 0xffffffff
    return a, b

def committed_wal(wal, aes, mkey, wal_index=None):
    expected_frames = wal_index['frames'] if wal_index is not None else None
    metadata = {'wal_processed': True, 'valid_frames': 0, 'applied_frames': 0, 'committed_pages': None, 'tail': 'none'}
    if not wal:
        if expected_frames:
            raise RuntimeError('WAL index references missing committed frames')
        return [], metadata
    if len(wal) < 32:
        raise RuntimeError('Incomplete nonempty WAL header')
    magic, version, page_size = struct.unpack_from('>III', wal)
    if magic not in (0x377f0682, 0x377f0683) or page_size != PAGE or version != 3007000:
        raise RuntimeError('Unsupported WAL format')
    endian = '<' if magic == 0x377f0682 else '>'
    state = checksum(wal[:24], endian)
    if state != struct.unpack_from('>II', wal, 24):
        raise RuntimeError('WAL header checksum failed')
    salt = wal[16:24]
    if wal_index is not None and wal_index['salt'] != salt.hex():
        raise RuntimeError('WAL index generation disagrees with WAL header')
    frames = []
    committed = 0
    mode = None
    stop = len(wal)-4119
    if expected_frames is not None:
        if len(wal) < 32 + expected_frames * 4120:
            raise RuntimeError('WAL index references missing committed frames')
        stop = 32 + expected_frames * 4120
    for offset in range(32, stop, 4120):
        header = wal[offset:offset+24]
        if header[8:16] != salt:
            # A valid WAL holds the current generation's frames first and only stale
            # leftovers at the tail. A current-salt frame after this point means the WAL
            # is inconsistent (e.g. a corrupted salt), so refuse it rather than silently
            # dropping committed frames while still reporting the snapshot complete.
            if any(wal[later+8:later+16] == salt for later in range(offset+4120, stop, 4120)):
                raise RuntimeError('WAL frame salt is inconsistent; refusing possibly-corrupt WAL')
            metadata['tail'] = 'stale_generation'
            break
        number, commit_size = struct.unpack_from('>II', header)
        if number == 0 or number > 0xfffffffe:
            raise RuntimeError('Invalid WAL page number')
        page = wal[offset+24:offset+4120]
        if not page_auth(page, number, mkey):
            raise RuntimeError('WAL page authentication failed')
        plain = page_decrypt(aes, page, number)
        expected = struct.unpack_from('>II', header, 16)
        possibilities = ('encrypted', 'plaintext') if mode is None else (mode,)
        for candidate in possibilities:
            check = checksum(header[:8] + (page if candidate == 'encrypted' else plain), endian, state)
            if check == expected:
                mode, state = candidate, check
                break
        else:
            raise RuntimeError('WAL frame checksum failed')
        frames.append((number, plain))
        if commit_size:
            committed = len(frames)
            metadata['committed_pages'] = commit_size
    metadata['valid_frames'] = len(frames)
    metadata['applied_frames'] = committed
    metadata['checksum_mode'] = mode
    metadata['uncommitted_frames_ignored'] = len(frames)-committed
    metadata['trailing_bytes'] = max(0, len(wal)-32) % 4120
    if expected_frames is not None and (len(frames) != expected_frames or committed != expected_frames):
        raise RuntimeError('WAL commit boundary disagrees with stable WAL index')
    return frames[:committed], metadata

def ro_open(path):
    conn = wc._open_sqlite_readonly(path)
    conn.text_factory = lambda b: b.decode('utf-8') if is_utf8(b) else b
    return conn

def is_utf8(data):
    try:
        data.decode('utf-8')
        return True
    except UnicodeDecodeError:
        return False

def validate_database(path):
    before = stat_signature(path)
    with closing(ro_open(path)) as conn:
        schema = conn.execute("SELECT name, sql, rootpage FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        tables = [{'name': row[0], 'sql': row[1]} for row in schema]
        scope = 'database'
        try:
            check = [row[0] for row in conn.execute('PRAGMA quick_check')]
            if check != ['ok']:
                raise RuntimeError('SQLite quick_check failed')
        except sqlite3.OperationalError:
            virtual = [row for row in schema if row[2] == 0 and (row[1] or '').upper().startswith('CREATE VIRTUAL TABLE')]
            # SQL keywords are case-insensitive, so match USING FTS5 case-insensitively;
            # MMFtsTokenizer stays an exact identifier check.
            if not virtual or not all('USING FTS5' in (row[1] or '').upper() and 'MMFtsTokenizer' in (row[1] or '') for row in virtual):
                raise
            physical = ['sqlite_schema'] + [row[0] for row in schema if row[2] > 0]
            for name in physical:
                result = [row[0] for row in conn.execute('PRAGMA quick_check("' + name.replace('"', '""') + '")')]
                if result != ['ok']:
                    raise RuntimeError('Physical table quick_check failed')
            scope = 'all_physical_tables_and_schema; custom_FTS_tokenizer_semantics_not_checked'
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if before != stat_signature(path):
        raise RuntimeError('Snapshot database changed during validation')
    wc._assert_no_plaintext_wal(path)
    return {'quick_check': 'ok', 'quick_check_scope': scope, 'tables': tables,
            'snapshot_sha256': digest, 'bytes': before[0]}


def snapshot_one(backend, source, dest, key, salt):
    dest.parent.mkdir(parents=True, exist_ok=True)
    encrypted_copy = dest.with_suffix('.capture')
    wal, info = stable_capture(source, encrypted_copy)
    mkey = hashlib.pbkdf2_hmac('sha512', key, bytes(v ^ 0x3a for v in salt), 2, dklen=32)
    aes = Aes256(backend, key)
    blank_pages = 0
    authenticated = 0
    try:
        with encrypted_copy.open('rb') as inp, dest.open('xb') as out:
            number = 1
            while page := inp.read(PAGE):
                if page == bytes(PAGE) and number != 1:
                    out.write(page)
                    blank_pages += 1
                else:
                    if not page_auth(page, number, mkey):
                        raise RuntimeError('Database page authentication failed at page ' + str(number))
                    out.write(page_decrypt(aes, page, number))
                    authenticated += 1
                number += 1
        frames, wal_info = committed_wal(wal, aes, mkey, info['wal_index'])
        with dest.open('r+b') as out:
            for number, page in frames:
                out.seek((number-1)*PAGE)
                out.write(page)
            if wal_info['committed_pages'] is not None:
                size = wal_info['committed_pages']
                out.truncate(size*PAGE)
                out.seek(28)
                out.write(struct.pack('>I', size))
        info.update(wal_info)
        info.update(validate_database(dest))
        info.update(authenticated_pages=authenticated, blank_pages=blank_pages)
        return info
    finally:
        aes.close()
        encrypted_copy.unlink(missing_ok=True)


def snapshot(args):
    if args.base_only:
        raise ValueError('个人微信会校验并合并 WAL，不支持 --base-only')
    source = Path(args.db_dir).resolve()
    out = Path(args.out_dir).absolute()
    wc.assert_safe_output(source, out)
    # Require message/ too, so a directory that snapshots cannot later be queried
    # (db_root/is_account_db also require it) fails early with a clear message.
    if not (source / 'contact/contact.db').is_file() or not (source / 'session/session.db').is_file() or not (source / 'message').is_dir():
        raise ValueError('--db-dir 必须指向个人微信账号的 db_storage 目录')
    files = {}
    skipped = []
    for path in sorted(source.rglob('*.db')):
        rel = path.relative_to(source).as_posix()
        if rel.casefold() == 'migrate/unspportmsg.db':
            skipped.append({'relative_path': rel, 'reason': 'reserved_unsupported_message_database_without_in_memory_key'})
        else:
            files[rel] = path
    if not files or any(path.stat().st_size < PAGE or path.stat().st_size % PAGE for path in files.values()):
        raise ValueError('存在空数据库或非完整的 4096 字节页')
    keys = recover(files, timeout=args.timeout, verbose=args.verbose)
    if len(keys) != len(files):
        missing = [name for name in files if name not in keys]
        raise RuntimeError('无法校验全部数据库；请确认当前账号已登录且目录正确：' + ', '.join(missing))
    manifest = {
        'format': 'wechat-local-snapshot-v1', 'app': 'wechat',
        'source': str(source), 'output': str(out), 'started_at': now(),
        'success': False, 'complete': False, 'wal_processed': True,
        'third_party_dependencies': 0, 'keys_persisted': False,
        'cross_database_atomic': False, 'databases': [], 'failed': 0,
        'skipped_databases': skipped,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f'.{out.name}.staging-', dir=out.parent))
    try:
        with wc.WindowsAesCbc() as backend:
            wc.cng_self_test(backend)
            cng256_test(backend)
            for rel, path in files.items():
                try:
                    info = snapshot_one(backend, path, staging / 'db' / rel, *keys[rel])
                except Exception as error:
                    raise RuntimeError(f'{rel}: {error}') from error
                info['relative_path'] = rel
                manifest['databases'].append(info)
                if args.verbose:
                    emit(event='snapshot_verified', database=rel, wal_frames=info['applied_frames'])
        manifest.update(finished_at=now(), success=True, complete=True, wal_processed=True)
        (staging / 'snapshot_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        # On Windows rename refuses a destination created by another process.
        staging.rename(out)
    except BaseException as error:
        try:
            shutil.rmtree(staging)
        except OSError as cleanup_error:
            raise RuntimeError(f'快照创建失败（{type(error).__name__}: {error}）；明文暂存目录清理失败，需要处理：{staging}') from cleanup_error
        raise
    manifest_path = out / 'snapshot_manifest.json'
    emit(success=True, app='wechat', complete=True,
         wal_processed=True, failed=0,
         database_count=len(files), third_party_dependencies=0, keys_persisted=False,
         output_dir=str(out), manifest=str(manifest_path))
    return 0
