"""First start: what was found on this machine and where to begin."""

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from drstool_core import game_scan

from .base import APP_ID
from .launchers import BottlesBackend, HeroicBackend, SteamBackend
from .recipes import all_recipes, recipe_requirement


def maybe_show_onboarding(win, info) -> None:
    """Shown once, after the first finished system probe."""
    qs = QSettings(APP_ID, APP_ID)
    if not info.probed or qs.value("onboarding/done", False, bool):
        return
    qs.setValue("onboarding/done", True)
    OnboardingDialog(win, info).show()


class OnboardingDialog(QDialog):
    def __init__(self, win, info):
        super().__init__(win)
        self.setWindowTitle("Welcome to DRSTool")
        gpu = (info.nv_gpus[0] if info.nv_gpus else
               next((g.name for g in info.vk_gpus if g.is_nvidia), "")) or "no NVIDIA GPU found"
        found = [b.name for b in (SteamBackend(), HeroicBackend(), BottlesBackend()) if not b.unavailable()]
        if any(d.is_dir() for d in game_scan.lutris_dirs()):
            found.append("Lutris")
        fits = [r.title for r in all_recipes() if not recipe_requirement(r, info)]
        lay = QVBoxLayout(self)
        txt = QLabel(
            f"<b>GPU:</b> {gpu}" + (f" · driver {info.nv_driver}" if info.nv_driver else "")
            + f"<br><b>Session:</b> {info.session or '?'}"
            + f"<br><b>Launchers:</b> {', '.join(found) or 'none found'}"
            + "<br><br><b>Recipes that fit this machine:</b><br>• " + "<br>• ".join(fits[:8])
            + "<br><br>Pick settings in the editor or start from a recipe, save it as a profile, "
              "then write it to a game from the Games tab or a launcher tab.")
        txt.setWordWrap(True)
        lay.addWidget(txt)
        row = QHBoxLayout()
        row.addStretch(1)
        rec = QPushButton("Open recipes")
        rec.clicked.connect(lambda: (self.close(), win.open_recipes()))
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        row.addWidget(rec)
        row.addWidget(close)
        lay.addLayout(row)
