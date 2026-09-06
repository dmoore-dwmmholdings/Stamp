"""The update check - see :mod:`stamp.update`.

These sign real manifests with a real key and serve them over ``file://``, so
what is under test is the verification Stamp actually performs rather than a
stand-in for it.  The security-relevant cases - a forged manifest, a tampered
installer, a downgrade, an unset key - each get a test, because those are the
ones where "it silently worked" is the bad outcome.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from base64 import b64encode

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from stamp import update


@pytest.fixture
def keypair():
    private = Ed25519PrivateKey.generate()
    public = b64encode(private.public_key().public_bytes_raw()).decode()
    return private, public


def _publish(tmp_path, private, manifest: dict, *, corrupt_signature=False) -> str:
    """Write a signed manifest and return a URL Stamp can fetch."""
    payload = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
    signature = private.sign(payload)
    if corrupt_signature:
        signature = bytes(64)
    (tmp_path / "latest.json").write_bytes(payload)
    (tmp_path / "latest.json.sig").write_bytes(signature)
    return (tmp_path / "latest.json").as_uri()


def _manifest(version: str, artifact_path=None, **extra) -> dict:
    artifacts = {}
    if artifact_path is not None:
        body = artifact_path.read_bytes()
        artifacts[update.platform_key()] = {
            "name": artifact_path.name,
            "url": artifact_path.as_uri(),
            "size": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
    return {
        "version": version,
        "notes_url": "https://example.invalid/notes",
        "artifacts": artifacts,
        **extra,
    }


class TestComparingVersions:
    """String comparison strands everybody on an old release at 1.9 -> 1.10."""

    def test_ten_is_newer_than_nine(self):
        assert update.parse_version("1.10.0") > update.parse_version("1.9.0")

    def test_a_leading_v_is_the_same_version(self):
        assert update.parse_version("v1.4.0") == update.parse_version("1.4.0")

    def test_a_release_candidate_is_older_than_the_release(self):
        assert update.parse_version("1.5.0-rc1") < update.parse_version("1.5.0")

    def test_a_short_version_is_padded(self):
        assert update.parse_version("2") == update.parse_version("2.0.0")

    def test_nonsense_does_not_raise(self):
        """A feed is remote input; it must not be able to crash the check."""
        assert update.parse_version("") == (0, 0, 0, 1)


class TestTrustingTheManifest:
    """Nothing is parsed, let alone run, before the signature checks out."""

    def test_a_signed_manifest_is_read(self, keypair, tmp_path):
        private, public = keypair
        url = _publish(tmp_path, private, _manifest("9.9.9"))
        with _key(public):
            release = update.check("1.0.0", url)
        assert release is not None
        assert release.version == "9.9.9"

    def test_a_forged_manifest_is_refused(self, keypair, tmp_path):
        """Someone else's key, or no key at all, must not get through."""
        private, _ = keypair
        other = b64encode(
            Ed25519PrivateKey.generate().public_key().public_bytes_raw()
        ).decode()
        url = _publish(tmp_path, private, _manifest("9.9.9"))
        with _key(other), pytest.raises(update.UpdateError, match="not signed"):
            update.check("1.0.0", url)

    def test_a_tampered_signature_is_refused(self, keypair, tmp_path):
        private, public = keypair
        url = _publish(tmp_path, private, _manifest("9.9.9"), corrupt_signature=True)
        with _key(public), pytest.raises(update.UpdateError):
            update.check("1.0.0", url)

    def test_editing_the_manifest_breaks_the_signature(self, keypair, tmp_path):
        """The signature is over the bytes, so one changed digit invalidates it."""
        private, public = keypair
        url = _publish(tmp_path, private, _manifest("1.5.0"))
        path = tmp_path / "latest.json"
        path.write_bytes(path.read_bytes().replace(b"1.5.0", b"9.9.9"))
        with _key(public), pytest.raises(update.UpdateError):
            update.check("1.0.0", url)

    def test_with_no_key_it_refuses_rather_than_trusting(self, keypair, tmp_path):
        """A check that trusts anything is worse than no check."""
        private, _ = keypair
        url = _publish(tmp_path, private, _manifest("9.9.9"))
        with _key(""):
            assert not update.is_configured()
            with pytest.raises(update.UpdateError, match="switched off"):
                update.check("1.0.0", url)

    def test_an_oversized_manifest_is_refused(self, keypair, tmp_path):
        private, public = keypair
        _publish(tmp_path, private, _manifest("9.9.9"))
        fat = tmp_path / "latest.json"
        fat.write_bytes(b"x" * (update.MAX_MANIFEST_BYTES + 1))
        with _key(public), pytest.raises(update.UpdateError, match="larger"):
            update.check("1.0.0", fat.as_uri())


class TestWhatItOffers:
    def test_the_same_version_is_not_an_update(self, keypair, tmp_path):
        private, public = keypair
        url = _publish(tmp_path, private, _manifest("1.4.0"))
        with _key(public):
            assert update.check("1.4.0", url) is None

    def test_an_older_release_is_not_offered(self, keypair, tmp_path):
        """A feed rolled back must not walk people down a version."""
        private, public = keypair
        url = _publish(tmp_path, private, _manifest("1.2.0"))
        with _key(public):
            assert update.check("1.4.0", url) is None

    def test_a_withdrawn_release_is_not_offered(self, keypair, tmp_path):
        """The kill switch: re-upload the manifest naming the bad version."""
        private, public = keypair
        url = _publish(
            tmp_path, private, _manifest("1.5.0", revoked=["1.5.0"])
        )
        with _key(public):
            assert update.check("1.4.0", url) is None

    def test_running_a_withdrawn_version_makes_it_urgent(self, keypair, tmp_path):
        private, public = keypair
        url = _publish(tmp_path, private, _manifest("1.6.0", revoked=["1.4.0"]))
        with _key(public):
            release = update.check("1.4.0", url)
        assert release is not None and release.urgent

    def test_below_the_minimum_is_urgent_too(self, keypair, tmp_path):
        private, public = keypair
        url = _publish(
            tmp_path, private, _manifest("1.6.0", minimum_supported="1.5.0")
        )
        with _key(public):
            release = update.check("1.4.0", url)
        assert release is not None and release.urgent

    def test_no_artifact_for_this_machine_still_reports_the_release(
        self, keypair, tmp_path
    ):
        """macOS has no installer path yet; the release page is still worth naming."""
        private, public = keypair
        url = _publish(tmp_path, private, _manifest("9.9.9"))
        with _key(public):
            release = update.check("1.4.0", url)
        assert release is not None
        assert release.artifact is None
        assert release.notes_url


class TestDownloading:
    @pytest.fixture
    def installer(self, tmp_path):
        path = tmp_path / "Stamp-9.9.9-Setup.exe"
        path.write_bytes(b"pretend this is an installer" * 500)
        return path

    def test_a_good_download_is_kept(self, keypair, tmp_path, installer):
        private, public = keypair
        url = _publish(tmp_path, private, _manifest("9.9.9", installer))
        with _key(public):
            release = update.check("1.4.0", url)
        got = update.download(release.artifact, tmp_path / "into")
        assert got.read_bytes() == installer.read_bytes()

    def test_a_tampered_installer_is_deleted_rather_than_run(
        self, keypair, tmp_path, installer
    ):
        """The signed manifest carries the hash; this is what it is for.

        Swapped for something the same length on purpose: a different size is
        caught by the cheaper check first, and then this would be passing
        without the hash ever being compared."""
        private, public = keypair
        url = _publish(tmp_path, private, _manifest("9.9.9", installer))
        with _key(public):
            release = update.check("1.4.0", url)
        installer.write_bytes(b"\x00" * installer.stat().st_size)

        with pytest.raises(update.UpdateError, match="signed manifest"):
            update.download(release.artifact, tmp_path / "into")
        assert not (tmp_path / "into" / installer.name).exists()

    def test_a_short_download_is_deleted_too(self, keypair, tmp_path, installer):
        private, public = keypair
        url = _publish(tmp_path, private, _manifest("9.9.9", installer))
        with _key(public):
            release = update.check("1.4.0", url)
        installer.write_bytes(b"cut short")

        with pytest.raises(update.UpdateError, match="size"):
            update.download(release.artifact, tmp_path / "into")
        assert not (tmp_path / "into" / installer.name).exists()

    def test_progress_is_reported(self, keypair, tmp_path, installer):
        private, public = keypair
        url = _publish(tmp_path, private, _manifest("9.9.9", installer))
        with _key(public):
            release = update.check("1.4.0", url)
        seen: list[tuple[int, int]] = []
        update.download(release.artifact, tmp_path / "into", progress=lambda *a: seen.append(a))
        assert seen and seen[-1][0] == installer.stat().st_size

    def test_cancelling_leaves_nothing_behind(self, keypair, tmp_path, installer):
        private, public = keypair
        url = _publish(tmp_path, private, _manifest("9.9.9", installer))
        with _key(public):
            release = update.check("1.4.0", url)
        with pytest.raises(update.UpdateError, match="cancelled"):
            update.download(
                release.artifact, tmp_path / "into", cancelled=lambda: True
            )
        assert not (tmp_path / "into" / installer.name).exists()


class TestWhereItWillAct:
    def test_it_only_fetches_over_https(self, tmp_path):
        """A manifest may name the URL to download; it may not name any scheme."""
        artifact = update.Artifact(
            name="x", url="http://example.invalid/x", size=1, sha256="00"
        )
        with pytest.raises(update.UpdateError, match="HTTPS"):
            update.download(artifact, tmp_path)

    def test_a_source_checkout_does_not_install_over_itself(self):
        """can_install is False unless this is a frozen build."""
        assert not update.can_install()

    def test_the_platform_key_names_this_machine(self):
        assert update.platform_key().split("-")[0] in ("windows", "macos", "linux")


@contextlib.contextmanager
def _key(value: str):
    """Point Stamp at a key for the length of a test, and put it back after.

    The built-in key is cleared too: an empty environment variable falls back to
    it, so without this the "no key at all" test would pass for the wrong reason
    the moment a real key is compiled in.
    """
    was = os.environ.get("STAMP_UPDATE_PUBLIC_KEY")
    built_in = update.RELEASE_PUBLIC_KEY
    os.environ["STAMP_UPDATE_PUBLIC_KEY"] = value
    update.RELEASE_PUBLIC_KEY = ""
    try:
        yield
    finally:
        update.RELEASE_PUBLIC_KEY = built_in
        if was is None:
            os.environ.pop("STAMP_UPDATE_PUBLIC_KEY", None)
        else:
            os.environ["STAMP_UPDATE_PUBLIC_KEY"] = was
