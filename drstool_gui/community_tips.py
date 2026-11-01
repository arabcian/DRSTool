"""Community tips page (ProtonDB user reports)."""

from datetime import datetime

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from .game_hints import (
    _IDX_MONTHS, _IndexWorker, community_entries, load_env_index, reports_for_app,
)
from .sysinfo import SYSHUB
from .tasks import _NAV, TASKS
from .theme import T
from .tool_ui import _tool_btn
from .widgets import _COMBO_QSS, _table_qss, _title_label


class CommunityTipsWidget(QWidget):
    """ProtonDB user-reported settings — deliberately separate, gated and never preselected."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._appid = ""
        self._info: dict = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)
        lay.addWidget(_title_label("Community tips — use at your own risk"))
        warn = QLabel(
            "<b>USE AT YOUR OWN RISK.</b> These settings were mentioned in ProtonDB <i>user reports</i>. "
            "They are individual experiences, not tested or verified by DRSTool or the game's developers, and can "
            "behave differently on other hardware, GPU drivers, kernels, Proton builds and Linux setups — they may "
            "cause crashes, lower performance or reduce stability. Compare the reporter's hardware, Proton and "
            "system with yours before trying anything. <b>Never apply them blindly:</b> change one at a time, test "
            "the game, and keep only what helps.")
        warn.setWordWrap(True)
        warn.setStyleSheet(f"color:{T['text']}; background:#2a1414; border:1px solid {T['danger']};"
                           "border-radius:4px; padding:8px 10px; font-size:12px;")
        lay.addWidget(warn)
        frow = QHBoxLayout()
        self._nv = QCheckBox("NVIDIA reports only")
        self._nv.setChecked(bool(SYSHUB.info.has_nvidia))
        self._period = QComboBox()
        self._period.setStyleSheet(_COMBO_QSS)
        for label, months in (("last 6 months", 6), ("last 12 months", 12)):
            self._period.addItem(label, months)
        self._cat = QCheckBox("only variables DRSTool can set")
        for w_ in (self._nv, self._cat):
            w_.setStyleSheet(f"color:{T['text2']};")
            w_.toggled.connect(lambda _c: self._rebuild())
        self._period.currentIndexChanged.connect(lambda _i: self._rebuild())
        frow.addWidget(self._nv)
        frow.addWidget(self._period)
        frow.addWidget(self._cat)
        frow.addStretch(1)
        lay.addLayout(frow)
        drow = QHBoxLayout()
        self._dl = _tool_btn("Download / update ProtonDB report data")
        self._dl.clicked.connect(self._download)
        drow.addWidget(self._dl)
        self._data_lbl = QLabel("")
        self._data_lbl.setWordWrap(True)
        self._data_lbl.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        drow.addWidget(self._data_lbl, 1)
        lay.addLayout(drow)
        self._worker = None
        self._refresh_data_lbl()
        self._info_lbl = QLabel("Look up a game on the Game hints tab first.")
        self._info_lbl.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        self._info_lbl.setWordWrap(True)
        lay.addWidget(self._info_lbl)
        self._table = QTableWidget(0, 10)
        self._table.setHorizontalHeaderLabels(["Use", "Variable", "Value", "Seen", "Date", "GPU · driver",
                                               "Proton", "OS · kernel", "Verdict", "Where"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.NoSelection)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setStretchLastSection(True)
        self._table.setStyleSheet(_table_qss())
        lay.addWidget(self._table, 1)
        self._ack = QCheckBox("I understand these are unverified user reports and I use them at my own risk")
        self._ack.setStyleSheet(f"color:{T['warn']};")
        self._ack.toggled.connect(lambda _c: self._sync_btn())
        lay.addWidget(self._ack)
        btns = QHBoxLayout()
        self._apply = _tool_btn("Apply checked", bg="#33270f", border=T["warn"], fg="#f0d79a", bold=True)
        self._apply.clicked.connect(self._apply_checked)
        btns.addWidget(self._apply)
        pb = _tool_btn("Open ProtonDB page")
        pb.clicked.connect(lambda: self._appid and QDesktopServices.openUrl(
            QUrl(f"https://www.protondb.com/app/{self._appid}")))
        btns.addWidget(pb)
        btns.addStretch(1)
        lay.addLayout(btns)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{T['muted']}; font-size:12px;")
        lay.addWidget(self._status)
        self._sync_btn()

    def _sync_btn(self):
        self._apply.setEnabled(self._ack.isChecked() and self._table.rowCount() > 0)

    def _refresh_data_lbl(self):
        idx = load_env_index()
        if idx:
            self._data_lbl.setText(
                f"Local data: {idx.get('dump', '?')} (downloaded {datetime.fromtimestamp(idx.get('built', 0)):%Y-%m-%d}), "
                f"{idx.get('kept', 0)} reports with settings from the last {_IDX_MONTHS} months, "
                f"{len(idx.get('by_app', {}))} games. ProtonDB publishes a new dump about monthly.")
        else:
            self._data_lbl.setText("No report data yet. ProtonDB publishes its user reports as a public monthly "
                                   "dump (~70 MB, github.com/bdefore/protondb-data); download it once, it is then "
                                   "searched offline for every game.")

    def _download(self):
        if self._worker and self._worker.isRunning():
            return
        self._dl.setEnabled(False)
        self._worker = _IndexWorker(self)
        TASKS.track(self._worker, "Community tips: ProtonDB data", _NAV("Community tips"),
                    self._worker.progress, self._worker.done)
        self._worker.progress.connect(self._data_lbl.setText)
        self._worker.done.connect(self._download_done)
        self._worker.start()

    def _download_done(self, err: str):
        self._dl.setEnabled(True)
        self._refresh_data_lbl()
        if err:
            self._data_lbl.setText(f"⚠ Download failed: {err}")
            return
        if self._appid:
            self._info["reports"] = reports_for_app(self._appid)
            self._rebuild()

    def set_data(self, info: dict):
        self._info = info
        self._appid = str(info.get("appid", ""))
        self._ack.setChecked(False)
        self._rebuild()

    @staticmethod
    def _date(t) -> str:
        return datetime.fromtimestamp(t).strftime("%Y-%m-%d") if t else "?"

    def _rebuild(self):
        info = self._info
        t = self._table
        t.setRowCount(0)
        if not info:
            self._sync_btn()
            return
        w = self.window()
        cur = w.collect_launch_payload().get("env", {}) if hasattr(w, "collect_launch_payload") else {}
        rp = info.get("reports")
        if rp is None:
            self._info_lbl.setText(f"appid {self._appid}: no report data downloaded yet — use the button above.")
            self._sync_btn()
            return
        ents, st = community_entries(info, SYSHUB.info, cur, self._period.currentData() or 0,
                                     self._nv.isChecked(), self._cat.isChecked())
        for r, e in enumerate(ents):
            it = e["item"]
            t.insertRow(r)
            chk = QTableWidgetItem("")
            if e["known"]:
                chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                chk.setCheckState(Qt.Unchecked)
            else:
                chk.setFlags(Qt.ItemIsEnabled)
                chk.setToolTip("Not in DRSTool's variable catalog — shown for information; add it by hand if you want it.")
            t.setItem(r, 0, chk)
            gpu = " · ".join(x for x in (it["gpu"], it["drv"]) if x) or it["sys"] or "?"
            osk = " · ".join(x for x in (it["os"], it["kern"]) if x) or "?"
            cells = (e["name"], e["value"] + ("   (set now)" if e["current"] == e["value"] else ""),
                     f"{e['seen']}×", self._date(it["t"]), gpu, it["proton"] or "?", osk,
                     it["verdict"] or "?", e["where"])
            tip = (f"GPU: {it['gpu'] or '?'}  driver: {it['drv'] or '?'}\nCPU: {it['cpu'] or '?'}  RAM: {it['ram'] or '?'}\n"
                   f"OS: {it['os'] or '?'}  kernel: {it['kern'] or '?'}\nProton: {it['proton'] or '?'}  "
                   f"verdict: {it['verdict'] or '?'}\nReport date: {self._date(it['t'])}\n"
                   f"Currently here: {e['current'] or '(not set)'}")
            for c, txt in enumerate(cells, 1):
                cell = QTableWidgetItem(txt)
                cell.setToolTip(tip)
                cell.setForeground(QColor(T["warn"] if e["known"] else T["muted"]))
                t.setItem(r, c, cell)
        self._info_lbl.setText(
            f"appid {self._appid}: {st['total']} report(s) with settings in the data;"
            f" {st['window']} match the filters; {len(ents)} setting example(s) shown. "
            "Hover a row for the reporter's CPU, RAM and full system details."
            + ("" if st["vendors_known"] else " (GPU vendor not present in the data — NVIDIA filter not applied.)")
            + ("" if st["total"] else "  No report for this game in the last 12 months mentions environment "
                                      "variables (others may only describe settings in prose)."))
        self._status.setText("")
        self._sync_btn()

    def _apply_checked(self):
        t = self._table
        pick = [(t.item(r, 1).text(), t.item(r, 2).text().replace("   (set now)", "")) for r in range(t.rowCount())
                if t.item(r, 0).checkState() == Qt.Checked]
        if not pick:
            self._status.setText("Tick the settings you want to try first.")
            return
        if QMessageBox.warning(
                self, "Apply community settings",
                "Apply these user-reported settings?\n\n" + "\n".join(f"  {k}={v}" for k, v in pick) +
                "\n\nThey are unverified and may break the game or lower performance. Test them one at a time.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        w = self.window()
        for k, v in pick:
            w.set_env_var(k, v)
        self._status.setText(f"Applied {len(pick)} community setting(s) to the Environment tab — "
                             "test the game; remove them if anything gets worse.")
