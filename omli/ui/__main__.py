"""``omli-ui``: init (make your login key), sign (answer a sign-in code), login (sign in the
browser in one step), serve.

No command takes the passphrase or the key as an option, so neither appears on a command
line.

There is deliberately no option for the bind address or for turning login off.
"""

from __future__ import annotations

import os
import sys

if __name__ == "__main__":
    # Run with ``python -m``, Python puts the folder it was started in first on the
    # import path. Drop it before importing anything else, so no module this command imports
    # from here on (argparse, say) is loaded from there. Python's own start-up modules, and
    # omli itself, are imported before this line and may already have come from that folder;
    # only the ``omli-ui`` command, or ``python -P``/``-I``, avoids that.
    _start = os.path.normcase(os.path.abspath(os.getcwd()))
    sys.path[:] = [p for p in sys.path
                   if p and os.path.normcase(os.path.abspath(p)) != _start]

import argparse  # noqa: E402
import getpass  # noqa: E402
from typing import List, Optional  # noqa: E402

_EPILOG = (
    "The login key is protected by a passphrase, which init and sign ask for with a hidden "
    "prompt. OMLI_UI_KEY_PASSPHRASE supplies it without a prompt; use it for scripts only: "
    "the passphrase then stays in the process environment, where other programs you run "
    "can read it."
)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="omli-ui", allow_abbrev=False, epilog=_EPILOG)
    sub = p.add_subparsers(dest="cmd", required=True)
    init = sub.add_parser("init", help="make your own login key (first run)",
                          allow_abbrev=False, epilog=_EPILOG)
    init.add_argument("--replace", action="store_true",
                      help="make a new key in place of the existing one and revoke every "
                           "key registered so far")
    sign = sub.add_parser("sign", help="sign a sign-in code from the login page",
                          allow_abbrev=False, epilog=_EPILOG)
    sign.add_argument("challenge")
    login = sub.add_parser("login", help="sign in the browser in one step (the server must "
                                         "be running)", allow_abbrev=False, epilog=_EPILOG)
    login.add_argument("--port", type=int, default=None)
    serve = sub.add_parser("serve", help="run the UI on 127.0.0.1", allow_abbrev=False)
    serve.add_argument("--port", type=int, default=None)
    return p


def _error(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def _scripted_passphrase() -> Optional[str]:
    from omli.ui import auth

    return auth._passphrase(None)


def _init(replace: bool) -> int:
    from omli.ui import auth

    pw = _scripted_passphrase()
    if pw is None:
        try:
            first = getpass.getpass("Passphrase for your new login key: ")
            try:
                auth.check_new_passphrase(first)
            except ValueError as exc:
                return _error(str(exc))
            if getpass.getpass("Type it again: ") != first:
                return _error("the two passphrases did not match; nothing was created")
        except (EOFError, KeyboardInterrupt):
            return _error("cancelled; nothing was created")
        pw = first
    try:
        priv, pubkey_id = auth.create_login_key(passphrase=pw, replace=replace)
    except (FileExistsError, ValueError, RuntimeError) as exc:
        return _error(str(exc))
    print(f"Made your login key {pubkey_id}.")
    print(f"Private key: {priv} (keep it; it never goes to the browser)")
    if replace:
        print("Every key registered before this one is revoked.")
    print(f"Public key registered in the data folder {auth.registry_path().parent.parent}. "
          "Next: omli-ui serve")
    return 0


def _sign(challenge: str) -> int:
    from omli.ui import auth

    try:
        auth.read_private_key_file()  # refuse a missing or unprotected key before prompting
        pw = _scripted_passphrase()
        if pw is None:
            try:
                pw = getpass.getpass("Login key passphrase: ")
            except (EOFError, KeyboardInterrupt):
                return _error("cancelled")
        token = auth.sign_challenge(challenge, passphrase=pw)
    except auth.LoginKeyError as exc:
        return _error(str(exc))
    except RuntimeError as exc:
        return _error(str(exc))
    print(token)
    return 0


def _login(port: int) -> int:
    from omli.ui import auth, client

    try:
        auth.read_private_key_file()  # refuse a missing or unprotected key before prompting
        pw = _scripted_passphrase()
        if pw is None:
            try:
                pw = getpass.getpass("Login key passphrase: ")
            except (EOFError, KeyboardInterrupt):
                return _error("cancelled")
        # Unlock once before contacting the server, so a wrong passphrase costs nothing.
        auth.unlock_private_key(passphrase=pw)
        client.run_login(port, lambda ch: auth.sign_challenge(ch, passphrase=pw),
                         client.http_post(port), lambda url: client.open_browser(url),
                         on_opened=lambda: print("Opened the browser to finish signing in. "
                                                 "Cleaning up when the sign-in link expires...",
                                                 flush=True))
    except (auth.LoginKeyError, client.LoginError, RuntimeError) as exc:
        return _error(str(exc))
    print("Done.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.cmd == "init":
        return _init(args.replace)
    if args.cmd == "sign":
        return _sign(args.challenge)

    try:
        from omli.ui import server
    except ImportError as exc:
        return _error(f'the UI needs the ui extra: pip install "omli[ui]" (missing: {exc.name})')

    port = args.port if args.port is not None else server.DEFAULT_PORT
    if args.cmd == "login":
        return _login(port)
    from omli.ui.home import ui_home

    print(f"OMLi UI on http://127.0.0.1:{port}/ (localhost only)")
    print(f"Mind-map: http://127.0.0.1:{port}/mindmap")
    home = ui_home()
    print(f"Data folder: {home}")
    server.serve(port=port, home=home)
    return 0


if __name__ == "__main__":
    sys.exit(main())
