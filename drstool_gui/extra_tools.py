"""Extra Tools tab: the page container that wires all tool pages together."""

from typing import Dict, List

from PySide6.QtWidgets import QScrollArea, QTabWidget, QVBoxLayout, QWidget

from .cache_manager import CacheManagerWidget
from .community_tips import CommunityTipsWidget
from .diag_panels import LinterWidget, SystemInfoWidget
from .dll_manager import DllManagerWidget
from .flm import FlmInstallWidget
from .frametime import FrametimeWidget
from .game_hints import GameHintsWidget
from .lgtune import LgtuneWidget
from .live_process import LiveProcessWidget
from .logs import LogViewerWidget
from .overlay_builders import MangoHudBuilder, VkBasaltBuilder
from .theme import SCROLLABLE_CONTROL_QSS, T
from .tool_manager import ToolManagerWidget
from .upstream_drift import UpstreamDriftWidget


class ExtraToolsWidget(QWidget):
    """
    Container that holds both sub-tools (vk_flip_meter build panel and
    lutris-game-tune settings/installer) as QTabWidget sub-tabs inside the
    Extra Tools top-level tab.
    """
    # Shared QTabWidget stylesheet (same look as the Profiles subtabs)
    _TAB_SS = f"""
QTabWidget::pane {{ border:none; border-top:1px solid {T['border']}; top:-1px; padding-top:10px; }}
QTabBar::tab {{
    background:transparent; color:{T['muted']}; border:none; border-bottom:2px solid transparent;
    padding:7px 14px; font-size:12px; font-weight:600;
}}
QTabBar::tab:selected {{ color:{T['text']}; border-bottom:2px solid {T['accent']}; }}
QTabBar::tab:hover:!selected {{ color:{T['text2']}; border-bottom:2px solid {T['border_hi']}; }}
"""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        tabs = QTabWidget()
        tabs.setStyleSheet(self._TAB_SS)

        # Tabs are built on first visit (lazy): constructing all 15 tool pages at
        # startup cost ~1.7 s of Qt style polishing for pages nobody had opened.
        self._tabs = tabs
        self._lazy: Dict[str, tuple] = {}      # title -> (page, attr, factory)
        self._lazy_attr: Dict[str, str] = {}   # attr  -> title

        def scrolled(w):
            sc = QScrollArea()
            sc.setWidgetResizable(True)
            sc.setWidget(w)
            sc.setStyleSheet(SCROLLABLE_CONTROL_QSS)
            return sc

        def lazy(title, attr, factory, wrap=None):
            page = QWidget()
            lay = QVBoxLayout(page)
            lay.setContentsMargins(0, 0, 0, 0)
            self._lazy[title] = (page, attr, factory, wrap)
            self._lazy_attr[attr] = title
            tabs.addTab(page, title)

        lazy("vk_flip_meter", "_flm_install", FlmInstallWidget, scrolled)
        lazy("lutris-game-tune", "_lgtune", LgtuneWidget)
        lazy("DLL Manager", "dllmgr", DllManagerWidget, scrolled)
        # Linter and Cache are wired to the main window at startup -> eager.
        self.linter = LinterWidget()
        tabs.addTab(self.linter, "Linter")
        lazy("System", "sysinfo", SystemInfoWidget)
        lazy("Tool Manager", "toolmgr", ToolManagerWidget, scrolled)
        lazy("MangoHud", "mangohud", MangoHudBuilder)
        lazy("vkBasalt", "vkbasalt", VkBasaltBuilder)
        self.cache = CacheManagerWidget()
        tabs.addTab(self.cache, "Cache")
        self.cache.stale_changed.connect(
            lambda stale: tabs.setTabText(tabs.indexOf(self.cache), "Cache ⚠" if stale else "Cache"))
        lazy("Upstream drift", "drift", UpstreamDriftWidget)
        lazy("Game hints", "hints", GameHintsWidget)
        lazy("Community tips", "community", CommunityTipsWidget)
        lazy("Frametime", "frametime", FrametimeWidget)
        lazy("Live process", "live", LiveProcessWidget)
        lazy("Logs", "logview", LogViewerWidget)
        tabs.currentChanged.connect(lambda i: self._build_lazy(tabs.tabText(i)))
        self._build_lazy(tabs.tabText(tabs.currentIndex()))

        # Navigation lives in the left sidebar (ExtraToolsSidebarWidget); the
        # tab strip itself would overflow with this many tools.
        tabs.tabBar().hide()
        tabs.setStyleSheet("QTabWidget::pane { border:none; }")
        layout.addWidget(tabs)

    def _build_lazy(self, title: str):
        ent = self._lazy.pop(title, None)
        if ent is None:
            return None
        page, attr, factory, wrap = ent
        w = factory()
        object.__setattr__(self, attr, w)
        page.layout().addWidget(wrap(w) if wrap else w)
        return w

    def __getattr__(self, name):
        # Only reached for attributes not set yet: build the lazy page on demand.
        lz = self.__dict__.get("_lazy_attr")
        if lz and name in lz:
            w = self._build_lazy(lz[name])
            if w is not None:
                return w
        raise AttributeError(name)

    def is_built(self, attr: str) -> bool:
        return attr in self.__dict__

    def tab_titles(self) -> List[str]:
        return [self._tabs.tabText(i).replace(" ⚠", "") for i in range(self._tabs.count())]

    # Pages merged into another one: a remembered session / palette entry still lands there.
    _RENAMED = {"DLSS Updater": "DLL Manager", "Prefix Doctor": "DLL Manager"}

    def show_tab(self, title: str) -> bool:
        title = self._RENAMED.get(title, title)
        for i in range(self._tabs.count()):
            if self._tabs.tabText(i).replace(" ⚠", "") == title:
                self._tabs.setCurrentIndex(i)
                return True
        return False
