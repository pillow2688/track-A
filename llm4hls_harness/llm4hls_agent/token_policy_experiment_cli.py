"""Evaluation-only V3 CLI exposing Dynamic Token Policy controls.

The competition product entry point accepts Fixed Token Policy only.  This
module exists solely so the frozen A/B/C experiment can still be reproduced
without making Dynamic a fourth formal runtime component.
"""

from __future__ import annotations

from .v3_prototype_cli import main


def main_entry() -> None:
    raise SystemExit(main(experimental_token_policy=True))


if __name__ == "__main__":
    main_entry()
