"""Application entry point: main()."""

import os
import sys
import time
from typing import Optional

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from .app_dialogs import UXFilter
from .base import APP_ID, APP_TITLE, apply_wayland_app_id, ICON_PATH
from .cli import _cli_parse, _cli_wants, cli_run
from .main_window import MainWindow
from .theme import APPEARANCE, apply_theme


_UX_FILTER: Optional[UXFilter] = None


def main():
    # Gerçek process adını (argv[0]) 'drstool' yapar — python-exec2c/python3
    # olarak görünmesini önler. QApplication oluşturulmadan önce, mümkün
    # olduğunca erken çağrılmalı ki Wayland platform eklentisi app_id'yi
    # okurken zaten doğru argv[0]'ı görsün.
    apply_wayland_app_id(APP_ID)
    cli_args, cli_rest = _cli_parse(sys.argv[1:])
    if _cli_wants(cli_args):
        cli_run(cli_args)                     # headless; exits
    sys.argv = sys.argv[:1] + cli_rest        # whatever is left belongs to Qt

    # PySide6 prints an unhandled exception raised inside a slot to stderr and
    # then carries on, which can leave the UI half-rebuilt and silently broken
    # (e.g. a populate() that raised after self.clear() leaves an empty list
    # with signals still blocked). Surface it instead of losing it.
    _err = {"showing": False, "last": 0.0}

    def _excepthook(exc_type, exc_value, exc_tb):
        import traceback
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        traceback.print_exception(exc_type, exc_value, exc_tb)
        # A recurring error (e.g. inside paintEvent) must not stack modal
        # dialogs: one at a time, and at most one per 2 seconds.
        if _err["showing"] or time.monotonic() - _err["last"] < 2.0:
            return
        _err["showing"] = True
        try:
            QMessageBox.critical(
                None, "Internal error",
                "".join(traceback.format_exception_only(exc_type, exc_value)),
            )
        except Exception:
            pass
        finally:
            _err["showing"] = False
            _err["last"] = time.monotonic()

    sys.excepthook = _excepthook

    # Wayland altında pencere sınıfının .desktop girdisiyle eşleşmesi için
    # QApplication oluşturulmadan önce app adı ayarlanır.
    QApplication.setDesktopFileName(APP_ID)

    scale_env = APPEARANCE["scale"] != 100 and "QT_SCALE_FACTOR" not in os.environ
    if scale_env:                              # Appearance > UI scale (read by Qt while the app is created)
        os.environ["QT_SCALE_FACTOR"] = f"{APPEARANCE['scale'] / 100:g}"
    app = QApplication(sys.argv)
    if scale_env:
        os.environ.pop("QT_SCALE_FACTOR", None)   # do not leak into the games / tools we start
    app.setStyle("Fusion")
    app.setApplicationName(APP_TITLE)
    app.setApplicationDisplayName(APP_TITLE)

    if ICON_PATH.is_file():
        app.setWindowIcon(QIcon(str(ICON_PATH)))
    else:
        # Paket kurulumunda script /usr/lib/python-exec/ altında durur ve yanında
        # assets/ yoktur; ebuild ikonu hicolor temasına kurar.
        themed = QIcon.fromTheme(APP_ID)
        if not themed.isNull():
            app.setWindowIcon(themed)
    # .desktop girdisini (Exec=drstool) ebuild /usr/share/applications altına
    # kurar; kullanıcı dizinine ikinci bir python3 sarmalı girdi yazmak KDE'de
    # StartupWMClass eşleşmesini bozduğu için uygulama bunu kendisi yapmaz.

    apply_theme(app)

    window = MainWindow()
    global _UX_FILTER
    _UX_FILTER = UXFilter(app)       # after the build: it still names later pages and dialogs on Show
    _UX_FILTER.label_tree(window)
    if cli_args.profile and not window.load_profile_by_name(cli_args.profile):
        window.statusBar().showMessage(f'No such profile: {cli_args.profile}', 8000)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
