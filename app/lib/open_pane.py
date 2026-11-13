#!/usr/bin/env python3
"""Open-or-focus a plugin terminal from the App mod (single source of truth).

The mod execs this helper instead of reimplementing the label/adoption logic:

    python open_pane.py herdr.quota --script <root>/plugins/herdr-quota/bin/dashboard.py \
        --name herdr-quota --title "LLM Usage" [--machine dev] [--space dashboards] [--no-focus]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import herdr_app


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("plugin_id")
    parser.add_argument("--script", help="Python script to run in the terminal")
    parser.add_argument("--argv", help="JSON argv overriding --script")
    parser.add_argument("--name", required=True, help="unique terminal name (a-z 0-9 - _)")
    parser.add_argument("--cwd")
    parser.add_argument("--machine")
    parser.add_argument("--space", default=herdr_app.DEFAULT_SPACE_NAME)
    parser.add_argument("--no-focus", action="store_true")
    args = parser.parse_args(argv[1:])

    if args.argv:
        argv = json.loads(args.argv)
    elif args.script:
        argv = [herdr_app.python_executable(), args.script]
    else:
        parser.error("provide --script or --argv")
    try:
        address = herdr_app.open_plugin_pane(
            args.plugin_id,
            argv,
            name=args.name,
            cwd=args.cwd,
            machine=args.machine or None,
            space_name=args.space,
            focus=not args.no_focus,
        )
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    print(json.dumps({"terminal": address}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
