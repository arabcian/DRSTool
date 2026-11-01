"""GPU architecture list and detail pages."""

from typing import List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget, QPushButton, QVBoxLayout, QWidget,
)

from drstool_core.models import GPUArch

from .favorites import FAVS
from .lookup import arch_by_code, arch_rank, detect_real_arch
from .setting_editor import EDITOR_CRUMB_SS, EDITOR_DESC_SS, EDITOR_TITLE_SS
from .theme import btn_ss, T
from .widgets import make_header_item, make_list_item, setup_sidebar_list


# ============================================================================
# Arch List - Same style as Settings List
# ============================================================================

class ArchListWidget(QListWidget):
    """List of GPU architectures, drawn by the shared SidebarDelegate."""
    arch_selected = Signal(object)  # emits GPUArch

    def __init__(self):
        super().__init__()
        self.setSelectionMode(QListWidget.SingleSelection)
        self.itemClicked.connect(self._on_item_clicked)
        self.currentItemChanged.connect(self._on_current_item_changed)
        setup_sidebar_list(self)

    def populate(self, archs: List[GPUArch], selected_arch: Optional[GPUArch],
                 only_set: bool = False, only_fav: bool = False):
        """Fill the list with architecture items, highlighting the selected one."""
        # Block currentItemChanged for the rebuild - clear()+re-add can fire
        # spurious selection-changed signals we don't want driving navigation.
        self.blockSignals(True)
        self.clear()
        self.addItem(make_header_item("GPU architectures", 1 if selected_arch else 0))
        real = detect_real_arch()
        for arch in archs:
            is_sel = bool(selected_arch and arch.code == selected_arch.code)
            if only_set and not is_sel:
                continue
            if only_fav and not FAVS.is_fav("arch", arch.code):
                continue
            mine = arch.code == real
            item = make_list_item(f"{arch.name}  ·  {arch.arch}" + ("   ◆ your GPU" if mine else ""), arch,
                                  is_set=is_sel, value=arch.code,
                                  fav=FAVS.is_fav("arch", arch.code))
            if mine:
                item.setToolTip("This is the generation of the NVIDIA card in this machine — "
                                "spoofing it changes nothing.")
            self.addItem(item)
        self.blockSignals(False)

    def _on_item_clicked(self, item):
        arch = item.data(Qt.UserRole)
        if arch:
            self.arch_selected.emit(arch)

    def _on_current_item_changed(self, current, previous):
        if current is None:
            return
        arch = current.data(Qt.UserRole)
        if arch:
            self.arch_selected.emit(arch)


# ============================================================================
# Arch Detail - Shows info and a Clear button (similar to SettingEditor)
# ============================================================================

class ArchDetailWidget(QWidget):
    """Detail view for a selected architecture, with a Clear button."""
    clear_requested = Signal()

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(0, 0, 0, 0)

        crumb = QLabel("GPU ARCH  \u203a  DXVK_NVAPI_GPU_ARCH")
        crumb.setStyleSheet(EDITOR_CRUMB_SS)
        layout.addWidget(crumb)

        head = QHBoxLayout()
        self._name_label = QLabel()
        self._name_label.setStyleSheet(EDITOR_TITLE_SS)
        self._name_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        head.addWidget(self._name_label, 1)
        self._clear_btn = QPushButton("Clear")
        self._clear_btn.setToolTip("Stop spoofing — report the real GPU")
        self._clear_btn.setCursor(Qt.PointingHandCursor)
        self._clear_btn.clicked.connect(self.clear_requested)
        self._clear_btn.setStyleSheet(btn_ss("danger", "sm"))
        head.addWidget(self._clear_btn, 0, Qt.AlignVCenter)
        layout.addLayout(head)

        spec = QFrame()
        spec.setObjectName("archSpec")
        spec.setStyleSheet(f"#archSpec {{ background:{T['surface']}; border:1px solid {T['border']};"
                           " border-radius:10px; }")
        sl = QVBoxLayout(spec)
        sl.setContentsMargins(16, 12, 16, 12)
        sl.setSpacing(6)
        self._arch_label = QLabel()
        self._arch_label.setStyleSheet(f"color:{T['text']}; font-size:14px; font-weight:600;")
        self._arch_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        sl.addWidget(self._arch_label)
        self._code_label = QLabel()
        self._code_label.setStyleSheet(f"font-family:{T['mono']}; font-size:12px; color:{T['accent_hi']};")
        self._code_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        sl.addWidget(self._code_label)
        layout.addWidget(spec)

        self._desc_label = QLabel(
            "Makes DXVK-NVAPI report this architecture instead of your real "
            "GPU's (DXVK_NVAPI_GPU_ARCH). It is a spoof meant for "
            "troubleshooting or unlocking features a game gates on a newer "
            "generation — leave it unset to report the real card."
        )
        self._desc_label.setWordWrap(True)
        self._desc_label.setStyleSheet(EDITOR_DESC_SS)
        layout.addWidget(self._desc_label)

        self._rel_label = QLabel()
        self._rel_label.setWordWrap(True)
        self._rel_label.setTextFormat(Qt.RichText)
        self._rel_label.setStyleSheet(f"font-size:13px; background:{T['raised']}; border:1px solid "
                                      f"{T['border_hi']}; border-radius:8px; padding:10px 12px;")
        self._rel_label.hide()
        layout.addWidget(self._rel_label)

        layout.addStretch()

    def set_arch(self, arch: Optional[GPUArch]):
        """Update the detail view with the given architecture (or clear it)."""
        if arch:
            self._name_label.setText(arch.name)
            self._arch_label.setText(f"Architecture: {arch.arch}")
            self._code_label.setText(f"Code: {arch.code}  ·  Example: {arch.example}")
            self._clear_btn.show()
            real = detect_real_arch()
            ra = arch_by_code(real)
            if ra is None:
                self._rel_label.hide()
            else:
                if real == arch.code:
                    col, txt = T["muted"], (f"◆ Your card is {ra.name} ({ra.arch}) — this spoof is "
                                            "identical to what dxvk-nvapi already reports.")
                elif arch_rank(arch.code) > arch_rank(real):
                    col, txt = T["warn"], (f"▲ Newer than your {ra.name} ({ra.arch}). Games may "
                                           "switch on features this hardware lacks (frame generation, "
                                           "newer DLSS paths) and crash or render black.")
                else:
                    col, txt = T["text2"], (f"▼ Older than your {ra.name} ({ra.arch}). Games may hide "
                                            "DLSS / Frame Generation / Reflex — use it as a workaround "
                                            "for titles that misbehave on new generations.")
                self._rel_label.setText(f'<span style="color:{col}">{txt}</span>')
                self._rel_label.show()
        else:
            self._name_label.setText("")
            self._arch_label.setText("")
            self._code_label.setText("")
            self._clear_btn.hide()
            self._rel_label.hide()
