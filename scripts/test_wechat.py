"""Synthetic regression tests; no real account data or process access."""
from argparse import Namespace
from contextlib import closing, contextmanager, redirect_stdout
import ctypes
import hashlib
import hmac
import io
import json
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import wechat_backend as b
import wechat_queries as q
import wecomcracker as wc


@contextmanager
def writable(path):
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            yield connection


def encrypt(aes, data, iv):
    fn = wc.bcrypt.BCryptEncrypt
    fn.argtypes = wc.bcrypt.BCryptDecrypt.argtypes
    fn.restype = wc.bcrypt.BCryptDecrypt.restype
    source = ctypes.create_string_buffer(data, len(data))
    output = ctypes.create_string_buffer(len(data))
    ivbuf = ctypes.create_string_buffer(iv, 16)
    written = wc.wt.ULONG()
    wc._check_ntstatus(fn(aes.handle, source, len(data), None, ivbuf, 16, output, len(data), ctypes.byref(written), 0), 'test encrypt')
    return output.raw


def manifest_with_dbs(db):
    dbs = [{'relative_path': p.relative_to(db).as_posix()} for p in sorted(db.rglob('*.db'))]
    return json.dumps({'format': 'wechat-local-snapshot-v1', 'success': True, 'failed': 0,
                       'complete': True, 'wal_processed': True, 'databases': dbs})


class WeChatTests(unittest.TestCase):
    def setUp(self):
        self.key = bytes(range(32))
        self.salt = bytes(range(16))
        self.mkey = hashlib.pbkdf2_hmac('sha512', self.key, bytes(x ^ 0x3a for x in self.salt), 2, 32)
        self.backend = wc.WindowsAesCbc()
        self.aes = b.Aes256(self.backend, self.key)

    def tearDown(self):
        self.aes.close()
        self.backend.close()

    def page(self, number):
        plain = bytes((i + number) % 251 for i in range(4016))
        iv = hashlib.sha256(struct.pack('<I', number)).digest()[:16]
        data = (self.salt if number == 1 else b'') + encrypt(self.aes, plain[16:] if number == 1 else plain, iv) + iv
        mac = hmac.new(self.mkey, data[16 if number == 1 else 0:] + struct.pack('<I', number), hashlib.sha512).digest()
        return data + mac, (b.MAGIC + plain[16:] if number == 1 else plain) + bytes(80)

    def wal(self, commit_at=2, frames=3):
        commits = (commit_at,) if isinstance(commit_at, int) else commit_at
        header = struct.pack('>IIII', 0x377f0682, 3007000, 4096, 0) + b'GEN12345'
        state = b.checksum(header, '<')
        out = header + struct.pack('>II', *state)
        for number in range(1, frames + 1):
            page, _ = self.page(number)
            frame = struct.pack('>II', number, number if number in commits else 0) + b'GEN12345'
            state = b.checksum(frame[:8] + page, '<', state)
            out += frame + struct.pack('>II', *state) + page
        return out

    def shm(self, frames=2, salt=b'GEN12345'):
        head = bytearray(40)
        struct.pack_into('<IIIBBHII', head, 0, 3007000, 0, 1, 1, 0, 4096, frames, frames)
        head[32:40] = salt
        header = bytes(head) + struct.pack('<II', *b.checksum(head, '<'))
        return header * 2

    def wal_index(self, frames=2, salt=b'GEN12345'):
        return b.wal_index_state(self.shm(frames, salt))

    def test_aes256_and_page_authentication(self):
        b.cng256_test(self.backend)
        page, expected = self.page(1)
        self.assertTrue(b.verify(self.key, page))
        self.assertFalse(b.verify(bytes(reversed(self.key)), page))
        self.assertEqual(b.page_decrypt(self.aes, page, 1), expected)
        corrupt = bytearray(page); corrupt[100] ^= 1
        self.assertFalse(b.page_auth(corrupt, 1, self.mkey))
        page2, expected2 = self.page(2)
        self.assertEqual(b.page_decrypt(self.aes, page2, 2), expected2)
        self.assertFalse(b.page_auth(page2, 3, self.mkey))

    def test_wal_committed_boundary_and_stale_generation(self):
        wal = self.wal()
        frames, info = b.committed_wal(wal, self.aes, self.mkey)
        self.assertEqual(len(frames), 2)
        self.assertEqual(info['uncommitted_frames_ignored'], 1)
        self.assertEqual(info['committed_pages'], 2)
        self.assertEqual(len(b.committed_wal(wal, self.aes, self.mkey, self.wal_index(2))[0]), 2)
        self.assertEqual(b.committed_wal(wal, self.aes, self.mkey, self.wal_index(0))[0], [])
        with self.assertRaises(RuntimeError):
            b.committed_wal(wal, self.aes, self.mkey, self.wal_index(3))
        stale = bytearray(wal); stale[40:48] = b'OLD12345'
        self.assertEqual(b.committed_wal(stale, self.aes, self.mkey)[0], [])
        with self.assertRaises(RuntimeError):
            b.committed_wal(stale, self.aes, self.mkey, self.wal_index(2))

    def test_stale_wal_index_cannot_silently_truncate_a_committed_wal(self):
        wal = self.wal(commit_at=(2, 3))
        self.assertEqual(len(b.committed_wal(wal, self.aes, self.mkey, self.wal_index(3))[0]), 3)
        for frames in (0, 2):
            with self.subTest(frames=frames), self.assertRaisesRegex(RuntimeError, 'generation'):
                b.committed_wal(wal, self.aes, self.mkey, self.wal_index(frames, b'OLD12345'))
        self.assertEqual(b.committed_wal(b'', self.aes, self.mkey, self.wal_index(0))[0], [])
        with self.assertRaisesRegex(RuntimeError, 'missing committed frames'):
            b.committed_wal(b'', self.aes, self.mkey, self.wal_index(2))

    def test_capture_preserves_wal_index_generation_for_snapshot_validation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / 'source.db'
            source.write_bytes(self.page(1)[0])
            Path(str(source) + '-wal').write_bytes(self.wal(commit_at=(2, 3)))
            Path(str(source) + '-shm').write_bytes(self.shm(2, b'OLD12345'))
            wal, info = b.stable_capture(source, root / 'copy.capture')
            self.assertEqual(info['wal_index_frames'], 2)
            self.assertEqual(info['wal_index'], {'frames': 2, 'salt': b'OLD12345'.hex()})
            before = {p.name: p.read_bytes() for p in root.glob('source.db*')}
            with self.assertRaisesRegex(RuntimeError, 'generation'):
                b.snapshot_one(self.backend, source, root / 'output.db', self.key, self.salt)
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.glob('source.db*')})
            self.assertFalse((root / 'output.capture').exists())

    def test_wal_damage_is_not_silently_ignored(self):
        wal = self.wal()
        for offset in (10, 33, 100, 4145):
            corrupt = bytearray(wal); corrupt[offset] ^= 1
            with self.assertRaises(RuntimeError):
                b.committed_wal(corrupt, self.aes, self.mkey, self.wal_index(2))
        with self.assertRaises(RuntimeError):
            b.committed_wal(wal[:100], self.aes, self.mkey, self.wal_index(2))

    def test_wal_index_and_timeout(self):
        header = self.shm()
        self.assertEqual(b.wal_index_state(header), {'frames': 2, 'salt': b'GEN12345'.hex()})
        self.assertIsNone(b.wal_index_state(b''))
        with self.assertRaises(RuntimeError):
            b.wal_index_state(header[:48] + bytes(48))
        corrupt = bytearray(header); corrupt[32] ^= 1
        with self.assertRaisesRegex(RuntimeError, 'checksum'):
            b.wal_index_state(corrupt[:48] * 2)
        with self.assertRaises(TimeoutError):
            next(b.blocks(None, [(0, 1)], deadline=0))

    def test_decode_and_redaction(self):
        from compression import zstd
        text = '会议安排：下周确认 👋'
        self.assertEqual(q.decode_content(zstd.compress(text.encode())), (text, 'zstd'))
        for text in ('aeskey="secret"', '<filekey>secret</filekey>', '?token=secret', '&amp;token=secret', '{"authkey":"secret"}'):
            self.assertNotIn('secret', q.redact(text))
        self.assertEqual(q.decode_content(b'\xff\x00')[1], 'binary_unresolved')

    def test_immutable_queries_global_order_and_sender_mapping(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); db = root / 'db'
            for folder in ('contact', 'session', 'message'):
                (db / folder).mkdir(parents=True)
            with writable(db / 'contact/contact.db') as c:
                c.execute('CREATE TABLE contact(username,nick_name,remark)')
                c.executemany('INSERT INTO contact VALUES(?,?,?)', [('alice', 'Alice', ''), ('bob', 'Bob', '')])
            with writable(db / 'session/session.db') as c:
                c.execute('CREATE TABLE SessionTable(username)')
                c.execute("INSERT INTO SessionTable VALUES('alice')")
            for filename, who, sid, stamp in [('message_0.db', 'alice', 1, 100), ('biz_message_0.db', 'bob', 9, 200)]:
                with writable(db / 'message' / filename) as c:
                    c.execute('CREATE TABLE Name2Id(user_name)')
                    c.execute('INSERT INTO Name2Id(rowid,user_name) VALUES(?,?)', (sid, who))
                    table = 'Msg_' + hashlib.md5(who.encode()).hexdigest()
                    c.execute('CREATE TABLE ' + table + '(local_id,server_id,real_sender_id,create_time,sort_seq,local_type,message_content,compress_content)')
                    c.execute('INSERT INTO ' + table + ' VALUES(1,2,?,?,?,?,?,NULL)', (sid, stamp, stamp, 1, '会议 ' + who))
            (root / 'snapshot_manifest.json').write_text(manifest_with_dbs(db))
            before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in db.rglob('*') if p.is_file()}
            args = Namespace(conversation_id=None, keyword='会议', since=0, until=300, limit=1)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                q.query_messages(args, root)
            result = json.loads(stdout.getvalue())
            self.assertEqual(result['messages'][0]['sender_id'], 'bob')
            self.assertEqual(result['messages'][0]['send_time'], 200)
            self.assertEqual(before, {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in db.rglob('*') if p.is_file()})
            self.assertFalse(list(db.rglob('*-wal')))
            sidecar = db / 'contact/contact.db-wal'; sidecar.write_bytes(b'not empty')
            with self.assertRaises(RuntimeError):
                q.identities(db)
            existing = root / 'existing'; existing.mkdir()
            with patch.object(b, 'recover', side_effect=AssertionError('must not scan')):
                with self.assertRaises(FileExistsError):
                    b.snapshot(Namespace(db_dir=str(db),out_dir=str(existing),base_only=False))

    def test_same_second_queries_use_sequence_across_and_within_shards(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); db = root / 'db'
            for folder in ('contact', 'session', 'message'):
                (db / folder).mkdir(parents=True)
            with writable(db / 'contact/contact.db') as c:
                c.execute('CREATE TABLE contact(username,nick_name,remark)')
            with writable(db / 'session/session.db') as c:
                c.execute('CREATE TABLE SessionTable(username)')
            table = 'Msg_' + hashlib.md5(b'alice').hexdigest()
            fixtures = [
                ('message_0.db', [(1, 100, 3000), (99, 100, 2000), (100, 99, 9999)]),
                ('message_1.db', [(101, 100, 1000)]),
            ]
            for filename, rows in fixtures:
                with writable(db / 'message' / filename) as c:
                    c.execute('CREATE TABLE Name2Id(user_name)')
                    c.execute("INSERT INTO Name2Id VALUES('alice')")
                    c.execute('CREATE TABLE ' + table + '(local_id,server_id,real_sender_id,create_time,sort_seq,local_type,message_content,compress_content)')
                    c.executemany('INSERT INTO ' + table + ' VALUES(?,2,1,?,?,1,?,NULL)',
                                  [(local, stamp, seq, '会议 ' + str(seq)) for local, stamp, seq in rows])
            (root / 'snapshot_manifest.json').write_text(manifest_with_dbs(db))
            before = {p: p.read_bytes() for p in db.rglob('*.db')}
            for keyword in (None, '会议'):
                for limit in (1, 2, 4):
                    with self.subTest(keyword=keyword, limit=limit):
                        args = Namespace(conversation_id='alice', keyword=keyword, since=99, until=100, limit=limit)
                        stdout = io.StringIO()
                        with redirect_stdout(stdout):
                            q.query_messages(args, root)
                        records = json.loads(stdout.getvalue())['messages']
                        self.assertEqual([r['sequence'] for r in records], [3000, 2000, 1000, 9999][:limit])
            self.assertEqual(before, {p: p.read_bytes() for p in db.rglob('*.db')})

    def snapshot_fixture(self, root):
        source = root / 'source'
        for name in ('contact/contact.db', 'session/session.db', 'message/message_0.db'):
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(bytes(b.PAGE))
        args = Namespace(db_dir=str(source), out_dir=str(root / 'snapshot'), base_only=False,
                         timeout=1, verbose=False)
        keys = {p.relative_to(source).as_posix(): (self.key, self.salt) for p in source.rglob('*.db')}
        return source, args, keys

    def test_compressed_content_failures_are_preserved_and_counted(self):
        from compression import zstd
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); db = root / 'db'
            for folder in ('contact', 'session', 'message'):
                (db / folder).mkdir(parents=True)
            with writable(db / 'contact/contact.db') as c:
                c.execute('CREATE TABLE contact(username,nick_name,remark)')
            with writable(db / 'session/session.db') as c:
                c.execute('CREATE TABLE SessionTable(username)')
            bad_zstd = b'\x28\xb5\x2f\xfd\xff'
            rows = [(1, '', b'\xff'), (2, None, bad_zstd),
                    (3, b'\xff', zstd.compress('会议恢复'.encode())),
                    (4, '会议正文', bad_zstd), (5, b'\xff', bad_zstd),
                    (6, '', zstd.compress(b''))]
            table = 'Msg_' + hashlib.md5(b'alice').hexdigest()
            with writable(db / 'message/message_0.db') as c:
                c.execute('CREATE TABLE Name2Id(user_name)')
                c.execute("INSERT INTO Name2Id VALUES('alice')")
                c.execute('CREATE TABLE ' + table + '(local_id,server_id,real_sender_id,create_time,sort_seq,local_type,message_content,compress_content)')
                c.executemany('INSERT INTO ' + table + ' VALUES(?,2,1,100,1,1,?,?)', rows)
            (root / 'snapshot_manifest.json').write_text(manifest_with_dbs(db))
            for keyword in (None, '会议'):
                args = Namespace(conversation_id='alice', keyword=keyword, since=None, until=None, limit=10)
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    q.query_messages(args, root)
                result = json.loads(stdout.getvalue())
                self.assertEqual(result['scanned_unresolved_messages'], 3)
                records = {r['local_id']: r for r in result['messages']}
                self.assertEqual(records[3]['parse_status'], 'compress_content:zstd')
                self.assertEqual(records[4]['parse_status'], 'utf8')
                if keyword is None:
                    self.assertEqual(records[1]['parse_status'], 'compress_content:binary_unresolved')
                    self.assertEqual(records[2]['parse_status'], 'compress_content:zstd_unresolved')
                    self.assertIn('binary_unresolved', records[5]['parse_status'])
                    self.assertIn('compress_content:zstd_unresolved', records[5]['parse_status'])
                    self.assertEqual(records[6]['parse_status'], 'compress_content:zstd')
                else:
                    self.assertEqual(set(records), {3, 4})

    def inspection_fixture(self, root):
        db = root / 'db'
        manifest = {'format': 'wechat-local-snapshot-v1', 'success': True, 'failed': 0,
                    'complete': True, 'wal_processed': True, 'databases': []}
        for name in ('contact/contact.db', 'session/session.db', 'message/message_0.db'):
            path = db / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with writable(path) as c:
                c.execute('CREATE TABLE data(value)')
                c.execute("INSERT INTO data VALUES('before')")
            manifest['databases'].append({'relative_path': name, 'quick_check': 'ok',
                                         'quick_check_scope': 'cached_scope',
                                         'snapshot_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        (root / 'snapshot_manifest.json').write_text(json.dumps(manifest))
        return manifest

    def test_inspection_reruns_checks_and_matches_hash_without_writing(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = self.inspection_fixture(root)
            before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in root.rglob('*') if p.is_file()}
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(q.dispatch(Namespace(command='inspect', decrypted_dir=str(root))), 0)
            records = json.loads(stdout.getvalue())['databases']
            for actual, recorded in zip(records, manifest['databases']):
                self.assertEqual(actual['quick_check'], 'ok')
                self.assertEqual(actual['quick_check_scope'], 'database')
                self.assertTrue(actual['hash_matches'])
                self.assertEqual(actual['snapshot_sha256'], recorded['snapshot_sha256'])
            self.assertEqual(before, {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in root.rglob('*') if p.is_file()})

    def test_inspection_rejects_valid_but_modified_or_corrupt_database(self):
        for corruption in (False, True):
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                self.inspection_fixture(root)
                path = root / 'db/contact/contact.db'
                if corruption:
                    path.write_bytes(b'not SQLite')
                else:
                    size = path.stat().st_size
                    with writable(path) as c:
                        c.execute("UPDATE data SET value='after!'")
                    self.assertEqual(path.stat().st_size, size)
                with redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
                    q.dispatch(Namespace(command='inspect', decrypted_dir=str(root)))

    def test_inspection_rejects_missing_hash_and_nonempty_plaintext_wal(self):
        for missing_hash in (False, True):
            with self.subTest(missing_hash=missing_hash), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                manifest = self.inspection_fixture(root)
                if missing_hash:
                    del manifest['databases'][0]['snapshot_sha256']
                    (root / 'snapshot_manifest.json').write_text(json.dumps(manifest))
                else:
                    (root / 'db/contact/contact.db-wal').write_bytes(b'nonempty')
                with redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
                    q.dispatch(Namespace(command='inspect', decrypted_dir=str(root)))

    def test_stable_capture_retries_sidecar_disappearance_on_either_read(self):
        for suffix in ('-wal', '-shm'):
            for fail_at in (1, 2):
                with self.subTest(suffix=suffix, fail_at=fail_at), tempfile.TemporaryDirectory() as raw:
                    root = Path(raw); source = root / 'source.db'
                    source.write_bytes(self.page(1)[0])
                    Path(str(source) + '-wal').write_bytes(self.wal())
                    Path(str(source) + '-shm').write_bytes(self.shm())
                    calls = 0
                    original_read = Path.read_bytes
                    original_shm = b.read_shm_header
                    def maybe_disappear(path):
                        nonlocal calls
                        if str(path).endswith(suffix):
                            calls += 1
                            if calls == fail_at:
                                raise FileNotFoundError('synthetic sidecar churn')
                        return original_read(path) if suffix == '-wal' else original_shm(path)
                    target, method = (Path, 'read_bytes') if suffix == '-wal' else (b, 'read_shm_header')
                    with patch.object(target, method, maybe_disappear), patch.object(b.time, 'sleep'):
                        wal, info = b.stable_capture(source, root / 'captured.db')
                    self.assertEqual(info['capture_attempts'], 2)
                    self.assertEqual(wal, self.wal())
                    self.assertEqual(info['wal_index'], self.wal_index())
                    self.assertEqual((root / 'captured.db').read_bytes(), source.read_bytes())

    def test_sidecar_retries_are_bounded_and_do_not_hide_permission_errors(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / 'source.db'
            source.write_bytes(self.page(1)[0])
            with patch.object(b, 'read_shm_header', side_effect=FileNotFoundError('synthetic churn')) as reader, \
                 patch.object(b.time, 'sleep'):
                with self.assertRaisesRegex(RuntimeError, 'remained busy'):
                    b.stable_capture(source, root / 'captured.db')
                self.assertEqual(reader.call_count, 12)
            with patch.object(b, 'read_shm_header', side_effect=PermissionError('synthetic denied')) as reader:
                with self.assertRaises(PermissionError):
                    b.stable_capture(source, root / 'captured.db')
                self.assertEqual(reader.call_count, 1)

    def fake_snapshot_one(self, backend, source, dest, *key):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b'synthetic plaintext')
        return {'quick_check': 'ok', 'applied_frames': 0}

    def test_snapshot_excludes_only_reserved_database_and_publishes_after_validation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source, args, keys = self.snapshot_fixture(root)
            reserved = source / 'Migrate/Unspportmsg.db'
            reserved.parent.mkdir(); reserved.write_bytes(b'')
            other = source / 'message/unspportmsg.db'; other.write_bytes(bytes(b.PAGE))
            keys['message/unspportmsg.db'] = (self.key, self.salt)
            before = {p: p.read_bytes() for p in source.rglob('*.db')}
            output = Path(args.out_dir)
            def capture(backend, path, dest, *key):
                self.assertFalse(output.exists())
                self.assertEqual(dest.parents[2].resolve().parent, output.parent.resolve())
                return self.fake_snapshot_one(backend, path, dest, *key)
            with patch.object(b, 'recover', return_value=keys) as recover, \
                 patch.object(b, 'snapshot_one', side_effect=capture), redirect_stdout(io.StringIO()):
                self.assertEqual(b.snapshot(args), 0)
            self.assertEqual(set(recover.call_args.args[0]), set(keys))
            manifest = json.loads((output / 'snapshot_manifest.json').read_text('utf-8'))
            self.assertTrue(manifest['success'] and manifest['complete'])
            self.assertEqual(manifest['output'], str(output))
            self.assertEqual(manifest['skipped_databases'][0]['relative_path'], 'Migrate/Unspportmsg.db')
            self.assertEqual(len(manifest['databases']), 4)
            self.assertEqual(len(list((output / 'db').rglob('*.db'))), 4)
            self.assertEqual(list(root.glob('.snapshot.staging-*')), [])
            self.assertEqual(before, {p: p.read_bytes() for p in source.rglob('*.db')})

    def test_snapshot_failure_or_interrupt_cleans_plaintext_and_allows_retry(self):
        for error in (RuntimeError('synthetic validation failure'), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                source, args, keys = self.snapshot_fixture(root)
                before = {p: p.read_bytes() for p in source.rglob('*.db')}
                calls = 0
                def capture(backend, path, dest, *key):
                    nonlocal calls
                    calls += 1
                    info = self.fake_snapshot_one(backend, path, dest, *key)
                    if calls == 2:
                        dest.with_suffix('.capture').write_bytes(b'synthetic encrypted copy')
                        raise error
                    return info
                stdout = io.StringIO()
                with patch.object(b, 'recover', return_value=keys), \
                     patch.object(b, 'snapshot_one', side_effect=capture), redirect_stdout(stdout):
                    with self.assertRaises(type(error)):
                        b.snapshot(args)
                    self.assertFalse(Path(args.out_dir).exists())
                    self.assertEqual(list(root.glob('.snapshot.staging-*')), [])
                    self.assertEqual(stdout.getvalue(), '')
                    self.assertEqual(b.snapshot(args), 0)
                self.assertEqual(before, {p: p.read_bytes() for p in source.rglob('*.db')})

    def test_snapshot_publication_failure_cleans_staging(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source, args, keys = self.snapshot_fixture(root)
            with patch.object(b, 'recover', return_value=keys), \
                 patch.object(b, 'snapshot_one', side_effect=self.fake_snapshot_one), \
                 patch.object(Path, 'rename', side_effect=PermissionError('synthetic publication failure')):
                with self.assertRaisesRegex(PermissionError, 'publication failure'):
                    b.snapshot(args)
            self.assertFalse(Path(args.out_dir).exists())
            self.assertEqual(list(root.glob('.snapshot.staging-*')), [])

    def test_snapshot_reports_exact_staging_path_when_cleanup_fails(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source, args, keys = self.snapshot_fixture(root)
            def capture(backend, path, dest, *key):
                self.fake_snapshot_one(backend, path, dest, *key)
                raise RuntimeError('synthetic validation failure')
            with patch.object(b, 'recover', return_value=keys), \
                 patch.object(b, 'snapshot_one', side_effect=capture), \
                 patch.object(b.shutil, 'rmtree', side_effect=PermissionError('synthetic cleanup failure')):
                with self.assertRaisesRegex(RuntimeError, '明文暂存目录清理失败') as caught:
                    b.snapshot(args)
            staging, = root.glob('.snapshot.staging-*')
            self.assertIn(str(staging), str(caught.exception))
            self.assertIn('synthetic validation failure', str(caught.exception))
            self.assertFalse(Path(args.out_dir).exists())

    def test_scan_candidates_tolerates_odd_length_hex_runs(self):
        page = self.page(1)[0]
        pages = {'contact/contact.db': page}
        odd = b"x'" + b'ab' * 40 + b'c' + b"'"  # 81 hex chars; bytes.fromhex would reject it
        good = b"x'" + self.key.hex().encode() + b"'"
        keys, tested = {}, set()
        b.scan_candidates(odd + b'  ' + good, pages, keys, tested)
        self.assertEqual(keys['contact/contact.db'], (self.key, page[:16]))
        keys2, tested2 = {}, set()
        b.scan_candidates(odd, pages, keys2, tested2)  # odd run alone must not raise
        self.assertEqual(keys2, {})

    def query_fixture(self, db, rows, user='alice'):
        for folder in ('contact', 'session', 'message'):
            (db / folder).mkdir(parents=True, exist_ok=True)
        with writable(db / 'contact/contact.db') as c:
            c.execute('CREATE TABLE contact(username,nick_name,remark)')
        with writable(db / 'session/session.db') as c:
            c.execute('CREATE TABLE SessionTable(username)')
        table = 'Msg_' + hashlib.md5(user.encode()).hexdigest()
        with writable(db / 'message/message_0.db') as c:
            c.execute('CREATE TABLE Name2Id(user_name)')
            c.execute('INSERT INTO Name2Id VALUES(?)', (user,))
            c.execute('CREATE TABLE ' + table + '(local_id,server_id,real_sender_id,create_time,sort_seq,local_type,message_content,compress_content)')
            c.executemany('INSERT INTO ' + table + ' VALUES(?,2,1,100,1,?,?,NULL)', rows)
        (db.parent / 'snapshot_manifest.json').write_text(manifest_with_dbs(db))

    def test_null_local_type_is_rendered_without_crashing(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.query_fixture(root / 'db', [(1, None, '会议'), (2, 1 << 32 | 3, '图片占位')])
            args = Namespace(conversation_id='alice', keyword=None, since=None, until=None, limit=10)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                q.query_messages(args, root)
            records = {r['local_id']: r for r in json.loads(stdout.getvalue())['messages']}
            self.assertIsNone(records[1]['content_type'])
            self.assertEqual(records[1]['type_name'], 'None')
            self.assertEqual(records[1]['text'], '会议')
            self.assertEqual(records[2]['type_name'], '图片')  # composite type still uses low 32 bits

    def test_query_refuses_snapshot_whose_databases_drift_from_manifest(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); db = root / 'db'
            self.query_fixture(db, [(1, 1, '会议一')])
            table = 'Msg_' + hashlib.md5(b'alice').hexdigest()
            with writable(db / 'message/message_1.db') as c:
                c.execute('CREATE TABLE Name2Id(user_name)')
                c.execute("INSERT INTO Name2Id VALUES('alice')")
                c.execute('CREATE TABLE ' + table + '(local_id,server_id,real_sender_id,create_time,sort_seq,local_type,message_content,compress_content)')
                c.execute('INSERT INTO ' + table + ' VALUES(2,2,1,200,1,1,?,NULL)', ('会议二',))
            (root / 'snapshot_manifest.json').write_text(manifest_with_dbs(db))
            args = Namespace(conversation_id=None, keyword=None, since=None, until=None, limit=10)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                q.query_messages(args, root)
            self.assertEqual(json.loads(stdout.getvalue())['count'], 2)
            (db / 'message/message_1.db').unlink()  # manifest still lists it
            with redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, '不一致'):
                q.query_messages(args, root)
            (db / 'message/message_1.db').write_bytes(bytes(b.PAGE))
            (db / 'message/message_9.db').write_bytes(bytes(b.PAGE))  # not in manifest
            with redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, '不一致'):
                q.query_sessions(Namespace(keyword=None, limit=10), root)

    def test_redaction_covers_prefixed_and_bare_credentials(self):
        leaky = ('https://api.weixin.qq.com/cgi-bin/media/get?access_token=ACCESS_SECRET&media_id=123 '
                 'token=TOKEN_SECRET refresh_token: REFRESH_SECRET my_api_key=KEYSECRET123')
        cleaned = q.redact(leaky)
        for secret in ('ACCESS_SECRET', 'TOKEN_SECRET', 'REFRESH_SECRET', 'KEYSECRET123'):
            self.assertNotIn(secret, cleaned)
        self.assertIn('media_id=123', cleaned)
        plain = '下周三 10:00 开会，会议室 key 在前台'
        self.assertEqual(q.redact(plain), plain)

    def test_non_utf8_text_columns_do_not_crash_queries(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); db = root / 'db'
            self.query_fixture(db, [(1, 1, '会议')])
            with writable(db / 'contact/contact.db') as c:
                c.execute('INSERT INTO contact VALUES(?,?,?)', (b'\xff\xfe', b'\xff', 'bad'))
            names, hashes = q.identities(q.db_root(root))
            self.assertIn(hashlib.md5(b'alice').hexdigest(), hashes)
            self.assertIn(hashlib.md5(b'\xff\xfe').hexdigest(), hashes)
            for handler, args in (
                (q.query_sessions, Namespace(keyword=None, limit=10)),
                (q.query_messages, Namespace(conversation_id='alice', keyword=None, since=None, until=None, limit=10)),
            ):
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    handler(args, root)
                json.loads(stdout.getvalue())  # valid JSON ⇒ no bytes leaked into output

    def test_redaction_is_not_pathologically_slow(self):
        # Each input targets a different quantifier: the bounded prefix (alnum run),
        # the cdn core, and the escaped-quoted-value alternation (backslash bomb with
        # no closing quote). All must stay far from the pre-fix multi-second blowups.
        for label, blob in (('alnum', 'A1' * 60000), ('cdn', 'cdn' * 60000),
                            ('backslash', 'aeskey="' + chr(92) * 400)):
            start = time.perf_counter()
            q.redact(blob)
            elapsed = time.perf_counter() - start
            self.assertLess(elapsed, 5, f'{label} redact took {elapsed:.2f}s')

    def test_redaction_matches_opening_quote_and_does_not_leak_value_tail(self):
        for text in ('{"password":"abc\'defSECRET"}', 'aeskey="abc\'defSECRET"',
                     "authkey='va\"lSECRET'", '{"access_token":"ab\'cdSECRET"}',
                     'aeskey="ab\\"cdSECRET"', '{"password":123456789}',
                     '{"token": 987654321}'):
            self.assertNotIn('SECRET', q.redact(text))
            self.assertNotIn('123456789', q.redact('{"password":123456789}'))
            self.assertNotIn('987654321', q.redact('{"token": 987654321}'))

    def test_redaction_does_not_over_redact_benign_suffix_fields(self):
        for text in ('hotkey="Ctrl+C"', 'monkey=banana123', '<turnkey>door</turnkey>'):
            self.assertEqual(q.redact(text), text)

    def test_output_pipeline_handles_emoji_under_legacy_code_page(self):
        sink = io.TextIOWrapper(io.BytesIO(), encoding='cp936', errors='strict', newline='')
        original = sys.stdout
        sys.stdout = sink
        try:
            wc._force_utf8_output()
            b.emit(success=True, app='wechat', text='安排好啦 👋')
            sink.flush()
            raw = sink.buffer.getvalue()
        finally:
            sys.stdout = original
        self.assertIn('👋', raw.decode('utf-8'))

    def test_emit_tolerates_stray_bytes_values(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            b.emit(success=True, server_id=b'\xff\xfe', note='ok')  # bytes in a numeric field
        data = json.loads(stdout.getvalue())  # must be valid JSON, not a TypeError
        self.assertEqual(data['note'], 'ok')


if __name__ == '__main__':
    unittest.main()
