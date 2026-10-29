"""Command line for the vault connector.

``python -m src.brain.connector serve`` runs the connector on 127.0.0.1.
``set-password`` asks twice for the sign-in password (at least 16
characters). ``totp-setup`` prints the ``otpauth://`` address for the
authenticator app and nothing else. ``revoke-all`` cancels every token.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Sequence

from src.brain.connector.auth.provider import set_password, totp_setup
from src.brain.connector.auth.store import AuthStore
from src.brain.connector.config import load_config
from src.brain.connector.server import AUTH_DB, serve

MIN_PASSWORD = 16


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.brain.connector")
    parser.add_argument("command", choices=("serve", "set-password", "totp-setup", "revoke-all"))
    args = parser.parse_args(argv)
    try:
        config = load_config()
    except (ValueError, FileNotFoundError) as exc:
        print(f"connector: {exc}", file=sys.stderr)
        return 2

    if args.command == "serve":
        serve(config)
        return 0

    store = AuthStore(config.data_dir / AUTH_DB)
    if args.command == "set-password":
        password = getpass.getpass("New connector password: ")
        if getpass.getpass("Again: ") != password:
            print("The two passwords differ; nothing was changed.", file=sys.stderr)
            return 1
        if len(password) < MIN_PASSWORD:
            print(
                f"The password needs at least {MIN_PASSWORD} characters; nothing was changed.",
                file=sys.stderr,
            )
            return 1
        set_password(store, password)
        print("Password set.")
    elif args.command == "totp-setup":
        print(totp_setup(store))
    else:
        count = store.revoke_all()
        print(f"Revoked {count} tokens and codes." if count else "done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
