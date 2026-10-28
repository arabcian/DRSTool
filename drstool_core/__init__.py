"""Qt-free building blocks of DRSTool.

Everything in this package is plain Python (stdlib only) so it can be unit-tested
without a display and reused by the headless CLI. The GUI (drstool_gui) imports from
here; new non-GUI logic should land here rather than in the GUI package.

Modules:
    models          dataclasses of the catalog (Setting, EnvVarDef, GamescopeFlagDef, ...)
    catalog         loads the catalog from the JSON files in data/ (DRS settings, env vars,
                    gamescope flags, GPU architectures, dxvk.conf keys, VKD3D_CONFIG flags)
    share_code      profile share codes ("DRSTOOL1:…") for copy/paste sharing
    steam_shortcuts binary VDF + Steam non-Steam-game shortcuts (shortcuts.vdf)
    pe_version      FileVersion of Windows DLLs (VS_FIXEDFILEINFO)
    game_scan       find installed games (Steam, Lutris, Heroic, Bottles) and
                    the DLSS / Streamline DLLs inside them
    dll_ops         replace DLLs atomically with a restorable backup
"""

__all__ = ["models", "catalog", "share_code", "steam_shortcuts", "pe_version", "game_scan", "dll_ops"]
