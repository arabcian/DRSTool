"""Second-pass regressions; no host tuning, network, Qt or real GPU required."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch
import zipfile

import pytest
from drstool_core import game_links, launch_preflight, pe_version, performance_history, session_recorder
from drstool_core import steam_shortcuts as ss, support_bundle, winetricks, xid_analysis, yaml_merge

SUPERVISOR = Path(__file__).resolve().parents[1] / 'lutris-game-tune-main/lgtune-session.py'
spec = importlib.util.spec_from_file_location('lgtune_session', SUPERVISOR)
supervisor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(supervisor)


def fake_wrapper(tmp_path, monkeypatch):
    log = tmp_path / 'events'
    wrapper = tmp_path / 'wrapper'
    wrapper.write_text(f'#!{sys.executable}\n' + '''import os, sys
args = sys.argv[1:]
with open(os.environ['TEST_EVENTS'], 'a') as stream:
    stream.write(' '.join(args[:3]) + '\\n')
if args[0] == 'PRE': sys.exit(int(os.getenv('TEST_PRE_RC', '0')))
if args[0] == 'POST': sys.exit(int(os.getenv('TEST_POST_RC', '0')))
os.execvp(args[3], args[3:])
''')
    wrapper.chmod(0o700)
    monkeypatch.setenv('TEST_EVENTS', str(log))
    return wrapper, log


def test_supervisor_concurrent_launch_tokens(tmp_path, monkeypatch):
    wrapper, log = fake_wrapper(tmp_path, monkeypatch)
    code = 'import runpy,sys; m=runpy.run_path(sys.argv[1]); sys.exit(m["run"]([sys.executable,"-c","import time;time.sleep(.3)"],-10,wrapper=sys.argv[2]))'
    procs = [subprocess.Popen([sys.executable, '-c', code, str(SUPERVISOR), str(wrapper)]) for _ in range(2)]
    try:
        assert [p.wait(timeout=5) for p in procs] == [0, 0]
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill(); p.wait()
    lines = log.read_text().splitlines()
    pres = [x.split()[-1] for x in lines if x.startswith('PRE ')]
    posts = [x.split()[-1] for x in lines if x.startswith('POST ')]
    assert len(set(pres)) == 2 and sorted(posts) == sorted(pres)


def test_supervisor_failed_pre_never_runs_game(tmp_path, monkeypatch):
    wrapper, log = fake_wrapper(tmp_path, monkeypatch)
    monkeypatch.setenv('TEST_PRE_RC', '3')
    assert supervisor.run(['/bin/true'], -10, wrapper=str(wrapper)) == 3
    assert len(log.read_text().splitlines()) == 1


def test_supervisor_reports_post_failure(tmp_path, monkeypatch):
    wrapper, log = fake_wrapper(tmp_path, monkeypatch)
    monkeypatch.setenv('TEST_POST_RC', '2')
    assert supervisor.run(['/bin/true'], -10, wrapper=str(wrapper)) == 1
    assert 'POST ' in log.read_text()


def test_supervisor_waits_for_descendant_group(tmp_path, monkeypatch):
    wrapper, log = fake_wrapper(tmp_path, monkeypatch)
    start = time.monotonic()
    assert supervisor.run(['/bin/sh', '-c', 'sleep .4 &'], -10, wrapper=str(wrapper)) == 0
    assert time.monotonic() - start >= .35


def test_lutris_migrates_only_known_hooks():
    token = 'lutris-' + 'a' * 32
    text = f'system:\n  prelaunch_command: pkexec /usr/local/bin/lutris-game-tune-wrapper PRE --global {token}\n  postexit_command: pkexec /usr/local/bin/lutris-game-tune-wrapper POST {token}\n'
    keys = {'prefix_command': '/usr/local/lib/lutris-game-tune/lgtune-session.py --polkit --nice -10 --'}
    out, _, _, removed, _ = yaml_merge.merge_lutris_yaml(text, keys, {})
    assert removed == {'prelaunch_command', 'postexit_command'}
    assert 'lgtune-session.py' in out and 'prelaunch_command' not in out
    with pytest.raises(ValueError):
        yaml_merge.merge_lutris_yaml(text.replace('PRE --global', 'PRE custom'), keys, {})


def test_yaml_rejects_non_mapping():
    with pytest.raises(ValueError):
        yaml_merge.merge_lutris_yaml('system: [a, b]\n', {'x': 1}, {})
    with pytest.raises(ValueError):
        yaml_merge.merge_lutris_yaml('system:\n  env: custom\n', {}, {'X': '1'})


def test_steam_id_preserved_and_collision_rejected():
    root = ss.KV([('shortcuts', ss.KV())])
    aid, _ = ss.upsert_shortcut(root, 'A', '/game/a')
    entry = root.get('shortcuts').get('0')
    entry.set('appid', ss.T_INT, ss.to_signed32(0x80000123))
    assert ss.upsert_shortcut(root, 'A', '/game/a')[0] == 0x80000123
    with patch.object(ss, 'shortcut_appid', return_value=0x80000123):
        with pytest.raises(ss.VdfError):
            ss.upsert_shortcut(root, 'B', '/game/b')
    assert len(ss.list_shortcuts(root)) == 1


def test_wine_prefix_requires_steam_layout(tmp_path):
    assert winetricks.steam_appid(tmp_path / 'custom/42/pfx') is None
    assert winetricks.steam_appid(tmp_path / 'steamapps/compatdata/42/pfx') == '42'
    assert winetricks.steam_appid(tmp_path / 'steamapps/compatdata/0/pfx') is None
    with pytest.raises(ValueError):
        winetricks.prefix_command(tmp_path / 'bad', 'kill')


def test_link_unassign_preserves_performance(tmp_path):
    store = game_links.LinkStore(tmp_path / 'links.json')
    store.assign('steam:42', 'A')
    store.add_perf('steam:42', {'path': 'capture', 'mtime': 1, 'avg': 55})
    store.assign('steam:42', None)
    assert len(store.get('steam:42')['perf']) == 1
    with pytest.raises(ValueError):
        store.recover_backup(only_if_corrupt=True)


def test_session_id_traversal_and_old_boot(tmp_path):
    store = session_recorder.SessionStore(tmp_path / 'sessions')
    e = store.begin(profile='p', profile_fp='x', command=['true'], env={})
    with pytest.raises(ValueError):
        store.write(dict(e, id='../' + e['id']))
    e['boot_id'] = 'old-boot'
    store.write(e)
    assert store.read(e['id'])['status'] == 'interrupted'
    assert store.recent(limit=0) == []


def test_preflight_steam_prefix_and_colon_preload(tmp_path):
    prefix = tmp_path / 'pfx'; prefix.mkdir()
    existing = tmp_path / 'lib.so'; existing.touch()
    r = launch_preflight.evaluate({'env': {'STEAM_COMPAT_DATA_PATH': str(tmp_path), 'LD_PRELOAD': f'{existing}:/no-such-lib.so'}}, env_base={})
    assert not any(x['code'] == 'prefix-unknown' for x in r['findings'])
    assert any(x['code'] == 'preload-missing' and '1 missing' in x['message'] for x in r['findings'])


def test_performance_chooses_matching_latest_baseline():
    def rows(fp, scene, when):
        return [dict(profile='p', fp=fp, kind='flm', scene=scene, driver='1', proton='1', dxvk='1', vkd3d='1', avg=100, dur=60, mtime=when+i) for i in range(3)]
    history = rows('a', 'same', 1) + rows('b', 'different', 10) + rows('c', 'same', 20)
    r = performance_history.trend_report(history)['comparison']
    assert r['from_fp'] == 'a' and r['to_fp'] == 'c' and r['comparable']


def test_higher_order_bayesian_hypotheses_and_invalid_duration():
    model = xid_analysis.CrashModel(list('abcde'), triple_limit=5, higher_limit=5)
    assert ('and', 'a', 'b', 'c', 'd', 'e') in model.hyps
    assert sum(p for _, p in model.posterior()) == pytest.approx(1)
    with pytest.raises(ValueError):
        model.add(['a'], float('nan'), True)


def test_non_pe_version_signature_rejected(tmp_path):
    p = tmp_path / 'false.dll'
    p.write_bytes(b'MZ' + b'\0'*100 + b'\xbd\x04\xef\xfe\x00\x00\x01\x00' + b'\0'*100)
    assert pe_version.read_version(p) is None


def test_support_bundle_excludes_symlink_and_quoted_secret(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    d = tmp_path / 'drstool'; d.mkdir()
    private = tmp_path / 'private'; private.write_text('PRIVATE CONTENT')
    (d / 'debug.log.1').symlink_to(private)
    (d / 'debug.log').write_text('PASSWORD="first second third"\n')
    out = support_bundle.build(tmp_path / 'out.zip', {})
    with zipfile.ZipFile(out) as z:
        assert 'log/debug.log.1' not in z.namelist()
        assert 'second third' not in z.read('log/debug.log').decode()
    assert out.stat().st_mode & 0o077 == 0
