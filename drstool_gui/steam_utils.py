"""Steam files: VDF text editing, library / compat-tool discovery, umu stores."""

import re
from functools import lru_cache
from pathlib import Path
from typing import List


# ── minimal VDF (Valve KeyValues text) editor ───────────────────────────────

_VDF_TOK = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])', re.S)


def _vdf_unesc(s: str) -> str:
    if "\\" not in s:
        return s
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), s)


def _vdf_esc(s: str) -> str:
    # Newlines/tabs too: _vdf_unesc() decodes \n and \t, so a raw newline in a
    # value broke the read-back self-check (and the file's line structure).
    return (s.replace("\\", "\\\\").replace('"', '\\"')
             .replace("\n", "\\n").replace("\t", "\\t"))


@lru_cache(maxsize=8)       # keyed by the file content: re-reads of an unchanged file skip the parse
def _vdf_parse(text: str):
    toks = []
    for m in _VDF_TOK.finditer(text):
        if m.group(2) == "{":
            toks.append(("open", "", m.start(), m.end()))
        elif m.group(2) == "}":
            toks.append(("close", "", m.start(), m.end()))
        else:
            toks.append(("str", _vdf_unesc(m.group(1)), m.start(), m.end()))

    def block(i):
        items = []
        while i < len(toks):
            k = toks[i]
            if k[0] == "close":
                return items, i
            if k[0] == "str" and i + 1 < len(toks):
                n = toks[i + 1]
                if n[0] == "str":
                    items.append((k, "val", n))
                    i += 2
                    continue
                if n[0] == "open":
                    sub, j = block(i + 2)
                    if j >= len(toks):      # EOF before the matching "}" — never hand out the open brace as a close position
                        raise ValueError(f'unbalanced braces in VDF: block "{k[1]}" is never closed')
                    items.append((k, "block", (n, sub, toks[j])))
                    i = j + 1
                    continue
            i += 1
        return items, i

    items, _ = block(0)
    return items


def _vdf_find(items, key: str, kind: str):
    kl = key.lower()
    for k, kd, payload in items:
        if kd == kind and k[1].lower() == kl:
            return payload
    return None


def _vdf_apps_block(text: str):
    items = _vdf_parse(text)
    node = items
    for part in ("UserLocalConfigStore", "Software", "Valve", "Steam", "apps"):
        b = _vdf_find(node, part, "block")
        if b is None:
            raise ValueError(f'"{part}" section not found in localconfig.vdf '
                             "(start Steam once with this account first)")
        node = b[1]
        last = b
    return last            # (open_tok, items, close_tok) of "apps"


def vdf_get_launch_options(text: str, appid: str) -> str:
    try:
        apps = _vdf_apps_block(text)
    except ValueError:
        return ""
    app = _vdf_find(apps[1], str(appid), "block")
    if app is None:
        return ""
    v = _vdf_find(app[1], "LaunchOptions", "val")
    return v[1] if v else ""


def vdf_set_launch_options(text: str, appid: str, value: str) -> str:
    apps = _vdf_apps_block(text)
    appid = str(appid)
    app = _vdf_find(apps[1], appid, "block")
    esc = _vdf_esc(value)

    def child_insert(close_tok, body: str) -> str:
        pos = close_tok[2]
        ls = text.rfind("\n", 0, pos) + 1
        indent = text[ls:pos]
        if indent.strip() == "":                       # brace alone on its line
            return text[:ls] + body + text[ls:]
        return text[:pos] + "\n" + body + text[pos:]

    def indent_of(close_tok) -> str:
        pos = close_tok[2]
        ls = text.rfind("\n", 0, pos) + 1
        ind = text[ls:pos]
        return ind if ind.strip() == "" else ""

    if app is not None:
        v = _vdf_find(app[1], "LaunchOptions", "val")
        if v is not None:
            tok = v
            return text[:tok[2]] + f'"{esc}"' + text[tok[3]:]
        ind = indent_of(app[2]) + "\t"
        return child_insert(app[2], f'{ind}"LaunchOptions"\t\t"{esc}"\n')
    ind = indent_of(apps[2]) + "\t"
    body = (f'{ind}"{appid}"\n{ind}{{\n{ind}\t"LaunchOptions"\t\t"{esc}"\n{ind}}}\n')
    return child_insert(apps[2], body)


# ----------------------------------------------------------------------------
# VDF: generic "set a value inside <path>/<appid>" (Steam config.vdf)
# ----------------------------------------------------------------------------

def _vdf_indent_at(text: str, close_tok) -> str:
    pos = close_tok[2]
    ls = text.rfind("\n", 0, pos) + 1
    ind = text[ls:pos]
    return ind if ind.strip() == "" else ""


def _vdf_insert_before_close(text: str, close_tok, body: str) -> str:
    pos = close_tok[2]
    ls = text.rfind("\n", 0, pos) + 1
    if text[ls:pos].strip() == "":
        return text[:ls] + body + text[ls:]
    return text[:pos] + "\n" + body + text[pos:]


def _vdf_descend(text: str, path: tuple):
    """-> (deepest existing block or None, #path parts that exist)."""
    node, blk, depth = _vdf_parse(text), None, 0
    for part in path:
        b = _vdf_find(node, part, "block")
        if b is None:
            break
        blk, node, depth = b, b[1], depth + 1
    return blk, depth


def vdf_get_app_value(text: str, path: tuple, appid: str, key: str) -> str:
    blk, depth = _vdf_descend(text, path)
    if blk is None or depth < len(path):
        return ""
    app = _vdf_find(blk[1], str(appid), "block")
    if app is None:
        return ""
    v = _vdf_find(app[1], key, "val")
    return v[1] if v else ""


def vdf_set_app_value(text: str, path: tuple, appid: str, key: str, value: str) -> str:
    appid, esc = str(appid), _vdf_esc(value)
    blk, depth = _vdf_descend(text, path)
    if blk is None:
        raise ValueError(f'"{path[0]}" section not found in the VDF file')
    if depth < len(path):                      # container missing -> create the chain
        cur = _vdf_indent_at(text, blk[2]) + "\t"
        lines = []
        for n in path[depth:]:
            lines.append(f'{cur}"{n}"\n{cur}{{\n')
            cur += "\t"
        lines.append(f'{cur}"{appid}"\n{cur}{{\n{cur}\t"{key}"\t\t"{esc}"\n{cur}}}\n')
        for _ in path[depth:]:
            cur = cur[:-1]
            lines.append(f"{cur}}}\n")
        return _vdf_insert_before_close(text, blk[2], "".join(lines))
    app = _vdf_find(blk[1], appid, "block")
    if app is None:
        ind = _vdf_indent_at(text, blk[2]) + "\t"
        return _vdf_insert_before_close(
            text, blk[2], f'{ind}"{appid}"\n{ind}{{\n{ind}\t"{key}"\t\t"{esc}"\n{ind}}}\n')
    v = _vdf_find(app[1], key, "val")
    if v is not None:
        return text[:v[2]] + f'"{esc}"' + text[v[3]:]
    ind = _vdf_indent_at(text, app[2]) + "\t"
    return _vdf_insert_before_close(text, app[2], f'{ind}"{key}"\t\t"{esc}"\n')


def vdf_remove_app_block(text: str, path: tuple, appid: str) -> str:
    blk, depth = _vdf_descend(text, path)
    if blk is None or depth < len(path):
        return text
    app = _vdf_find(blk[1], str(appid), "block")
    if app is None:
        return text
    # app = (open_tok, items, close_tok); the key token sits right before open_tok
    open_tok, close_tok = app[0], app[2]
    items = blk[1]
    key_tok = next((k for k, kd, pl in items if kd == "block" and pl is app), None)
    if key_tok is None:
        return text
    start = text.rfind("\n", 0, key_tok[2]) + 1
    if text[start:key_tok[2]].strip():
        start = key_tok[2]
    end = close_tok[3]
    if text[end:end + 1] == "\n":
        end += 1
    return text[:start] + text[end:]


STEAM_COMPAT_PATH = ("InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping")


def _official_proton_name(dirname: str) -> str:
    low = dirname.lower()
    if "experimental" in low:
        return "proton_experimental"
    if "hotfix" in low:
        return "proton_hotfix"
    m = re.match(r"proton\s+(\d+)\.(\d+)", low)
    if m:
        major, minor = m.groups()
        return f"proton_{major}" if minor == "0" else f"proton_{major}{minor}"
    return ""


def steam_compat_tools(root: Path) -> List[tuple]:
    """[(internal_name, label)] of Proton builds Steam can use (custom + official)."""
    from .launchers import SteamBackend
    out, seen = [], set()
    cdirs = [root / "compatibilitytools.d", Path.home() / ".steam/root/compatibilitytools.d",
             Path("/usr/share/steam/compatibilitytools.d"),
             Path("/usr/local/share/steam/compatibilitytools.d")]
    for cd in cdirs:
        try:
            subs = sorted(p for p in cd.iterdir() if p.is_dir())
        except OSError:
            continue
        for d in subs:
            internal, disp = d.name, ""
            try:
                t = (d / "compatibilitytool.vdf").read_text(errors="replace")
                m = re.search(r'"compat_tools"\s*\{\s*"([^"]+)"', t)
                if m:
                    internal = m.group(1)
                m = re.search(r'"display_name"\s+"([^"]+)"', t)
                if m:
                    disp = m.group(1)
            except OSError:
                pass
            if internal not in seen:
                seen.add(internal)
                out.append((internal, f"{disp or d.name}  ({internal})" if (disp or d.name) != internal
                            else internal))
    for lib in SteamBackend._libraries(root):
        try:
            subs = sorted(p for p in (lib / "common").iterdir()
                          if p.is_dir() and p.name.lower().startswith("proton") and (p / "proton").exists())
        except OSError:
            continue
        for d in subs:
            nm = _official_proton_name(d.name)
            if nm and nm not in seen:
                seen.add(nm)
                out.append((nm, f"{d.name}  ({nm})"))
    return out


def steam_config_vdf(localconfig: Path) -> Path:
    return localconfig.parents[3] / "config" / "config.vdf"


def steam_install_dirs() -> List[tuple]:
    """[(label, installdir Path)] of installed Steam games."""
    from .launchers import SteamBackend
    res, seen = [], set()
    for root in SteamBackend.roots():
        for lib in SteamBackend._libraries(root):
            for acf in sorted(lib.glob("appmanifest_*.acf")):
                try:
                    t = acf.read_text(errors="replace")
                except OSError:
                    continue
                a = re.search(r'"appid"\s+"(\d+)"', t)
                n = re.search(r'"name"\s+"((?:[^"\\]|\\.)*)"', t)
                d = re.search(r'"installdir"\s+"((?:[^"\\]|\\.)*)"', t)
                if not (a and d) or a.group(1) in seen:
                    continue
                p = lib / "common" / _vdf_unesc(d.group(1))
                name = _vdf_unesc(n.group(1)) if n else a.group(1)
                if p.is_dir() and not re.match(r"(Proton\b|Steam Linux Runtime|Steamworks Common)", name):
                    seen.add(a.group(1))
                    res.append((f"{name}  [{a.group(1)}]", p, a.group(1)))
    res.sort(key=lambda x: x[0].lower())
    return res


# ----------------------------------------------------------------------------
# umu-launcher helpers
# ----------------------------------------------------------------------------

UMU_STORES = ["", "none", "steam", "gog", "egs", "amazon", "battlenet", "ea", "humble",
              "itchio", "ubisoft", "zoomplatform"]


def proton_tool_dirs() -> List[tuple]:
    """[(label, absolute dir)] of installed Proton builds, for umu's PROTONPATH."""
    out, seen = [], set()
    roots = [Path.home() / ".local/share/Steam", Path.home() / ".steam/root"]
    cds = [r / "compatibilitytools.d" for r in roots]
    cds += [Path.home() / ".local/share/umu/compatibilitytools",
            Path("/usr/share/steam/compatibilitytools.d")]
    for cd in cds:
        try:
            for d in sorted(cd.iterdir()):
                rp = d.resolve()
                if d.is_dir() and (d / "proton").exists() and rp not in seen:
                    seen.add(rp)
                    out.append((d.name, str(d)))
        except OSError:
            continue
    return out
