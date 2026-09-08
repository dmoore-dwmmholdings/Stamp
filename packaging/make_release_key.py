"""Make the Ed25519 key pair that release manifests are signed with.

    uv run python packaging/make_release_key.py
    uv run python packaging/make_release_key.py --private-key-out stamp-release.key

Run it once.  It has two halves to hand over:

* the **public** half, to paste into ``RELEASE_PUBLIC_KEY`` in
  ``src/stamp/update.py`` and commit;
* the **private** half, to paste into the repository secret
  ``STAMP_RELEASE_KEY`` (Settings -> Secrets and variables -> Actions).

The private half is printed only to a terminal a person is sitting at.  Piped,
redirected or run from a script it goes nowhere unless ``--private-key-out``
names a file, because a key that lands in a log, a scrollback buffer or a CI
transcript is a key that has been published.  Nothing is written next to the
repository by default, so it cannot be committed by accident.  Losing it costs
one new key pair and a release that ships the new public half; leaking it means
anyone can publish something Stamp will install, so treat it the way you would a
signing certificate.

Rotating: generate a new pair, ship a release carrying the new public key, and
keep signing with the old key until enough people have that release - a client
only trusts the key it was built with.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from base64 import b64encode
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--private-key-out",
        type=Path,
        help="write the private half to this file instead of showing it",
    )
    args = parser.parse_args(argv)

    private = Ed25519PrivateKey.generate()
    raw_private = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    raw_public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    print()
    print("Public key - paste into RELEASE_PUBLIC_KEY in src/stamp/update.py:")
    print()
    print(f'RELEASE_PUBLIC_KEY = "{b64encode(raw_public).decode()}"')
    print()

    secret = b64encode(raw_private).decode()
    if args.private_key_out is not None:
        target = args.private_key_out
        # Made empty and readable only by this user before anything is in it:
        # writing the key first and fixing the mode after leaves a moment when
        # it is there for everybody.
        handle = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(secret + "\n")
        print(f"Private key written to {target}.")
        print("Paste it into the STAMP_RELEASE_KEY repository secret, then delete it.")
        print()
        return 0

    if not sys.stdout.isatty():
        print(
            "Private key NOT shown: this is not a terminal, and a key that goes "
            "into a pipe, a log or a CI transcript has been published. Run this "
            "at a prompt, or pass --private-key-out FILE.",
            file=sys.stderr,
        )
        return 1

    print("Private key - paste into the STAMP_RELEASE_KEY repository secret.")
    print("It is shown once and not saved anywhere:")
    print()
    print(secret)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
