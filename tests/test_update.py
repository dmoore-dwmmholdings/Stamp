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
import pathlib
import sys
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

    def test_a_suffix_with_no_dash_is_still_a_pre_release(self):
        """1.5.0rc1 read "rc1" as the patch number and sorted above 1.5.0."""
        assert update.parse_version("1.5.0rc1") < update.parse_version("1.5.0")
        assert update.parse_version("1.5.0rc1") > update.parse_version("1.4.9")

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

    def test_a_withdrawal_written_with_a_v_still_counts(self, keypair, tmp_path):
        """The tag is v1.4.0 and the build calls itself 1.4.0.

        Compared as strings, a manifest that withdraws the tag name withdrew
        nothing at all and the person stayed on the bad build.
        """
        private, public = keypair
        url = _publish(tmp_path, private, _manifest("1.6.0", revoked=["v1.4.0"]))
        with _key(public):
            release = update.check("1.4.0", url)
        assert release is not None and release.urgent

    def test_a_newest_release_withdrawn_by_tag_is_not_offered(self, keypair, tmp_path):
        private, public = keypair
        url = _publish(tmp_path, private, _manifest("1.5.0", revoked=["v1.5.0"]))
        with _key(public):
            assert update.check("1.4.0", url) is None

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

    def test_a_download_longer_than_the_manifest_is_stopped_in_the_loop(
        self, keypair, tmp_path, installer
    ):
        """Reading to EOF is reading as much as the server wants to send."""
        private, public = keypair
        manifest = _manifest("9.9.9", installer)
        entry = manifest["artifacts"][update.platform_key()]
        entry["size"] = 64
        url = _publish(tmp_path, private, manifest)
        with _key(public):
            release = update.check("1.4.0", url)

        with pytest.raises(update.UpdateError, match="longer than"):
            update.download(release.artifact, tmp_path / "into")

    def test_an_artifact_of_no_stated_size_is_capped(
        self, keypair, tmp_path, installer, monkeypatch
    ):
        """A size of zero left the loop with nothing to stop it at all."""
        private, public = keypair
        manifest = _manifest("9.9.9", installer)
        entry = manifest["artifacts"][update.platform_key()]
        entry["size"] = 0
        url = _publish(tmp_path, private, manifest)
        with _key(public):
            release = update.check("1.4.0", url)
        monkeypatch.setattr(update, "MAX_ARTIFACT_BYTES", 128)

        with pytest.raises(update.UpdateError, match="longer than"):
            update.download(release.artifact, tmp_path / "into")

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


class TestInstalling:
    """What is run has to be what was checked, at the moment it is run."""

    @pytest.fixture
    def installer(self, tmp_path):
        path = tmp_path / "Stamp-9.9.9-Setup.exe"
        path.write_bytes(b"pretend this is an installer" * 500)
        return path

    def _downloaded(self, keypair, tmp_path, installer):
        private, public = keypair
        url = _publish(tmp_path, private, _manifest("9.9.9", installer))
        with _key(public):
            release = update.check("1.4.0", url)
        return update.download(release.artifact, tmp_path / "into")

    def test_the_download_lands_somewhere_nobody_can_name_in_advance(
        self, keypair, tmp_path, installer
    ):
        """A fixed path is a file anything on the machine can swap out.

        "Install when I quit" leaves the verified installer on disk for as long
        as the user keeps working, and %TEMP%\\stamp-update\\<name> is a path
        anyone could write to before that.
        """
        got = self._downloaded(keypair, tmp_path, installer)
        assert got != tmp_path / "into" / installer.name
        assert got.parent.name.startswith("stamp-update-")

    def test_an_installer_changed_after_the_check_is_not_run(
        self, keypair, tmp_path, installer, monkeypatch
    ):
        got = self._downloaded(keypair, tmp_path, installer)
        started: list[list[str]] = []
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(
            update.subprocess, "Popen", lambda cmd, **kw: started.append(cmd)
        )
        got.write_bytes(b"\x00" * got.stat().st_size)

        with pytest.raises(update.UpdateError, match="changed after"):
            update.install(got)
        assert started == []
        assert not got.exists()

    def test_the_installer_that_was_checked_is_run(
        self, keypair, tmp_path, installer, monkeypatch
    ):
        got = self._downloaded(keypair, tmp_path, installer)
        started: list[list[str]] = []
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(
            update.subprocess, "Popen", lambda cmd, **kw: started.append(cmd)
        )
        update.install(got)
        assert started and started[0][0] == str(got)

    def test_a_file_stamp_never_checked_is_refused(self, tmp_path, monkeypatch):
        stray = tmp_path / "Stamp-Setup.exe"
        stray.write_bytes(b"anything at all")
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(update, "_verified", {})
        with pytest.raises(update.UpdateError, match="does not know"):
            update.install(stray)


class TestWhereItWillAct:
    def test_it_only_fetches_over_https(self, tmp_path):
        """A manifest may name the URL to download; it may not name any scheme."""
        artifact = update.Artifact(
            name="x", url="http://example.invalid/x", size=1, sha256="00"
        )
        with pytest.raises(update.UpdateError, match="HTTPS"):
            update.download(artifact, tmp_path)

    def test_a_shipped_build_reads_no_feed_but_its_own(self, monkeypatch):
        """An environment variable is not a trusted thing in a frozen build.

        Anything that can set one could otherwise point Stamp at a feed of its
        own signed with a key of its own, and both signature checks this module
        exists for would pass.
        """
        monkeypatch.setenv("STAMP_UPDATE_FEED", "https://example.invalid/feed.json")
        monkeypatch.setenv("STAMP_UPDATE_PUBLIC_KEY", "A" * 44)
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        assert update.feed_url() == update.FEED_URL
        assert update.public_key() == update.RELEASE_PUBLIC_KEY

    def test_a_source_checkout_still_takes_the_override(self, monkeypatch):
        """Which is what lets these tests serve a real signed feed."""
        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.setenv("STAMP_UPDATE_FEED", "https://example.invalid/feed.json")
        assert update.feed_url() == "https://example.invalid/feed.json"

    def test_a_shipped_build_does_not_read_a_feed_off_the_disk(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        artifact = update.Artifact(
            name="x", url=(tmp_path / "x").as_uri(), size=1, sha256="00"
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


def _make_manifest_module():
    """packaging/ is a directory of scripts, not an installed package."""
    import importlib.util

    here = pathlib.Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "stamp_make_manifest", here / "packaging" / "make_manifest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTheManifestTheWorkflowWrites:
    """What gets signed has to be the build the release is called after."""

    @pytest.fixture
    def make_manifest(self):
        return _make_manifest_module()

    def test_the_installer_carrying_the_version_is_the_one_signed(
        self, make_manifest, tmp_path
    ):
        """Sorted-first signed whichever name came first alphabetically.

        A tag rebuilt over a release that already had an installer leaves two
        on the same release, and the manifest then said 1.7.0 over the bytes of
        1.6.0.
        """
        (tmp_path / "Stamp-1.6.0-Setup.exe").write_bytes(b"old")
        (tmp_path / "Stamp-1.7.0-Setup.exe").write_bytes(b"new")
        (tmp_path / "Stamp-1.7.0-macos-arm64.dmg").write_bytes(b"arm")
        (tmp_path / "Stamp-1.7.0-macos-x86_64.dmg").write_bytes(b"intel")

        manifest, found = make_manifest.build(
            "1.7.0", "v1.7.0", tmp_path, "owner/Stamp"
        )
        assert manifest["artifacts"]["windows-x86_64"]["name"] == "Stamp-1.7.0-Setup.exe"
        assert len(found) == 3

    def test_an_installer_of_another_version_is_not_signed_as_this_one(
        self, make_manifest, tmp_path
    ):
        """A dispatch that built main and named an older tag ends here."""
        (tmp_path / "Stamp-1.6.0-Setup.exe").write_bytes(b"old")

        with pytest.raises(make_manifest.ManifestError, match="disagree"):
            make_manifest.build("1.7.0", "v1.7.0", tmp_path, "owner/Stamp")

    def test_a_missing_platform_stops_the_feed(self, make_manifest, tmp_path):
        """A feed published over a failed job offers a download nobody built."""
        (tmp_path / "Stamp-1.7.0-Setup.exe").write_bytes(b"win")

        with pytest.raises(make_manifest.ManifestError, match="No installer for"):
            make_manifest.build("1.7.0", "v1.7.0", tmp_path, "owner/Stamp")

        manifest, found = make_manifest.build(
            "1.7.0", "v1.7.0", tmp_path, "owner/Stamp", allow_missing=True
        )
        assert set(manifest["artifacts"]) == {"windows-x86_64"}
        assert len(found) == 1
