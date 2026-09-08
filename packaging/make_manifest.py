"""Write and sign ``latest.json``, the thing Stamp checks for an update.

    STAMP_RELEASE_KEY=... python packaging/make_manifest.py \
        --version 1.5.0 --tag v1.5.0 --assets ./assets \
        --repo dmoore-dwmmholdings/Stamp --output ./out

It reads the installers that the release jobs already built, hashes them, and
writes three files:

* ``latest.json``      - the version, the release notes URL, and one artifact
                         per platform with its size and SHA-256;
* ``latest.json.sig``  - an Ed25519 signature over the exact bytes of
                         ``latest.json``, which is what Stamp verifies before it
                         will so much as parse the manifest;
* ``SHA256SUMS``       - the same hashes in the usual format, for a person
                         checking a download by hand.

The signature covers the manifest, and the manifest carries the hashes, so one
signature covers every artifact.  The private key never leaves the Actions
secret: this runs in the release workflow, not on anybody's machine.

Run with no key and it fails rather than writing an unsigned manifest.  Stamp
rejects an unsigned one anyway, and half a feed is worse than none.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from base64 import b64decode, b64encode
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

#: Which built file belongs to which platform Stamp asks about.  The suffix is
#: matched against the file name the release jobs produce.
PLATFORMS = {
    "windows-x86_64": "-Setup.exe",
    "macos-arm64": "-macos-arm64.dmg",
    "macos-x86_64": "-macos-x86_64.dmg",
}


class ManifestError(RuntimeError):
    """A reason not to sign anything."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(
    version: str,
    tag: str,
    assets: Path,
    repo: str,
    allow_missing: bool = False,
) -> tuple[dict, list[Path]]:
    """The manifest for *version*, over the installers that carry that version.

    Named rather than sorted.  A release folder can hold more than one build of
    a platform - a rerun that left the previous tag's installer behind, a manual
    run over a tag whose installer was built from a different ref - and picking
    the first by name signs whichever one sorts first, which is a signature over
    a file nobody chose.
    """
    prefix = f"Stamp-{version}-"
    found: list[Path] = []
    missing: list[str] = []
    artifacts: dict[str, dict] = {}
    for key, suffix in PLATFORMS.items():
        # Spelled out rather than shown as "Stamp-1.6.0-*-Setup.exe": the match
        # is a prefix and a suffix, and Stamp-1.6.0-Setup.exe - the name the
        # Windows job produces - has nothing in the middle for a star.
        described = f"a name starting with {prefix} and ending with {suffix}"
        candidates = sorted(assets.glob(f"*{suffix}"))
        matches = [path for path in candidates if path.name.startswith(prefix)]
        if len(matches) > 1:
            raise ManifestError(
                f"{key}: more than one file in {assets} has {described} "
                f"({', '.join(path.name for path in matches)}). "
                "Refusing to guess which one to sign."
            )
        if not matches:
            if candidates:
                raise ManifestError(
                    f"{key}: none of {', '.join(p.name for p in candidates)} has "
                    f"{described}. The tag and the version that was built "
                    "disagree, so the manifest would name a version nobody has."
                )
            print(f"  no artifact for {key} (looked for {described})", file=sys.stderr)
            missing.append(key)
            continue
        path = matches[0]
        found.append(path)
        artifacts[key] = {
            "name": path.name,
            "url": f"https://github.com/{repo}/releases/download/{tag}/{path.name}",
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        }
        print(f"  {key}: {path.name}")
    manifest = {
        "version": version,
        "notes_url": f"https://github.com/{repo}/releases/tag/{tag}",
        "artifacts": artifacts,
        # Both are here from the start so the shape never has to change: a
        # release withdrawn after the fact is added to "revoked", and
        # "minimum_supported" marks the version below which an update stops
        # being optional.  Stamp reads both already.
        "revoked": [],
        "minimum_supported": None,
    }
    if missing and not allow_missing:
        raise ManifestError(
            "No installer for " + ", ".join(missing) + ". A feed published over "
            "a failed build offers people a download that is not there, or the "
            "one a previous run left on the tag. Pass --allow-missing to "
            "publish anyway."
        )
    return manifest, found


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--assets", required=True, type=Path)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="publish a manifest that names fewer than every platform",
    )
    args = parser.parse_args()

    secret = os.environ.get("STAMP_RELEASE_KEY", "").strip()
    if not secret:
        print(
            "STAMP_RELEASE_KEY is not set. Refusing to write an unsigned "
            "manifest - Stamp would reject it, and a half-published feed is "
            "worse than none. See packaging/make_release_key.py.",
            file=sys.stderr,
        )
        return 2
    try:
        private = Ed25519PrivateKey.from_private_bytes(b64decode(secret, validate=True))
    except Exception as exc:  # noqa: BLE001 - report and stop, whatever it was
        print(f"STAMP_RELEASE_KEY is not a usable Ed25519 key: {exc}", file=sys.stderr)
        return 2

    try:
        manifest, found = build(
            args.version, args.tag, args.assets, args.repo, args.allow_missing
        )
    except ManifestError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not found:
        print("No artifacts found; nothing to publish.", file=sys.stderr)
        return 2

    args.output.mkdir(parents=True, exist_ok=True)
    # Written once and signed exactly as written: re-serialising before signing
    # is how a signature ends up covering bytes nobody will ever download.
    payload = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    (args.output / "latest.json").write_bytes(payload)
    (args.output / "latest.json.sig").write_bytes(private.sign(payload))
    (args.output / "SHA256SUMS").write_text(
        "".join(
            f"{entry['sha256']}  {entry['name']}\n"
            for entry in manifest["artifacts"].values()
        ),
        encoding="utf-8",
    )

    shipped = b64encode(private.public_key().public_bytes_raw()).decode()
    print(f"\nSigned manifest for {args.version} with {len(found)} artifact(s).")
    print(f"  signed with the key whose public half is {shipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
