"""Qt-free building blocks of DRSTool.

Everything in this package is plain Python (stdlib only) so it can be unit-tested
without a display and reused by the headless CLI. DRSTool.py imports from here;
new non-GUI logic should land here rather than in the big GUI module.

Modules:
    share_code      profile share codes ("DRSTOOL1:…") for copy/paste sharing
    steam_shortcuts binary VDF + Steam non-Steam-game shortcuts (shortcuts.vdf)
    pe_version      FileVersion of Windows DLLs (VS_FIXEDFILEINFO)
    game_scan       find installed games (Steam, Lutris, Heroic, Bottles) and
                    the DLSS / Streamline DLLs inside them
    dll_ops         replace DLLs atomically with a restorable backup
"""

__all__ = ["share_code", "steam_shortcuts", "pe_version", "game_scan", "dll_ops"]
