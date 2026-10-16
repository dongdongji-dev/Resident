"""Console entry point::

    python -m kernel.console --config config/console.yaml
"""

from __future__ import annotations

from kernel.console.app import main

if __name__ == "__main__":
    raise SystemExit(main())
