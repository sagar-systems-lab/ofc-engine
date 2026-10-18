from __future__ import annotations

import argparse
import json
import os
import sys

from .storage import ControlStore


def main(argv=None):
    parser = argparse.ArgumentParser(description="Apply additive control-review storage migrations")
    parser.add_argument("command", choices=["migrate"])
    parser.add_argument("--database-url-env", default="OFC_CONTROL_DATABASE_URL")
    args = parser.parse_args(argv)
    dsn = os.environ.get(args.database_url_env, "")
    if not dsn.startswith(("postgres://", "postgresql://")):
        parser.error("an explicit PostgreSQL environment variable is required")
    try:
        ControlStore(dsn, initialize=True)
    except Exception as exc:
        print(json.dumps({"status": "MIGRATION_FAILED", "error_type": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps({"status": "MIGRATED", "schema": "ofc_control", "provider_calls": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
