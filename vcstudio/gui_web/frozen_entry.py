"""PyInstaller entry point with a window-free frozen healthcheck mode."""
from __future__ import annotations

import sys
from typing import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if '--healthcheck' in args:
        from vcstudio.gui_web.frozen_healthcheck import main as healthcheck_main
        return healthcheck_main(args)

    from vcstudio.gui_web.__main__ import main as gui_main
    return gui_main()


if __name__ == '__main__':
    raise SystemExit(main())
