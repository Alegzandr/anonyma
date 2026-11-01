"""GUI entry point: ``python -m anonyma.app``."""

from __future__ import annotations

import multiprocessing
import sys
from collections.abc import Sequence


def _configure_qt() -> None:
    """Keep every Qt-side write inside the portable folder."""
    from PySide6.QtCore import QSettings, Qt
    from PySide6.QtGui import QGuiApplication

    from anonyma import paths

    # Never the registry: any QSettings user goes to data/qt/*.ini.
    qt_dir = str(paths.data_dir() / "qt")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    for scope in (QSettings.Scope.UserScope, QSettings.Scope.SystemScope):
        QSettings.setPath(QSettings.Format.IniFormat, scope, qt_dir)
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    if sys.platform == "win32":
        try:  # own taskbar icon instead of python.exe's (no registry write)
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Anonyma.Anonyma")
        except (AttributeError, OSError):
            pass


def install_french(app: object) -> bool:
    """French for Qt's own texts (standard buttons, context menus, file system model)
    and French number formatting in every widget. True when the translation loaded."""
    from pathlib import Path

    import PySide6
    from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
    from PySide6.QtWidgets import QApplication

    QLocale.setDefault(QLocale(QLocale.Language.French, QLocale.Country.France))
    if not isinstance(app, QApplication):
        return False
    translator = QTranslator(app)
    folders = (
        QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath),
        # Frozen build: shipped next to the PySide6 package (see packaging/anonyma.spec).
        str(Path(PySide6.__file__).resolve().parent / "translations"),
    )
    for folder in folders:
        if folder and translator.load("qtbase_fr", folder):
            app.installTranslator(translator)
            return True
    return False


def main(argv: Sequence[str] | None = None) -> int:
    multiprocessing.freeze_support()
    from anonyma import logs, netguard, paths

    paths.configure_environment()
    netguard.install()
    paths.clean_tmp()
    logs.setup_logging()
    _configure_qt()

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from anonyma import updater

    if updater.can_self_update():
        updater.cleanup()  # leftovers of a previous update

    from anonyma.gui import dialogs, errors, icons, theme
    from anonyma.gui.context import AppContext
    from anonyma.gui.main_window import MainWindow

    app = QApplication(list(argv) if argv is not None else sys.argv)
    app.setApplicationName("Anonyma")
    app.setApplicationDisplayName("Anonyma")
    app.setWindowIcon(icons.app_icon())
    install_french(app)
    errors.install()

    context = AppContext()

    def apply_theme() -> None:
        theme.apply_theme(str(context.ui_pref("theme", theme.DEFAULT_THEME)))

    apply_theme()

    def system_scheme_changed() -> None:
        if context.ui_pref("theme", theme.DEFAULT_THEME) == "systeme":
            apply_theme()

    app.styleHints().colorSchemeChanged.connect(system_scheme_changed)

    window = MainWindow(context)
    window.show()
    if context.load_error:
        dialogs.warning(window, context.load_error)
    QTimer.singleShot(1500, window.updates.check_on_startup)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
