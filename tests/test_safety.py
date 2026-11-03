"""Failure-injection and adversarial archive tests: stdlib only, no Qt/network."""
import io
import json
import os
import subprocess
import tarfile
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from drstool_core import dll_ops, file_transactions as tx, safe_archive, support_bundle


class TransactionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.root = Path(self.td.name)
        (self.root / 'game').mkdir()
        self.a = self.root / 'game/a.dll'
        self.b = self.root / 'game/b.dll'
        self.a.write_bytes(b'old-a')
        self.b.write_bytes(b'old-b')
        self.sa = self.root / 'new-a'
        self.sb = self.root / 'new-b'
        self.sa.write_bytes(b'new-a')
        self.sb.write_bytes(b'new-b')

    def _changes(self):
        return [tx.Change(self.a, self.sa, 'a.dll'), tx.Change(self.b, self.sb, 'b.dll')]

    def test_swap_restores_all_and_does_not_repeat(self):
        backup = self.root / 'backup'
        self.assertEqual(tx.apply(self._changes(), backup), [self.a, self.b])
        self.assertEqual(self.a.read_bytes(), b'new-a')
        count, notes = tx.restore(backup)
        self.assertEqual((count, notes), (2, []))
        count, notes = tx.restore(backup)
        self.assertEqual((count, notes), (0, []))
        self.assertEqual(self.a.read_bytes(), b'old-a')

    def test_rolls_back_first_file_if_second_write_fails(self):
        original = tx.atomic_copy
        calls = [0]

        def fail_once(src, dest):
            calls[0] += 1
            if calls[0] == 2:
                raise OSError('simulated full disk')
            return original(src, dest)

        with mock.patch.object(tx, 'atomic_copy', fail_once):
            with self.assertRaisesRegex(OSError, 'full disk'):
                tx.apply(self._changes(), self.root / 'backup')
        self.assertEqual(self.a.read_bytes(), b'old-a')
        self.assertEqual(self.b.read_bytes(), b'old-b')
        self.assertEqual(json.loads((self.root / 'backup/transaction.json').read_text())['state'], 'rolled-back')

    def test_interrupted_write_ahead_log_can_restore(self):
        backup = self.root / 'backup'
        tx.apply([self._changes()[0]], backup)
        journal = json.loads((backup / 'transaction.json').read_text())
        journal['state'] = 'prepared'
        journal['entries'][0]['state'] = 'prepared'  # crash before applied flag
        tx.atomic_write_json(backup / 'transaction.json', journal)
        self.assertEqual(tx.restore(backup)[0], 1)
        self.assertEqual(self.a.read_bytes(), b'old-a')

    def test_modified_file_never_overwritten(self):
        backup = self.root / 'backup'
        tx.apply([self._changes()[0]], backup)
        self.a.write_bytes(b'patched-game')
        n, notes = tx.restore(backup)
        self.assertEqual(n, 0)
        self.assertTrue(any('externally' in note for note in notes))
        self.assertEqual(self.a.read_bytes(), b'patched-game')

    def test_corrupt_backup_not_restored(self):
        backup = self.root / 'backup'
        tx.apply([self._changes()[0]], backup)
        (backup / 'files/a.dll').write_bytes(b'broken')
        n, notes = tx.restore(backup)
        self.assertEqual(n, 0)
        self.assertTrue(any('corrupt' in note for note in notes))

    def test_symlink_destination_refused(self):
        self.a.unlink()
        self.a.symlink_to(self.b)
        with self.assertRaises(ValueError):
            tx.apply([self._changes()[0]], self.root / 'backup')
        self.assertEqual(self.b.read_bytes(), b'old-b')

    def test_atomic_manifest(self):
        path = self.root / 'metadata.json'
        tx.atomic_write_json(path, {'value': 123})
        self.assertEqual(json.loads(path.read_text()), {'value': 123})
        self.assertFalse(list(self.root.glob('.drstool-manifest-*')))

    def test_file_lock_serializes_threads(self):
        import concurrent.futures
        path = self.root / 'state.json'
        path.write_text('0')

        def advance(_):
            with tx.file_lock(path):
                x = int(path.read_text())
                path.write_text(str(x + 1))

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(advance, range(100)))
        self.assertEqual(path.read_text(), '100')


class ArchiveSafetyTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.root = Path(self.td.name)

    def _tar(self, name, entries):
        file = self.root / name
        with tarfile.open(file, 'w:gz') as tf:
            for path, content in entries:
                ti = tarfile.TarInfo(path)
                ti.size = len(content)
                tf.addfile(ti, io.BytesIO(content))
        return file

    def test_tar_traversal_rejected(self):
        archive = self._tar('bad.tar.gz', [('../../pwned.txt', b'exploit')])
        with self.assertRaises(ValueError):
            safe_archive.extract_tar(archive, self.root / 'out')
        self.assertFalse((self.root / 'pwned.txt').exists())

    def test_tar_links_rejected(self):
        archive = self.root / 'link.tar'
        with tarfile.open(archive, 'w') as tf:
            ti = tarfile.TarInfo('folder/secret')
            ti.type = tarfile.SYMTYPE
            ti.linkname = '/etc/passwd'
            tf.addfile(ti)
        with self.assertRaises(ValueError):
            safe_archive.extract_tar(archive, self.root / 'out', allow_internal_links=True)

    def test_tar_internal_link_allowed(self):
        archive = self.root / 'link.tar'
        with tarfile.open(archive, 'w') as tf:
            ti = tarfile.TarInfo('folder/version.txt')
            ti.size = 3
            tf.addfile(ti, io.BytesIO(b'abc'))
            ti = tarfile.TarInfo('folder/latest.txt')
            ti.type = tarfile.SYMTYPE
            ti.linkname = 'version.txt'
            tf.addfile(ti)
        dest = self.root / 'out'
        safe_archive.extract_tar(archive, dest, allow_internal_links=True)
        self.assertEqual((dest / 'folder/latest.txt').read_bytes(), b'abc')

    def test_rejects_tar_special_device(self):
        archive = self.root / 'device.tar'
        with tarfile.open(archive, 'w') as tf:
            ti = tarfile.TarInfo('dev')
            ti.type = tarfile.CHRTYPE
            tf.addfile(ti)
        with self.assertRaises(ValueError):
            safe_archive.extract_tar(archive, self.root / 'out')

    def test_zip_traversal_and_symlink_rejected(self):
        archive = self.root / 'bad.zip'
        with zipfile.ZipFile(archive, 'w') as zf:
            zf.writestr('../../hijack', b'a')
        with self.assertRaises(ValueError):
            safe_archive.extract_zip(archive, self.root / 'out')
        archive2 = self.root / 'link.zip'
        with zipfile.ZipFile(archive2, 'w') as zf:
            zi = zipfile.ZipInfo('escape')
            zi.create_system = 3
            zi.external_attr = (0o120777 << 16)
            zf.writestr(zi, '../../outside')
        with self.assertRaises(ValueError):
            safe_archive.extract_zip(archive2, self.root / 'out2')

    def test_zip_bomb_budget(self):
        archive = self.root / 'bomb.zip'
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('huge', b'x' * 7_000_000)
        with self.assertRaises(ValueError):
            safe_archive.extract_zip(archive, self.root / 'out')

    @unittest.skipUnless(bool(__import__('shutil').which('zstd')), 'zstd missing')
    def test_tar_zst_safe_stream(self):
        base = self._tar('good.tar.gz', [('contents/file', b'expected')])
        archive = self.root / 'good.tar.zst'
        raw = self.root / 'good.tar'
        with tarfile.open(raw, 'w') as tf:
            ti = tarfile.TarInfo('contents/file')
            ti.size = len(b'expected')
            tf.addfile(ti, io.BytesIO(b'expected'))
        subprocess.run(['zstd', '-q', '-f', str(raw), '-o', str(archive)], check=True)
        safe_archive.extract_tar(archive, self.root / 'out')
        self.assertEqual((self.root / 'out/contents/file').read_bytes(), b'expected')


class PrivacyTests(unittest.TestCase):
    def test_bearer_and_query_tokens_redacted(self):
        text = 'Authorization: Bearer abcDEFG1234 and https://api/a?token=EXPOSED&mode=1'
        safe = support_bundle.redact(text)
        self.assertNotIn('EXPOSED', safe)
        self.assertNotIn('abcDEFG1234', safe)

    def test_nested_sensitive_profile_key(self):
        p = {'Steam': {'api_key': 'abcdef', 'env_vars': {'GITHUB_TOKEN': 'secret', 'DXVK_HUD': 'fps'}}}
        result = support_bundle._redact_obj(p)
        self.assertEqual(result['Steam']['api_key'], '[REDACTED]')
        self.assertEqual(result['Steam']['env_vars']['GITHUB_TOKEN'], '[REDACTED]')
        self.assertEqual(result['Steam']['env_vars']['DXVK_HUD'], 'fps')


if __name__ == '__main__':
    unittest.main()
