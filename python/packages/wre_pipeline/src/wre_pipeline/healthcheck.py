"""Container healthcheck entrypoint: python -m wre_pipeline.healthcheck <port>."""

from __future__ import annotations

import sys
import urllib.request


def main() -> None:
    port = int(sys.argv[1])
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
            if response.status != 200:
                raise SystemExit(1)
    except Exception:
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
