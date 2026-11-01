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

    # Only what the splash needs comes before it: the main window's imports (pipeline,
    # detectors, pages) and construction are slow, slower still on a cold start.
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from anonyma.gui import errors, icons, theme
    from anonyma.gui.context import AppContext
    from anonyma.gui.splash import LOADING, OPENING, show_splash

    existing = QApplication.instance()  # the tests' own application
    if isinstance(existing, QApplication):
        app = existing
    else:
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

    splash = show_splash()
    try:
        from anonyma import updater

        update_state = None
        if updater.can_self_update():
            update_state = updater.take_state()  # outcome of the last installation
            if update_state is None or not update_state.running:
                updater.cleanup()  # leftovers of a previous update

        splash.set_status(LOADING)
        from anonyma.gui import dialogs
        from anonyma.gui.main_window import MainWindow

        splash.set_status(OPENING)
        window = MainWindow(context)
        window.show()
    finally:
        # Never left above a dialog: an error dialog or the startup ones below.
        splash.close()
    if context.load_error:
        dialogs.warning(window, context.load_error)
    window.updates.report_previous(update_state)
    QTimer.singleShot(1500, window.updates.check_on_startup)
    QTimer.singleShot(2000, window.settings.models.start_automatic)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
