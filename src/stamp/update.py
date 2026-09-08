"""Finding out that a newer Stamp exists, and fetching it safely.

An updater downloads an executable and runs it.  That is the exact shape of a
supply-chain attack, so the interesting part of this module is not the fetching -
it is what has to be true before anything is allowed to run.

Two signatures' worth of trust, in order:

1. A **manifest**, ``latest.json``, published as an asset on the newest release,
   listing the version and one artifact per platform with its SHA-256.  It is
   signed with an Ed25519 key that lives only in a GitHub Actions secret, and the
   matching public key is compiled into this file.  A manifest whose signature
   does not verify is not read at all - not parsed, not looked at.  TLS alone is
   not enough: it says the bytes came from GitHub, not that they are the bytes we
   published.
2. The **artifact's own SHA-256**, checked against the signed manifest after the
   download and again immediately before anything is executed.  A file that does
   not match is deleted rather than kept.  Twice because the two moments can be
   an afternoon apart - "install when I quit" is a real choice people make - and
   what matters is the file as it is when it runs.

Until a key is generated and pasted into :data:`RELEASE_PUBLIC_KEY`, the whole
feature reports itself unconfigured and does nothing.  A version check that
trusts whatever it is handed is worse than no version check, so it refuses to
run rather than degrading quietly.  See ``packaging/make_release_key.py``.

Nothing here imports Qt: this is the part that can be tested without a window.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from stamp import __version__

#: Where the signed manifest lives.  ``releases/latest/download/`` is a stable
#: redirect to the newest non-prerelease release's asset of that name, which
#: means no API call, no token on a public repository, and no rate limit worth
#: worrying about.  Re-uploading it to the current release is also the kill
#: switch: a release that turns out to be bad is withdrawn by pointing the
#: manifest back at the previous one.
FEED_URL = (
    "https://github.com/dmoore-dwmmholdings/Stamp"
    "/releases/latest/download/latest.json"
)

#: The Ed25519 public key that release manifests are signed with: standard
#: base64 of the 32 raw bytes.  Generate the pair with
#: ``uv run python packaging/make_release_key.py``, paste the public half here,
#: and put the private half in the STAMP_RELEASE_KEY repository secret.
#: Empty means updates are switched off - see the module docstring.
RELEASE_PUBLIC_KEY = ""

#: Long enough for a slow connection, short enough that a start is not held up.
TIMEOUT_S = 20

#: A manifest is a few hundred bytes.  Anything remotely this large is not one,
#: and reading it into memory unbounded is how a feed becomes a denial of service.
MAX_MANIFEST_BYTES = 64 * 1024

#: Read size while downloading.  Big enough not to thrash, small enough that
#: cancelling feels immediate.
CHUNK = 256 * 1024

#: The most that is read when the manifest names no size.  An installer is tens
#: of megabytes; without a bound, a feed that streams forever fills the disk.
MAX_ARTIFACT_BYTES = 1024 * 1024 * 1024

USER_AGENT = f"Stamp/{__version__}"


class UpdateError(RuntimeError):
    """Anything that stops an update being offered or trusted."""


# --------------------------------------------------------------------------
# Configuration.  Both are overridable so that the tests can stand up a feed of
# their own and exercise the real verification rather than a mock of it - but
# only in a source checkout.  In a shipped build an environment variable is not
# a trusted thing: anything that can set one could otherwise point Stamp at a
# feed of its own, signed with a key of its own, and the two signatures this
# module exists for would both check out.
# --------------------------------------------------------------------------

def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def feed_url() -> str:
    if _frozen():
        return FEED_URL
    return os.environ.get("STAMP_UPDATE_FEED") or FEED_URL


def public_key() -> str:
    if _frozen():
        return RELEASE_PUBLIC_KEY
    return os.environ.get("STAMP_UPDATE_PUBLIC_KEY") or RELEASE_PUBLIC_KEY


def is_configured() -> bool:
    """Whether there is a key to check signatures against."""
    return bool(public_key().strip())


def can_install() -> bool:
    """Whether this build can apply an update itself.

    Windows only, and only from a frozen build: the installer replaces an
    installed application, and running it over a source checkout would do
    something nobody asked for.  Everywhere else the offer is to open the
    release page - on macOS that stays true until the app is notarised, because
    Gatekeeper quarantines a downloaded bundle whatever the updater thinks.
    """
    return sys.platform.startswith("win") and bool(getattr(sys, "frozen", False))


# --------------------------------------------------------------------------
# Versions and platforms
# --------------------------------------------------------------------------

def parse_version(text: str) -> tuple[int, int, int, int, str, int]:
    """A comparable version.

    String comparison gets 1.10.0 wrong against 1.9.0, which is the classic way
    to strand everyone on an old release, so the three numbers come out as
    numbers.  What follows them is a pre-release tail, and it is kept whole
    rather than flattened to a yes-or-no: the fourth field says whether this is
    a final release, and the two after it order the pre-releases of one triple
    among themselves.  Flattening made every pre-release of 1.6.0 equal, so
    1.6.0-rc2 was not an update from 1.6.0-rc1 and a withdrawn rc1 revoked rc2
    along with it.

    A letter anywhere means the same thing as the dash does, thus 1.6.0rc1 and
    1.6.0.rc1 are the pre-release that 1.6.0-rc1 is.  "+build5" is metadata
    about how a release was built, not a different release, thus it is dropped.
    """
    cleaned = str(text).strip().lstrip("vV")
    cleaned = cleaned.partition("+")[0]
    head, _, tail = cleaned.partition("-")
    numbers: list[int] = []
    for index, piece in enumerate(head.split(".")):
        digits = ""
        for ch in piece:
            if not ch.isdigit():
                break
            digits += ch
        if index < 3:
            numbers.append(int(digits) if digits else 0)
        rest = piece[len(digits):]
        # The first letters in the head are where the version stops and its
        # pre-release begins, whether they are stuck to a number or a piece of
        # their own.  A tail after the dash is only reached when there are none.
        if rest and not tail:
            tail = rest
    while len(numbers) < 3:
        numbers.append(0)

    if not tail:
        return (numbers[0], numbers[1], numbers[2], 1, "", 0)
    # alpha, beta, rc happen to sort that way as words, which is the order they
    # are released in.  The number after the tag is a number, so rc10 follows
    # rc9 rather than rc1.
    tag = ""
    for ch in tail:
        if not ch.isalpha():
            break
        tag += ch
    digits = ""
    for ch in tail[len(tag):]:
        if ch.isdigit():
            digits += ch
        elif digits or ch not in ".-_":
            break
    return (numbers[0], numbers[1], numbers[2], 0, tag.lower(), int(digits or 0))


def platform_key() -> str:
    """The manifest key for the machine this is running on."""
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x86_64"
    if sys.platform.startswith("win"):
        return f"windows-{arch}"
    if sys.platform == "darwin":
        return f"macos-{arch}"
    return f"linux-{arch}"


# --------------------------------------------------------------------------
# The manifest
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Artifact:
    """One installer, and the hash it has to have."""

    name: str
    url: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Release:
    """A newer Stamp than this one."""

    version: str
    notes_url: str
    artifact: Artifact | None
    #: The running version is withdrawn or too old to be supported.
    urgent: bool = False


def verify(payload: bytes, signature: bytes, key: str | None = None) -> None:
    """Raise unless *signature* is a real Ed25519 signature over *payload*."""
    encoded = (key if key is not None else public_key()).strip()
    if not encoded:
        raise UpdateError("Stamp has no release key, so updates are switched off.")
    # Imported here rather than at the top so that the command line and the
    # tests still work on an install where cryptography is missing: the error
    # then says so, instead of the whole module failing to import.
    try:
        from base64 import b64decode

        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:  # pragma: no cover - a broken install
        raise UpdateError(f"Stamp cannot check the release signature: {exc}") from exc
    try:
        raw = b64decode(encoded, validate=True)
    except Exception as exc:
        raise UpdateError("Stamp's release key is not readable.") from exc
    if len(raw) != 32:
        raise UpdateError("Stamp's release key is the wrong length.")
    try:
        Ed25519PublicKey.from_public_bytes(raw).verify(signature, payload)
    except InvalidSignature as exc:
        raise UpdateError(
            "The update manifest is not signed by Stamp. Nothing was downloaded."
        ) from exc


def read_manifest(payload: bytes, signature: bytes, key: str | None = None) -> dict:
    """Verify first, parse second.

    In that order on purpose.  Parsing attacker-controlled JSON before checking
    who wrote it is doing work on their behalf.
    """
    verify(payload, signature, key)
    try:
        manifest = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("The update manifest could not be read.") from exc
    if not isinstance(manifest, dict) or "version" not in manifest:
        raise UpdateError("The update manifest is not in the expected shape.")
    return manifest


def _open(url: str, timeout: float = TIMEOUT_S):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    # file:// is how the tests serve a real signed feed.  A shipped build has no
    # business reading one off the disk, so there it is refused with the rest.
    allowed = ("https://",) if _frozen() else ("https://", "file://")
    if not url.startswith(allowed):
        raise UpdateError("Stamp only fetches updates over HTTPS.")
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 - checked above


def fetch(url: str, limit: int = MAX_MANIFEST_BYTES, timeout: float = TIMEOUT_S) -> bytes:
    """Read a small file, refusing one that is not small."""
    try:
        with _open(url, timeout) as response:
            payload = response.read(limit + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise UpdateError(f"Stamp could not reach the update feed: {exc}") from exc
    if len(payload) > limit:
        raise UpdateError("The update manifest is larger than one can be.")
    return payload


def check(current: str = __version__, url: str | None = None) -> Release | None:
    """The newer release, or None when this one is current.

    Raises :class:`UpdateError` rather than returning None when something went
    wrong, so "there is nothing new" and "the check did not happen" stay
    different answers - only one of them is worth telling the user about.
    """
    if not is_configured():
        raise UpdateError("Stamp has no release key, so updates are switched off.")
    where = url or feed_url()
    manifest = read_manifest(fetch(where), fetch(where + ".sig", limit=1024))

    version = str(manifest["version"])
    running = parse_version(current)
    # Compared as versions rather than as strings: a manifest that withdraws
    # "v1.6.0" is talking about the build that calls itself "1.6.0".
    revoked = {parse_version(v) for v in manifest.get("revoked", [])}
    minimum = manifest.get("minimum_supported")
    urgent = running in revoked or (
        bool(minimum) and running < parse_version(str(minimum))
    )
    if parse_version(version) <= running and not urgent:
        return None
    if parse_version(version) in revoked:
        # The newest release was withdrawn after it was published.
        return None

    entry = (manifest.get("artifacts") or {}).get(platform_key())
    artifact = None
    if isinstance(entry, dict) and entry.get("url") and entry.get("sha256"):
        artifact = Artifact(
            name=str(entry.get("name") or "Stamp-setup"),
            url=str(entry["url"]),
            size=int(entry.get("size") or 0),
            sha256=str(entry["sha256"]).lower(),
        )
    return Release(
        version=version,
        notes_url=str(manifest.get("notes_url") or ""),
        artifact=artifact,
        urgent=bool(urgent),
    )


# --------------------------------------------------------------------------
# Fetching, and running what was fetched
# --------------------------------------------------------------------------

#: What each downloaded file was verified to hash to, by absolute path.  The
#: install is a separate decision from the download - "install when I quit" can
#: be hours later - and this is what lets that later step check the file again
#: rather than trust that nothing touched it in between.
_verified: dict[str, str] = {}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


#: The prefix every download's directory is made with, and what a later
#: download recognises the earlier ones by.
TEMP_PREFIX = "stamp-update-"


def _forget_missing() -> None:
    """Drop what was verified about files that are no longer on disk."""
    for key in [name for name in _verified if not Path(name).exists()]:
        _verified.pop(key, None)


def _sweep(parent: Path) -> None:
    """Remove the directories earlier downloads left behind.

    An installer is around a hundred megabytes, and every offer that is skipped,
    put off, or quit out of used to leave one where it fell.  Errors are
    ignored on purpose: on Windows a directory an installer is still running
    out of cannot be removed, and failing to tidy up is not a reason to refuse
    the download that was actually asked for.
    """
    try:
        stale = [entry for entry in parent.glob(TEMP_PREFIX + "*") if entry.is_dir()]
    except OSError:
        stale = []
    for entry in stale:
        shutil.rmtree(entry, ignore_errors=True)
    _forget_missing()


def discard(path: Path) -> None:
    """Throw away a downloaded installer, directory and all.

    The download and the install are separate decisions, so there is a state
    where a verified installer sits in the temporary folder and nobody is going
    to run it - the user skipped the version, or closed the window without
    installing.  The UI says so here rather than leaving it for the next
    download to sweep.
    """
    target = Path(path)
    keys = {str(target)}
    try:
        keys.add(str(target.resolve()))
    except OSError:
        pass
    for key in keys:
        _verified.pop(key, None)
    directory = target.parent
    if directory.name.startswith(TEMP_PREFIX):
        shutil.rmtree(directory, ignore_errors=True)
    else:
        # Somewhere Stamp did not make: take the file and leave the folder.
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
    _forget_missing()


def download(
    artifact: Artifact,
    into: Path | None = None,
    progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Path:
    """Fetch *artifact*, and delete it unless its hash is the promised one.

    The file lands in a directory made fresh for it, never at a name anybody
    could work out in advance.  A verified installer sitting at
    ``%TEMP%\\stamp-update\\Stamp-Setup.exe`` until the user quits is an
    invitation to replace it between the check and the run; a directory created
    with the permissions :func:`tempfile.mkdtemp` gives it is not.
    """
    if into is not None:
        into.mkdir(parents=True, exist_ok=True)
    # Before, not after: what the last download left is dead weight the moment a
    # new one starts, and this is the only moment Stamp is certainly running.
    _sweep(into if into is not None else Path(tempfile.gettempdir()))
    directory = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=into))
    target = directory / Path(artifact.name).name
    digest = hashlib.sha256()
    # What the manifest promised, or the ceiling for an artifact of no stated
    # size: reading to EOF is reading as much as the server wants to send.
    limit = artifact.size or MAX_ARTIFACT_BYTES
    done = 0
    try:
        with _open(artifact.url) as response, target.open("wb") as handle:
            total = artifact.size or int(response.headers.get("Content-Length") or 0)
            while True:
                if cancelled is not None and cancelled():
                    raise UpdateError("cancelled")
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                handle.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if done > limit:
                    raise UpdateError(
                        "The download is longer than the manifest promised."
                    )
                if progress is not None:
                    progress(done, total)
    except UpdateError:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    except (urllib.error.URLError, OSError, ValueError) as exc:
        shutil.rmtree(directory, ignore_errors=True)
        raise UpdateError(f"The download did not finish: {exc}") from exc

    if artifact.size and done != artifact.size:
        shutil.rmtree(directory, ignore_errors=True)
        raise UpdateError("The download is not the size the manifest promised.")
    if digest.hexdigest() != artifact.sha256:
        shutil.rmtree(directory, ignore_errors=True)
        raise UpdateError(
            "The download does not match the signed manifest, so it was deleted."
        )
    _verified[str(target.resolve())] = artifact.sha256
    return target


def install(path: Path, relaunch: bool = True, sha256: str | None = None) -> None:
    """Start the installer and return, so the caller can quit.

    Stamp has to be gone before its own files can be replaced.  The installer is
    started detached, the caller closes the window, and ``CloseApplications`` in
    the Inno script covers the moment in between.  ``/RELAUNCH=1`` is Stamp's
    own switch, read by a Check in the script, because Inno's own "start the
    application" entry is deliberately skipped in a silent install.

    *sha256* is what the file has to hash to, and it is re-checked here rather
    than taken on trust from the download: the user may have chosen to install
    on the way out, and a verified file that then sits on disk for an afternoon
    has only been verified as it was that morning.  Left out, the hash
    :func:`download` recorded for that path is used, and a path with no recorded
    hash is refused rather than run.
    """
    if not sys.platform.startswith("win"):
        raise UpdateError("Stamp can only install an update on Windows.")
    if not path.exists():
        raise UpdateError("The downloaded installer is no longer there.")
    expected = (sha256 or _verified.get(str(path.resolve())) or "").lower()
    if not expected:
        raise UpdateError(
            "Stamp does not know what this installer should contain, so it will "
            "not run it."
        )
    # Checked again here rather than trusting the check the download made.  The
    # two can be hours apart, and it is the file as it is now that gets run.
    if _sha256_file(path) != expected:
        path.unlink(missing_ok=True)
        raise UpdateError(
            "The installer changed after it was downloaded, so it was deleted "
            "instead of run."
        )
    flags = ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]
    if relaunch:
        flags.append("/RELAUNCH=1")
    creation = 0
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
        creation |= getattr(subprocess, name, 0)
    try:
        subprocess.Popen(  # noqa: S603 - a file this process just hash-checked
            [str(path), *flags], close_fds=True, creationflags=creation
        )
    except OSError as exc:
        raise UpdateError(f"The installer would not start: {exc}") from exc


__all__ = [
    "Artifact",
    "FEED_URL",
    "RELEASE_PUBLIC_KEY",
    "Release",
    "MAX_ARTIFACT_BYTES",
    "UpdateError",
    "can_install",
    "check",
    "discard",
    "download",
    "feed_url",
    "install",
    "is_configured",
    "parse_version",
    "platform_key",
    "public_key",
    "read_manifest",
    "verify",
]
