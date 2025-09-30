import argparse
import sys

from PySide6.QtWidgets import QApplication

from . import MonitorWindow, config_log


def start():
    """Console entry point.

    Accepts --log=<LEVEL> and passes it to config_log. Uses parse_known_args so
    Qt/other args are preserved for QApplication.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--log", help="Set log level (DEBUG, INFO, WARNING, ERROR)", default=None
    )
    args, _ = parser.parse_known_args()

    if args.log:
        config_log(args.log)
    else:
        config_log()

    app = QApplication(sys.argv)
    _ = MonitorWindow()
    sys.exit(app.exec())
