"""F01–F11 audit regression tests. No Wine, GPU, network or Qt required."""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import signal
import struct
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from drstool_core import dll_ops, file_transactions as tx, pe_version, tool_recovery

ROOT = Path(__file__).resolve().parents[1]


def pe(path: Path, machine=0x8664, content=b'old') -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    dos = bytearray(128)
    dos[:2] = b'MZ'
    struct.pack_into('<I', dos, 0x3c, 128)
    path.write_bytes(dos + b'PE\0\0' + struct.pack('<H', machine) + b'\0' * 18 + content)
    return path


def gui_snippets(rel: str, names: set[str], env: dict) -> dict:
    """Execute *actual* pure GUI functions/classes without importing PySide6.

    The AST is taken directly from the shipped GUI, not a rewritten copy.
    This catches Qt-free behavioural bugs in headless CI.
    """
    tree = ast.parse((ROOT / rel).read_text(encoding='utf-8'))
    selected = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                and node.name in names]
    assert len(selected) == len(names), names - {node.name for node in selected}
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, *selected], type_ignores=[]))
    exec(compile(module, rel, 'exec'), env)
    return env


class AtomicDllRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_f01_post_commit_metadata_failure_never_reports_rollback(self):
        target = pe(self.root / 'game/a.dll')
        source = pe(self.root / 'src/a.dll', content=b'new')
        with mock.patch.object(dll_ops, '_write_json', side_effect=OSError('no space')):
            res = dll_ops.replace_files('game', 'Game', [(target, source, 'upgrade')],
                                        root=self.root / 'backups')
        self.assertEqual(res.status, 'committed_metadata_error')
        self.assertEqual(res.replaced, [target])
        self.assertEqual(target.read_bytes(), source.read_bytes())
        self.assertTrue((res.backup_dir / 'manifest.json').is_file())
        restored, problems = dll_ops.restore(res.backup_dir)
        self.assertEqual((restored, problems), (1, []))

    def test_f01_final_journal_commit_error_reports_files_installed(self):
        target = pe(self.root / 'game/a.dll')
        source = pe(self.root / 'src/a.dll', content=b'new')
        actual = tx.atomic_write_json
        def fail_commit(path, journal):
            if journal.get('state') == 'committed':
                raise OSError('disk full')
            return actual(path, journal)
        with mock.patch.object(tx, 'atomic_write_json', side_effect=fail_commit):
            result = dll_ops.replace_files('key', 'Game', [(target, source, 'new')],
                                           root=self.root / 'backups')
        self.assertEqual(result.status, 'committed_metadata_error')
        self.assertEqual(result.replaced, [target])
        self.assertEqual(target.read_bytes(), source.read_bytes())

    def test_f02_mismatched_architectures_refused_in_both_directions(self):
        for dest, src in ((0x14c, 0x8664), (0x8664, 0x14c)):
            target = pe(self.root / f'{dest}/game.dll', dest)
            source = pe(self.root / f'{dest}/src.dll', src, b'new')
            original = target.read_bytes()
            result = dll_ops.replace_files('game', 'G', [(target, source, 'new')],
                                           root=self.root / 'backups')
            self.assertFalse(result.replaced)
            self.assertIn('architecture mismatch', result.skipped[0][1])
            self.assertEqual(target.read_bytes(), original)

    def test_f02_matching_architectures_accepted(self):
        for machine in (0x14c, 0x8664):
            target = pe(self.root / f'{machine}/game.dll', machine)
            source = pe(self.root / f'{machine}/src.dll', machine, b'new')
            result = dll_ops.replace_files(str(machine), 'G', [(target, source, 'new')],
                                           root=self.root / 'backups')
            self.assertEqual(result.replaced, [target], result.skipped)

    def test_f03_linked_parent_rejected_for_atomic_copy_and_tx(self):
        outside = self.root / 'outside'; outside.mkdir()
        target = pe(outside / 'game.dll')
        link = self.root / 'game' / 'bin'; link.parent.mkdir()
        link.symlink_to(outside, target_is_directory=True)
        source = pe(self.root / 'new.dll', content=b'new')
        initial = target.read_bytes()
        with self.assertRaises((OSError, ValueError)):
            tx.atomic_copy(source, link / 'game.dll')
        with self.assertRaises((OSError, ValueError)):
            tx.apply([tx.Change(link / 'game.dll', source, 'original.dll')], self.root / 'backup')
        self.assertEqual(target.read_bytes(), initial)
        self.assertTrue(link.is_symlink())

    def test_f03_linked_source_parent_rejected(self):
        outside = self.root / 'outside'; outside.mkdir()
        pe(outside / 'source.dll')
        link = self.root / 'game' / 'bin'; link.parent.mkdir()
        link.symlink_to(outside, target_is_directory=True)
        target = pe(self.root / 'target.dll')
        with self.assertRaises((OSError, ValueError)):
            tx.atomic_copy(link / 'source.dll', target)


class GuiLogicRegressions(unittest.TestCase):
    @staticmethod
    def environment():
        env = {'Path': Path, 're': re, 'json': json, 'shutil': shutil,
               'tx': tx, 'pe_version': pe_version, 'datetime': __import__('datetime').datetime,
               '_PD_MANIFEST': '.drstool-doctor.json', '_PD_ADDED': 'added.json',
               '_DLLM_NGX': ('dlss', 'dlssd', 'dlssg', 'sl_sdk_0'),
               '_DLLM_PFX': ('dxvk', 'vkd3d', 'nvapi'),
               '_DLLM_PD': {x: {'id': x} for x in ('dxvk', 'vkd3d', 'nvapi')},
               '_pick_asset': lambda *_: {'name':'archive'},
              }
        env['_sha256'] = tx.sha256
        env['scan_dll_version'] = lambda *_: ''
        return gui_snippets('drstool_gui/dll_manager.py',
                            {'prefix_sysdirs', 'read_prefix_manifest', 'detect_installed',
                             'dllm_prefix_updatable', 'dllm_plan', 'PrefixRestoreError',
                             'restore_prefix_backup', 'prefix_backup_contents',
                             '_vtuple', '_vcmp'}, env)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = Path(self.tmp.name)
        self.env = self.environment()

    def test_f04_partial_manifest_is_not_ok(self):
        folder = self.p / 'drive_c/windows/system32'; folder.mkdir(parents=True)
        a = folder / 'd3d11.dll'; a.write_bytes(b'first')
        b = folder / 'dxgi.dll'; b.write_bytes(b'second')
        manifest = {'dxvk': {'files': {'system32/d3d11.dll': tx.sha256(a)}, 'version': '3.1'}}
        result = self.env['detect_installed'](self.p, {'id': 'dxvk', 'dlls': ['d3d11', 'dxgi']},
                                              manifest_data=manifest)
        self.assertEqual(result['state'], 'mixed')
        manifest['dxvk']['files']['system32/dxgi.dll'] = tx.sha256(b)
        result = self.env['detect_installed'](self.p, {'id': 'dxvk', 'dlls': ['d3d11', 'dxgi']},
                                              manifest_data=manifest)
        self.assertEqual(result['state'], 'ok')
        b.write_bytes(b'external edit')
        result = self.env['detect_installed'](self.p, {'id': 'dxvk', 'dlls': ['d3d11', 'dxgi']},
                                              manifest_data=manifest)
        self.assertEqual(result['state'], 'mixed')

    def test_f05_downgrade_and_unknown_versions_not_planned(self):
        build = types.SimpleNamespace(version='310.3.0')
        g = types.SimpleNamespace(prefix=None, prefix_manager='')
        for current, want in (((310, 4, 0), False), ((310, 3, 0), False),
                              ((310, 2, 0), True), ((), False)):
            dll = types.SimpleNamespace(version=current)
            game_dlls = types.SimpleNamespace(of=lambda kind: [dll] if kind == 'dlss' else [])
            st = types.SimpleNamespace(game=g, dlls=game_dlls, prefix={})
            plan = self.env['dllm_plan'](st, {'dlss': build})
            self.assertEqual(bool(plan['files']), want)

    def test_f05_prefix_upgrade_never_downgrades_or_replaces_unknown(self):
        game = types.SimpleNamespace(prefix=self.p, prefix_manager='Lutris')
        dlls = types.SimpleNamespace(of=lambda _cid: [])
        release = {'dxvk': {'tag_name': 'v2.6', 'assets': []}}
        for state, have, wanted in [('ok', '2.7', False), ('ok', '2.6', False),
                                     ('ok', '2.5', True), ('ok', '', False),
                                     ('unknown', '', False), ('mixed', '2.4', False)]:
            st = types.SimpleNamespace(game=game, dlls=dlls,
                                       prefix={'dxvk': {'state': state, 'version': have}})
            plan = self.env['dllm_plan'](st, release)
            self.assertEqual('dxvk' in plan['prefix'], wanted, (state, have))

    def test_f09_prefix_backup_contents_current_and_legacy(self):
        base = self.p / 'backup'
        source = self.p / 'new'; source.write_bytes(b'new')
        dst = self.p / 'drive_c/windows/system32/d3d11.dll'
        dst.parent.mkdir(parents=True)
        dst.write_bytes(b'old')
        tx.apply([tx.Change(dst, source, 'system32/d3d11.dll')], base,
                 journal_name='prefix-dxvk.json')
        self.assertEqual(self.env['prefix_backup_contents'](base), ['system32/d3d11.dll'])
        legacy = base / 'syswow64/dxgi.dll'
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b'old')
        self.assertEqual(self.env['prefix_backup_contents'](base),
                         ['system32/d3d11.dll', 'syswow64/dxgi.dll'])

    def test_f06_qt_slot_logs_partial_restore_error(self):
        source = ast.parse((ROOT / 'drstool_gui/dll_manager.py').read_text())
        klass = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == 'DllRestoreDialog')
        method = next(n for n in klass.body if isinstance(n, ast.FunctionDef) and n.name == '_restore')
        namespace = {**self.env, 'Qt': types.SimpleNamespace(UserRole=0), 'dll_ops': dll_ops}
        namespace['restore_prefix_backup'] = lambda _: (_ for _ in ()).throw(
            self.env['PrefixRestoreError'](self.p / 'retained', 1, ['changed externally']))
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])),
                     'drstool_gui/dll_manager.py', 'exec'), namespace)
        notes = []
        item = types.SimpleNamespace(data=lambda *_: ('prefix', self.p))
        dialog = types.SimpleNamespace(_list=types.SimpleNamespace(currentItem=lambda: item),
                                       _log=types.SimpleNamespace(appendPlainText=notes.append),
                                       restored=False)
        namespace['_restore'](dialog)
        self.assertTrue(dialog.restored)
        self.assertTrue(any('retained' in line for line in notes))
        self.assertTrue(any('changed externally' in line for line in notes))

    def test_f08_bottles_and_proton_managed(self):
        for manager, expected in [('Bottles', False), ('Proton', False), ('Lutris', True)]:
            st = types.SimpleNamespace(game=types.SimpleNamespace(
                prefix=self.p, prefix_manager=manager))
            self.assertEqual(self.env['dllm_prefix_updatable'](st), expected)

    def test_f06_restore_conflict_retains_backup_and_reports_partial(self):
        prefix = self.p / 'prefix'
        s32 = prefix / 'drive_c/windows/system32'; s32.mkdir(parents=True)
        target = s32 / 'd3d11.dll'; target.write_bytes(b'old')
        new = self.p / 'new.dll'; new.write_bytes(b'new')
        backup = prefix / '.drstool-backup' / 'stamp'
        tx.apply([tx.Change(target, new, 'system32/d3d11.dll')], backup,
                 journal_name='prefix-dxvk.json')
        target.write_bytes(b'external game update')
        with self.assertRaises(self.env['PrefixRestoreError']) as cm:
            self.env['restore_prefix_backup'](prefix)
        self.assertEqual(cm.exception.restored, 0)
        self.assertEqual(cm.exception.backup, backup)
        self.assertTrue(backup.is_dir())
        self.assertEqual(target.read_bytes(), b'external game update')


class ToolManagerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = Path(self.tmp.name)
        self.name = 'Proton-A'
        self.target = self.p / self.name
        self.stage = self.p / 'stage' / self.name
        self.stage.mkdir(parents=True)
        (self.stage / 'protondl_version.json').write_text(json.dumps({
            'compat_tool': 'Proton', 'version': 'new'}))

    def old(self):
        self.target.mkdir()
        (self.target / 'original').write_text('old')

    def journal(self):
        return tool_recovery.create_journal(self.p, [self.stage],
                                            tool='Proton', version='new', main=self.name)

    def test_f07_real_tm_install_swap_and_recovery_integration(self):
        # Exercise the original tm_install() implementation with injected
        # download/extraction, no network and no Qt import.
        import hashlib
        class FakeLauncher:
            key = 'custom'; label = 'Local'
            def dir_for(self, _kind): return self.p
            def supports(self, _tool): return True
        launcher = FakeLauncher(); launcher.p = self.p
        def make_cache():
            folder = self.p / 'cache'; folder.mkdir(exist_ok=True)
            return folder
        def download(_url, path, *_args): path.write_bytes(b'archive')
        def extract(_tool, _ver, _archive, stage, *_args):
            tool_dir = stage / 'Proton-A'; tool_dir.mkdir()
            (tool_dir / 'new-binary').write_text('installed')
        def tm_read_info(where):
            try: return json.loads((where / 'protondl_version.json').read_text())
            except OSError: return None
        env = {'Path': Path, 'os': os, 'json': json, 're': re, 'shutil': shutil,
               'time': time, 'tempfile': tempfile, 'hashlib': hashlib,
               'file_lock': tx.file_lock, 'atomic_write_json': tx.atomic_write_json,
               'tool_recovery': tool_recovery, '_tm_cache_dir': make_cache,
               '_tm_download': download, '_tm_verify': lambda *_a, **_k: 'verified',
               '_tm_extract': extract, '_tm_post': lambda tool, stage: stage / 'Proton-A',
               '_tm_read_info': tm_read_info, '_TM_VERSION_FILE': 'protondl_version.json',
               '_tm_actions_resolve': lambda *_: None, 'ngx_human': str,
               'tm_find_installed': lambda *_: None}
        gui_snippets('drstool_gui/tool_manager.py', {'tm_install'}, env)
        tool = types.SimpleNamespace(name='Proton', kind='proton', src='github')
        ver = types.SimpleNamespace(label='new', url='https://invalid.example/archive',
                                    size=0, name='tool.tar.gz', csum='')
        self.old()
        result = env['tm_install'](tool, ver, launcher, True, threading.Event(),
                                   lambda _: None, lambda *_: None)
        self.assertEqual(result['path'], self.target)
        self.assertTrue((self.target / 'new-binary').is_file())
        self.assertFalse((self.target / 'original').exists())
        self.assertFalse(list(self.p.glob('.drstool-tm-install-*.json')))
        self.assertFalse(list(self.p.glob('.drstool-previous-*')))

    def test_f07_crash_after_old_rename_recovers(self):
        self.old(); path = self.journal(); entry = json.loads(path.read_text())['entries'][0]
        os.replace(self.target, self.p / entry['backup'])
        tool_recovery.recover(self.p)
        self.assertTrue((self.target / 'original').is_file())
        self.assertFalse(path.exists())

    def test_f07_crash_after_stage_move_recovers_and_quarantines(self):
        self.old(); path = self.journal(); data = json.loads(path.read_text())
        os.replace(self.target, self.p / data['entries'][0]['backup'])
        os.replace(self.stage, self.target)
        tool_recovery.recover(self.p)
        self.assertTrue((self.target / 'original').is_file())
        self.assertTrue(list(self.p.glob('.drstool-incomplete-*')))
        self.assertFalse(path.exists())

    def test_f07_committed_new_install_kept_and_old_removed(self):
        self.old(); path = self.journal(); data = json.loads(path.read_text())
        bak = self.p / data['entries'][0]['backup']
        os.replace(self.target, bak); os.replace(self.stage, self.target)
        data['state'] = 'committed'; tx.atomic_write_json(path, data)
        tool_recovery.recover(self.p)
        self.assertFalse(path.exists())
        self.assertFalse(bak.exists())
        self.assertTrue((self.target / 'protondl_version.json').exists())

    def test_f07_missing_original_backup_cannot_claim_recovery(self):
        self.old(); path = self.journal(); data = json.loads(path.read_text())
        bak = self.p / data['entries'][0]['backup']
        os.replace(self.target, bak); os.replace(self.stage, self.target)
        shutil.rmtree(bak)  # simulate external deletion, not normal DRSTool cleanup
        with self.assertRaisesRegex(RuntimeError, 'original backup missing'):
            tool_recovery.recover(self.p)
        self.assertTrue(path.exists())
        self.assertTrue(self.target.exists())

    def test_f07_new_install_crash_quarantined(self):
        path = self.journal(); os.replace(self.stage, self.target)
        tool_recovery.recover(self.p)
        self.assertFalse(self.target.exists())
        self.assertFalse(path.exists())
        self.assertTrue(list(self.p.glob('.drstool-incomplete-*')))

    def test_f07_legacy_previous_backup_restored_only_when_missing(self):
        backup = self.p / '.drstool-previous-1234-Proton-A'
        backup.mkdir(); (backup / 'proton').write_text('old')
        tool_recovery.recover(self.p)
        self.assertTrue((self.target / 'proton').exists())
        self.assertFalse(backup.exists())
        backup.mkdir(); (backup / 'proton').write_text('older')
        tool_recovery.recover(self.p)
        self.assertTrue(backup.is_dir())
        self.assertEqual((self.target / 'proton').read_text(), 'old')


class WorkerCancellationRegressions(unittest.TestCase):
    def test_f11_cancelled_scan_reports_processed_not_planned(self):
        class SignalStub:
            def __init__(self, *_): self.values = [] ; self.callback = None
            def emit(self, *args):
                self.values.append(args)
                if self.callback: self.callback(*args)
        class QThreadStub:
            def __init__(self, parent=None): pass
        env = {'QThread': QThreadStub, 'Signal': SignalStub, 'threading': threading,
               'game_scan': types.SimpleNamespace(find_dlls=lambda *_args, **_kw: types.SimpleNamespace(),
                                                   GameDlls=lambda: types.SimpleNamespace()),
               'Dict': dict, 'List': list, 'Optional': object,
               'PD_COMPONENTS': [], 'read_prefix_manifest': lambda _: {},
               'detect_installed': lambda *_args, **_kw: {},
               'DllGameState': lambda *args: types.SimpleNamespace(game=args[0], dlls=args[1], prefix=args[2])}
        gui_snippets('drstool_gui/dll_manager.py', {'_DllScanWorker'}, env)
        games = [types.SimpleNamespace(name=str(i), prefix=None, prefix_manager='') for i in range(3)]
        w = env['_DllScanWorker']([], games)
        w.game_ready.callback = lambda *_: w.cancel()
        w.run()
        self.assertEqual(w.done.values[-1][:3], (1, 3, True))

    def test_f10_cancel_terminates_child_process_group_and_worker(self):
        class SignalStub:
            def __init__(self, *_): self.values=[]
            def emit(self, *args): self.values.append(args)
        class QThreadStub:
            def __init__(self, parent=None): pass
        env = {'QThread': QThreadStub, 'Signal': SignalStub, 'os': os,
               'subprocess': __import__('subprocess'), 'threading': threading,
               'signal': signal, 'Path': Path}
        gui_snippets('drstool_gui/winetricks_dialog.py', {'_TricksWorker'}, env)
        w = env['_TricksWorker'](['/bin/sh', '-c', 'sleep 20 & wait'], {})
        thread = threading.Thread(target=w.run)
        thread.start()
        try:
            deadline = time.monotonic() + 3
            while w._proc is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsNotNone(w._proc)
            w.cancel()
            thread.join(5)
            self.assertFalse(thread.is_alive(), 'worker blocked by surviving child process')
            self.assertTrue(w.done.values)
        finally:
            w.cancel()
            thread.join(5)


if __name__ == '__main__':
    unittest.main()
