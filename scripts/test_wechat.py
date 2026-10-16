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
import tempfile
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
            (root / 'snapshot_manifest.json').write_text(json.dumps({'format':'wechat-local-snapshot-v1','success':True,'failed':0,'complete':True,'wal_processed':True}))
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
            (root / 'snapshot_manifest.json').write_text(json.dumps({'format':'wechat-local-snapshot-v1','success':True,'failed':0,'complete':True,'wal_processed':True}))
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


if __name__ == '__main__':
    unittest.main()
