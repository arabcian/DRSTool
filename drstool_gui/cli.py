"""Command-line mode (--list-profiles, --print-env, --lint, --apply, ...)."""

import difflib
import json
import os
import shlex
import sys
from pathlib import Path

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from . import base as _base
from .base import _CORE_ERR, APP_TITLE, share_code
from .launch_line import build_launch_line
from .lint import lint_config
from .lookup import canonical_drs_key, canonical_drs_value, drs_lookup
from .main_window import MainWindow
from .nip_import import decode_nip_bytes, NipImportWidget, parse_nip
from .profile_io import lint_profile, profile_share_code
from .settings_manager import SettingsManager
from .state import _flat_profile
from .sysinfo import SYSHUB


def _cli_parse(argv):
    import argparse
    ap = argparse.ArgumentParser(
        prog="drstool", allow_abbrev=False,
        description="DRSTool. Without options the GUI starts; the options below run headless and exit.",
        epilog="Exit status: 0 ok · 1 lint errors (or warnings with --strict) or a failed write · "
               "2 bad arguments / not found.")
    g = ap.add_argument_group("headless commands")
    g.add_argument("--list-profiles", action="store_true", help="list the saved profiles")
    g.add_argument("--print-env", metavar="PROFILE", help="print a profile's launch line / environment")
    g.add_argument("--format", choices=("steam", "shell", "export", "json"), default="steam",
                   help="--print-env format: steam = env … -- %%command%% (default), "
                        "shell = env … \"$@\", export = export K=V lines, json = the full payload")
    g.add_argument("--lint", metavar="PROFILE", help="run the consistency linter on a profile")
    g.add_argument("--strict", action="store_true", help="--lint: warnings also give exit status 1")
    g.add_argument("--apply", metavar="PROFILE", help="write a profile into a Lutris game config (needs --lutris)")
    g.add_argument("--lutris", metavar="GAME", help="--apply: game slug or name")
    g.add_argument("--lutris-dir", metavar="DIR", help="--apply: Lutris games folder (default $XDG_DATA_HOME/lutris/games, "
                        "i.e. ~/.local/share/lutris/games)")
    g.add_argument("--dry-run", action="store_true", help="--apply: show the diff, write nothing")
    g.add_argument("-y", "--yes", action="store_true", help="--apply: write without asking")
    g.add_argument("--import-nip", metavar="FILE", help="convert an NVIDIA Profile Inspector .nip into "
                                                         "DXVK_NVAPI_DRS_SETTINGS pairs")
    g.add_argument("--nip-profile", metavar="NAME", help="--import-nip: profile inside the file (default: first)")
    g.add_argument("--into", metavar="PROFILE", help="--import-nip: merge the settings into this saved profile")
    g.add_argument("--save-as", metavar="NAME", help="--import-nip / --import-share: store as a new profile")
    g.add_argument("--share", metavar="PROFILE", help="print a profile's share code (DRSTOOL1:…)")
    g.add_argument("--import-share", metavar="CODE", help="import a share code (\"-\" reads it from stdin); "
                                                          "prints the content and the lint result, saves with --save-as")
    g.add_argument("--include-flagged", action="store_true",
                   help="--import-share: also keep variables flagged as sensitive (LD_PRELOAD, layer paths, …)")
    ap.add_argument("--profile", metavar="PROFILE", help="GUI: start with this profile loaded")
    return ap.parse_known_args(argv)


def _cli_wants(a) -> bool:
    return bool(a.list_profiles or a.print_env or a.lint or a.apply or a.import_nip
                or a.share or a.import_share)


def _cli_dispatch(a) -> int:
    def err(msg):
        print(f"drstool: {msg}", file=sys.stderr)

    def dlg(kind):
        def f(*args, **_kw):
            err(f"{args[1] if len(args) > 1 else kind}: {args[2] if len(args) > 2 else ''}")
            return QMessageBox.No if kind == "question" else QMessageBox.Ok
        return staticmethod(f)
    for k in ("question", "critical", "warning", "information"):
        setattr(QMessageBox, k, dlg(k))          # a headless run must never block on a dialog

    sm = SettingsManager()
    profiles = sm.get_profiles()

    def resolve(name):
        if name in profiles:
            return name
        low = [n for n in profiles if n.casefold() == name.casefold()]
        sub = low or [n for n in profiles if name.casefold() in n.casefold()]
        if len(sub) == 1:
            return sub[0]
        err(f'"{name}" is ambiguous: ' + ", ".join(sub) if sub else f'no such profile "{name}" (see --list-profiles)')
        return None

    win_box: list = []

    def window(name):
        if not win_box:
            win_box.append(MainWindow())
        w = win_box[0]
        w.settings_manager.load_profile(name)
        if w.settings_manager.get_current_profile() != name:
            return None
        w._on_profile_loaded(name)
        return w

    if a.list_profiles:
        for n, p in profiles.items():
            f = _flat_profile(p)
            print(f"{n}\tDRS={sum(1 for k in f if k[0] == 'DRS')}\tENV={sum(1 for k in f if k[0] == 'Env')}"
                  f"\tARCH={f.get(('Arch', 'GPU arch'), '-')}"
                  f"\tGAMESCOPE={'on' if ('Gamescope', 'enabled') in f else 'off'}")
        return 0

    if a.print_env:
        name = resolve(a.print_env)
        w = name and window(name)
        if not w:
            return 2
        p = w.collect_launch_payload()
        if a.format == "json":
            print(json.dumps(p, indent=2, ensure_ascii=False))
        elif a.format == "export":
            for k, v in p["env"].items():
                print(f"export {k}={shlex.quote(str(v))}")
            if p["gs_cmd"]:
                print(f"# gamescope: {p['gs_cmd']}")
        else:
            print(build_launch_line(p, "%command%" if a.format == "steam" else '"$@"', True))
        return 0

    if a.lint:
        name = resolve(a.lint)
        w = name and window(name)
        if not w:
            return 2
        loop = QEventLoop()                       # let the system probe finish so the lint sees this machine
        SYSHUB.info_changed.connect(lambda *_: loop.quit())
        QTimer.singleShot(8000, loop.quit)
        SYSHUB.refresh()
        loop.exec()
        p = w.collect_launch_payload()
        issues = lint_config(p["env"], p["drs"], p["arch"], p["gs_on"], p["gs_flags"], p["mangohud"], SYSHUB.info)
        for i in issues:
            print(f"{i.sev.upper():5} {i.code}: {i.msg}")
        n_err = sum(1 for i in issues if i.sev == "error")
        n_warn = sum(1 for i in issues if i.sev == "warn")
        err(f"{n_err} error(s), {n_warn} warning(s)")
        return 1 if (n_err or (a.strict and n_warn)) else 0

    if a.apply:
        if not a.lutris:
            err("--apply needs --lutris GAME")
            return 2
        name = resolve(a.apply)
        w = name and window(name)
        if not w:
            return 2
        ls = w._lutris_sync_widget
        if a.lutris_dir:
            ls._games_dir = Path(a.lutris_dir).expanduser()
            ls._scan_games()
        if not ls._games:
            err(f"no Lutris game configs found in {ls._games_dir}")
            return 2
        want = a.lutris.casefold()
        hit = [i for i, g in enumerate(ls._games) if want in (g.slug.casefold(), g.game_name.casefold())]
        hit = hit or [i for i, g in enumerate(ls._games) if want in g.slug.casefold() or want in g.game_name.casefold()]
        if len(hit) != 1:
            err(f'"{a.lutris}" ' + ("matches several games: " + ", ".join(ls._games[i].slug for i in hit)
                                    if hit else "matches no game"))
            return 2
        ls._game_combo.blockSignals(True)
        ls._game_combo.setCurrentIndex(hit[0])
        ls._game_combo.blockSignals(False)
        ls._on_game_selected(hit[0])
        entry = ls._games[hit[0]]
        system_keys, env_vars, warnings = ls._split_gamescope(ls._collect_all_env())
        original = entry.path.read_text(encoding="utf-8")
        new_text = ls._merge_yaml(original, system_keys, env_vars)[0]
        for wmsg in warnings:
            err(f"warning: {wmsg}")
        if new_text == original:
            print(f"{entry.path.name}: already up to date")
            return 0
        diff = "".join(difflib.unified_diff(original.splitlines(True), new_text.splitlines(True),
                                            str(entry.path), str(entry.path) + " (new)"))
        if a.dry_run:
            sys.stdout.write(diff)
            return 0
        if not a.yes:
            if not sys.stdin.isatty():
                err("refusing to write without --yes (use --dry-run to preview)")
                return 2
            sys.stdout.write(diff)
            if input("Write these changes? [y/N] ").strip().lower() not in ("y", "yes"):
                return 1
        ok = ls._apply_to_game(confirm=False)
        print(ls._status_label.text())
        return 0 if ok else 1

    if a.share:
        if _CORE_ERR:
            err(_CORE_ERR)
            return 2
        name = resolve(a.share)
        if not name:
            return 2
        try:
            print(profile_share_code(name, profiles[name]))
        except share_code.ShareCodeError as ex:
            err(f'cannot share "{name}": {ex}')
            return 1
        return 0

    if a.import_share:
        if _CORE_ERR:
            err(_CORE_ERR)
            return 2
        text = sys.stdin.read() if a.import_share == "-" else a.import_share
        try:
            sh = share_code.decode(text, home=str(Path.home()))
        except share_code.ShareCodeError as ex:
            err(str(ex))
            return 2
        for line in share_code.summary_lines(sh):
            print(line)
        for k, why in sh.flagged.items():
            err(f"flagged: {k} — {why}" + ("" if a.include_flagged else " (left out; --include-flagged keeps it)"))
        for d in sh.dropped:
            err(f"skipped: {d}")
        prof = sh.profile if a.include_flagged else sh.profile_without_flagged()
        loop = QEventLoop()                       # probe this machine so the lint result is meaningful
        SYSHUB.info_changed.connect(lambda *_: loop.quit())
        QTimer.singleShot(8000, loop.quit)
        SYSHUB.refresh()
        loop.exec()
        issues = lint_profile(prof, SYSHUB.info)
        for i in issues:
            print(f"{i.sev.upper():5} {i.code}: {i.msg}")
        if not a.save_as:
            err("not saved (use --save-as NAME)")
            return 0
        if a.save_as in profiles:
            err(f'profile "{a.save_as}" already exists')
            return 2
        if not sm.import_profiles({a.save_as: prof}):
            return 1
        err(f'saved profile "{a.save_as}"')
        return 0

    if a.import_nip:
        try:
            profs = parse_nip(decode_nip_bytes(Path(a.import_nip).expanduser().read_bytes()))
        except Exception as ex:
            err(f"cannot read {a.import_nip}: {ex}")
            return 2
        if not profs:
            err("no profiles in that file")
            return 2
        sel = next((p for p in profs if a.nip_profile and p["name"].casefold() == a.nip_profile.casefold()),
                   None if a.nip_profile else profs[0])
        if sel is None:
            err(f'no profile "{a.nip_profile}" in the file; it has: ' + ", ".join(p["name"] for p in profs))
            return 2
        pairs, skipped = [], 0
        for s in sel["settings"]:
            if s["value"] is None:
                skipped += 1
                continue
            key = f'0x{s["id"]:08X}'
            known = drs_lookup(key)
            pairs.append((known.id if known else key, NipImportWidget._fmt_value(s["value"])))
        print("DXVK_NVAPI_DRS_SETTINGS=" + ",".join(f"{k}={v}" for k, v in pairs))
        if skipped:
            err(f"{skipped} non-DWORD setting(s) skipped")
        if not (a.into or a.save_as):
            return 0
        saved: list = []
        sm.profile_saved.connect(saved.append)
        sm.profile_save_error.connect(err)
        if a.into:
            name = resolve(a.into)
            if not name:
                return 2
            sm.load_profile(name)
            cur = sm.get_settings_list()
            arch = sm.get_arch()
            env_vars = dict(sm.get_loaded_env_vars())
            gs_on, gs_flags = sm.get_loaded_gamescope_enabled(), dict(sm.get_loaded_gamescope_flags())
        else:
            name, cur, arch, env_vars, gs_on, gs_flags = a.save_as, {}, None, {}, False, {}
            if name in profiles:
                err(f'profile "{name}" already exists — use --into to merge')
                return 2
        for k, v in pairs:
            cur[canonical_drs_key(k)] = canonical_drs_value(drs_lookup(k), v)
        sm.replace_state(cur, arch.code if arch else None)
        sm.save_profile(name, env_vars, gs_on, gs_flags)
        if not saved:
            return 1
        err(f'saved profile "{name}" ({len(pairs)} setting(s) merged)')
        return 0
    return 0


def cli_run(a) -> int:
    _base._CLI_MODE = True
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication(sys.argv[:1])              # noqa: F841 - must stay alive
    app.setApplicationName(APP_TITLE)
    try:
        rc = _cli_dispatch(a)
    except Exception as ex:
        print(f"drstool: {type(ex).__name__}: {ex}", file=sys.stderr)
        rc = 1
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)                                   # skip Qt teardown / probe threads
