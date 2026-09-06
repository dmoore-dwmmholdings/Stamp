"""Build the installer for whatever platform you are sitting at.

    uv run python packaging/build_installer.py

That is the whole thing: icons, the frozen application, and the installer a
tester can actually double-click.  It writes the same file names the release
workflow attaches to a tag, so a local build and a released build are
interchangeable.

    build/Stamp-<version>-Setup.exe              Windows
    build/Stamp-<version>-macos-<arch>.dmg       macOS, drag to Applications
    build/Stamp-<version>-macos-<arch>.pkg       macOS, Installer does the copy
    build/Stamp-<version>-linux-<arch>.tar.gz    anywhere else

Options:

    --app-only     Freeze the application, skip the installer around it.
    --skip-icons   Reuse packaging/assets as it stands.

The Windows installer needs Inno Setup 6; everything else ships with the OS.
Expect several minutes and a couple of gigabytes of scratch - OpenCascade is
large and PyInstaller copies all of it.
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
BUILD = ROOT / "build"
DIST = BUILD / "dist"
WORK = BUILD / "work"

#: Where Inno Setup puts itself.  Checked in order, after PATH.
ISCC_GUESSES = (
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
)


def version() -> str:
    """The one version number, read where everything else reads it."""
    text = (ROOT / "src" / "stamp" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__ = "([^"]+)"', text)
    if not match:
        raise SystemExit("src/stamp/__init__.py has no __version__.")
    return match.group(1)


def check_versions_agree(expected: str) -> None:
    """Fail before the long part, not after it.

    pyproject.toml and stamp.iss repeat the version, and the installer takes its
    name from it.  A stale number here produces an installer that claims to be a
    release it is not, which is worth catching in the first second of the build.
    """
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    if f'version = "{expected}"' not in pyproject:
        raise SystemExit(
            f"pyproject.toml does not say version = \"{expected}\".  "
            "Update it to match src/stamp/__init__.py."
        )
    iss = (PACKAGING / "stamp.iss").read_text(encoding="utf-8")
    match = re.search(r'#define AppVersion "([^"]+)"', iss)
    if not match or match.group(1) != expected:
        found = match.group(1) if match else "nothing"
        raise SystemExit(
            f"packaging/stamp.iss defines AppVersion {found}, expected {expected}."
        )


def run(command: list[str], **kwargs) -> None:
    print("+ " + " ".join(str(part) for part in command), flush=True)
    subprocess.run(command, check=True, cwd=ROOT, **kwargs)


def arch_tag() -> str:
    """The architecture as the release file names spell it."""
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        return "x86_64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    return machine


def freeze(skip_icons: bool) -> None:
    if not skip_icons:
        run([sys.executable, str(PACKAGING / "make_icons.py")])
    # -m PyInstaller rather than the console script, so the build always uses
    # the interpreter this script is running under.
    run([
        sys.executable, "-m", "PyInstaller",
        str(PACKAGING / "stamp.spec"),
        "--noconfirm",
        "--distpath", str(DIST),
        "--workpath", str(WORK),
    ])


def build_macos(tag: str) -> list[Path]:
    app = DIST / "Stamp.app"
    if not app.is_dir():
        raise SystemExit(f"{app} is missing; the freeze step did not finish.")

    staging = BUILD / "dmg"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    # ditto, not copytree: it preserves the bundle's symlinks and permissions.
    run(["ditto", str(app), str(staging / "Stamp.app")])
    os.symlink("/Applications", staging / "Applications")

    dmg = BUILD / f"Stamp-{tag}.dmg"
    run([
        "hdiutil", "create", "-volname", "Stamp",
        "-srcfolder", str(staging), "-ov", "-format", "UDZO", str(dmg),
    ])

    pkg = BUILD / f"Stamp-{tag}.pkg"
    run([
        "pkgbuild", "--identifier", "com.stamp.app",
        "--version", tag.split("-")[0],
        "--component", str(app),
        "--install-location", "/Applications", str(pkg),
    ])
    return [dmg, pkg]


def build_windows(release: str) -> list[Path]:
    iscc = shutil.which("iscc") or shutil.which("ISCC")
    if iscc is None:
        iscc = next((path for path in ISCC_GUESSES if Path(path).exists()), None)
    if iscc is None:
        raise SystemExit(
            "Inno Setup 6 was not found.  Install it from https://jrsoftware.org/isdl.php "
            "(or `winget install JRSoftware.InnoSetup`), then run this again.  "
            "Use --app-only to stop after build/dist/Stamp."
        )
    run([iscc, str(PACKAGING / "stamp.iss")])
    # stamp.iss writes to packaging/dist; put it beside the other installers.
    produced = PACKAGING / "dist" / f"Stamp-{release}-Setup.exe"
    if not produced.exists():
        raise SystemExit(f"Inno Setup did not write {produced}.")
    target = BUILD / produced.name
    shutil.move(str(produced), target)
    return [target]


def build_linux(tag: str) -> list[Path]:
    folder = DIST / "Stamp"
    if not folder.is_dir():
        raise SystemExit(f"{folder} is missing; the freeze step did not finish.")
    archive = BUILD / f"Stamp-{tag}.tar.gz"
    print(f"+ tar czf {archive}", flush=True)
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(folder, arcname="Stamp")
    return [archive]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--app-only", action="store_true",
                        help="freeze the application but do not wrap an installer around it")
    parser.add_argument("--skip-icons", action="store_true",
                        help="reuse packaging/assets instead of regenerating the icons")
    args = parser.parse_args()

    release = version()
    check_versions_agree(release)
    tag = f"{release}-{'macos' if sys.platform == 'darwin' else 'linux'}-{arch_tag()}"

    print(f"Building Stamp {release} for {sys.platform} {arch_tag()}", flush=True)
    BUILD.mkdir(exist_ok=True)
    freeze(args.skip_icons)

    if args.app_only:
        print(f"\nApplication written to {DIST}")
        return 0

    if sys.platform == "darwin":
        products = build_macos(tag)
    elif sys.platform == "win32":
        products = build_windows(release)
    else:
        products = build_linux(tag)

    print("\nDone:")
    for product in products:
        size = product.stat().st_size / (1024 * 1024)
        print(f"  {product.relative_to(ROOT)}  ({size:.0f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
