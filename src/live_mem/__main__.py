# -*- coding: utf-8 -*-
"""Entry point for the service and local-only maintenance commands."""

from __future__ import annotations

import sys


def _main() -> int | None:
    # Dispatch local key generation before importing server/config globals.
    # This keeps key generation independent from service startup readiness and
    # ensures the private key is never passed through MCP or HTTP surfaces.
    if sys.argv[1:2] == ["mesh-keygen"]:
        from .mesh.cli import mesh_keygen_main

        return mesh_keygen_main(sys.argv[2:])

    from .server import main

    main()
    return None


if __name__ == "__main__":
    result = _main()
    if result is not None:
        raise SystemExit(result)
