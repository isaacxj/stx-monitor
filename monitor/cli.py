"""Commands you run on the server, inside the container:

    docker exec -it stx-monitor python -m monitor set-password
    docker exec -it stx-monitor python -m monitor status
    docker exec stx-monitor python -m monitor unlock
    docker exec stx-monitor python -m monitor reset-passkeys
    docker exec stx-monitor python -m monitor sign-out-everywhere
"""

from __future__ import annotations

import argparse
import getpass
import sys

from .auth import Auth
from .config import settings
from .store import Store


def _store() -> Store:
    return Store(settings.data_dir / "monitor.db")


def set_password(args: argparse.Namespace) -> int:
    auth = Auth(_store())
    username = args.username or auth.username() or (settings.owner or "admin").lower()
    print(f"Setting the password for {username}.")
    first = getpass.getpass("New password: ")
    second = getpass.getpass("Same again: ")
    if first != second:
        print("The two passwords don't match. Nothing changed.", file=sys.stderr)
        return 1
    try:
        auth.set_password(username, first)
    except ValueError as exc:
        print(f"{exc}. Nothing changed.", file=sys.stderr)
        return 1
    signed_out = auth.store.delete_all_sessions() if args.sign_out else 0
    auth.store.clear_failed_sign_ins()
    print(f"Password saved for {username}." + (f" Signed out {signed_out} sessions." if signed_out else ""))
    return 0


def status(args: argparse.Namespace) -> int:
    auth = Auth(_store())
    print(f"User:      {auth.username() or '(none yet)'}")
    print(f"Password:  {'set' if auth.has_password() else 'not set'}")
    print(f"Passkeys:  {len(auth.store.list_passkeys())}")
    print(f"Sessions:  {len(auth.store.list_sessions())}")
    locked = auth.locked_for()
    print(f"Sign-in:   {f'paused for {locked // 60 + 1} more min' if locked else 'open'}")
    return 0


def unlock(args: argparse.Namespace) -> int:
    _store().clear_failed_sign_ins()
    print("Failed attempts cleared. You can sign in again.")
    return 0


def reset_passkeys(args: argparse.Namespace) -> int:
    n = _store().delete_all_passkeys()
    print(f"Removed {n} passkey{'s' * (n != 1)}. Sign in with the password and add them again.")
    return 0


def sign_out_everywhere(args: argparse.Namespace) -> int:
    n = _store().delete_all_sessions()
    print(f"Signed out {n} session{'s' * (n != 1)}.")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m monitor", description="stx-monitor server commands")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("set-password", help="set the username and password (asks for the password)")
    p.add_argument("--username", help="defaults to the current one, or STX_OWNER in lower case")
    p.add_argument("--sign-out", action="store_true", help="also sign out every device")
    p.set_defaults(fn=set_password)
    sub.add_parser("status", help="show the sign-in setup").set_defaults(fn=status)
    sub.add_parser("unlock", help="end a sign-in lockout now").set_defaults(fn=unlock)
    sub.add_parser("reset-passkeys", help="remove every passkey").set_defaults(fn=reset_passkeys)
    sub.add_parser("sign-out-everywhere", help="end every session").set_defaults(fn=sign_out_everywhere)
    args = parser.parse_args(argv)
    return args.fn(args)
