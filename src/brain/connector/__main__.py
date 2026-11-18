"""Command line for the vault connector.

``python -m src.brain.connector serve`` runs the connector on 127.0.0.1.
``set-password`` asks twice for the sign-in password (at least 16
characters). ``totp-setup`` prints the ``otpauth://`` address for the
authenticator app and nothing else. ``revoke-all`` cancels every token.
``--person KEY`` names whose password, code or tokens these three change; with
more than one person in the settings it is required for ``set-password`` and
``totp-setup``, and ``revoke-all`` without it cancels everyone's tokens.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Sequence

from src.brain.connector.auth.provider import set_password, totp_setup
from src.brain.connector.auth.store import AuthStore
from src.brain.connector.config import load_config
from src.brain.connector.people import DEFAULT_PERSON_KEY, Person
from src.brain.connector.server import AUTH_DB, serve

MIN_PASSWORD = 16


def _label(person: Person) -> str:
    """The name of the person's entry in the authenticator app."""
    return "owner" if person.key == DEFAULT_PERSON_KEY else person.name


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.brain.connector")
    parser.add_argument("command", choices=("serve", "set-password", "totp-setup", "revoke-all"))
    parser.add_argument(
        "--person", help="set-password, totp-setup, revoke-all: the person's key in the settings"
    )
    args = parser.parse_args(argv)
    try:
        config = load_config()
    except (ValueError, FileNotFoundError) as exc:
        print(f"connector: {exc}", file=sys.stderr)
        return 2

    if args.command == "serve":
        serve(config)
        return 0

    keys = ", ".join(person.key for person in config.people)
    person: Person | None = None
    if args.person is not None:
        person = config.person(args.person)
        if person is None:
            print(
                f"connector: no person {args.person} in the settings; they are: {keys}",
                file=sys.stderr,
            )
            return 2
    elif len(config.people) == 1:
        person = config.root_person

    store = AuthStore(config.data_dir / AUTH_DB)
    store.adopt(config.root_person.key)
    if args.command == "revoke-all":
        count = store.revoke_all(None if args.person is None else args.person)
        print(f"Revoked {count} tokens and codes." if count else "done")
        return 0
    if person is None:
        print(f"connector: {args.command} needs --person, one of: {keys}", file=sys.stderr)
        return 2
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
        set_password(store, person.key, password)
        print("Password set.")
    else:
        print(totp_setup(store, person.key, _label(person)))
    return 0

if __name__ == "__main__":
    sys.exit(main())
