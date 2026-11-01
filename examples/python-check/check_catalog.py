"""Check a small product catalog using only Python's standard library."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
import time


def check_catalog(path: Path) -> tuple[int, int]:
    """Reject duplicate SKUs and invalid quantities/prices; return count and value in cents."""
    products = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(products, list) or not products:
        raise ValueError("catalog must be a nonempty list")
    seen = set()
    total_cents = 0
    for index, product in enumerate(products):
        if not isinstance(product, dict):
            raise ValueError(f"product {index}: expected an object")
        sku = product.get("sku")
        if not isinstance(sku, str) or not sku.strip():
            raise ValueError(f"product {index}: sku must be a nonempty string")
        if sku in seen:
            raise ValueError(f"duplicate sku: {sku}")
        seen.add(sku)
        for field in ("quantity", "priceCents"):
            value = product.get(field)
            if type(value) is not int or value < 0:
                raise ValueError(f"{sku}: {field} must be a nonnegative integer")
        total_cents += product["quantity"] * product["priceCents"]
    return len(products), total_cents


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--delay-sec", type=float, default=0, help="simulate slow input for the timeout example")
    arguments = parser.parse_args()
    if not math.isfinite(arguments.delay_sec) or arguments.delay_sec < 0:
        parser.error("--delay-sec must be finite and nonnegative")
    print(f"Checking {arguments.catalog.name}", flush=True)
    time.sleep(arguments.delay_sec)
    try:
        count, total_cents = check_catalog(arguments.catalog)
    except (OSError, ValueError) as error:
        print(f"Catalog check failed: {error}", file=sys.stderr, flush=True)
        return 1
    print(f"Checked {count} products; inventory value {total_cents} cents", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
