"""Make the Ed25519 key pair that release manifests are signed with.

    uv run python packaging/make_release_key.py

Run it once.  It prints two things and stores neither:

* the **public** half, to paste into ``RELEASE_PUBLIC_KEY`` in
  ``src/stamp/update.py`` and commit;
* the **private** half, to paste into the repository secret
  ``STAMP_RELEASE_KEY`` (Settings -> Secrets and variables -> Actions).

The private half must not go in the repository, and it is not written to disk
here so that it cannot be committed by accident.  Losing it costs one new key
pair and a release that ships the new public half; leaking it means anyone can
publish something Stamp will install, so treat it the way you would a signing
certificate.

Rotating: generate a new pair, ship a release carrying the new public key, and
keep signing with the old key until enough people have that release - a client
only trusts the key it was built with.
"""

from __future__ import annotations

from base64 import b64encode

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> int:
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
    print("Private key - paste into the STAMP_RELEASE_KEY repository secret.")
    print("It is shown once and not saved anywhere:")
    print()
    print(b64encode(raw_private).decode())
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
