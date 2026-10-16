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
        header = struct.pack('>IIII', 0x377f0682, 3007000, 4096, 0) + b'GEN12345'
        state = b.checksum(header, '<')
        out = header + struct.pack('>II', *state)
        for number in range(1, frames + 1):
            page, _ = self.page(number)
            frame = struct.pack('>II', number, number if number == commit_at else 0) + b'GEN12345'
            state = b.checksum(frame[:8] + page, '<', state)
            out += frame + struct.pack('>II', *state) + page
        return out

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
        self.assertEqual(len(b.committed_wal(wal, self.aes, self.mkey, 2)[0]), 2)
        self.assertEqual(b.committed_wal(wal, self.aes, self.mkey, 0)[0], [])
        with self.assertRaises(RuntimeError):
            b.committed_wal(wal, self.aes, self.mkey, 3)
        stale = bytearray(wal); stale[40:48] = b'OLD12345'
        self.assertEqual(b.committed_wal(stale, self.aes, self.mkey)[0], [])
        with self.assertRaises(RuntimeError):
            b.committed_wal(stale, self.aes, self.mkey, 2)

    def test_wal_damage_is_not_silently_ignored(self):
        wal = self.wal()
        for offset in (10, 33, 100, 4145):
            corrupt = bytearray(wal); corrupt[offset] ^= 1
            with self.assertRaises(RuntimeError):
                b.committed_wal(corrupt, self.aes, self.mkey, 2)
        with self.assertRaises(RuntimeError):
            b.committed_wal(wal[:100], self.aes, self.mkey, 2)

    def test_wal_index_and_timeout(self):
        head = bytearray(40)
        struct.pack_into('<IIIBBHII', head, 0, 3007000, 0, 1, 1, 0, 4096, 2, 2)
        header = bytes(head) + struct.pack('<II', *b.checksum(head, '<'))
        self.assertEqual(b.wal_index_frame_count(header * 2), 2)
        with self.assertRaises(RuntimeError):
            b.wal_index_frame_count(header + bytes(48))
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


if __name__ == '__main__':
    unittest.main()
