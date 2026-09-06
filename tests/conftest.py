from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session", autouse=True)
def ensure_fixtures():
    if not (FIXTURES / "bracket.step").exists():
        import make_fixtures  # noqa: F401 - generated on demand

        make_fixtures.main()
    return FIXTURES


@pytest.fixture(scope="session", autouse=True)
def scratch_settings(tmp_path_factory):
    """Give the window its own settings rather than the developer's.

    ``QSettings("Stamp", "Stamp")`` is the real registry key, so without this a
    run both reads whatever preferences the machine happens to hold - a ribbon
    left on large buttons fails three tests that ask for the default - and
    writes the test's choices back into them.  Pointing the user scope at a
    temporary directory keeps the run to itself.
    """
    from PySide6.QtCore import QSettings

    from stamp.ui import main_window

    path = tmp_path_factory.mktemp("settings") / "stamp.ini"
    main_window.user_settings = lambda: QSettings(
        str(path), QSettings.Format.IniFormat
    )
    return path


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture(scope="session")
def bracket_step():
    from stamp.io.part_import import import_part

    return import_part(FIXTURES / "bracket.step").part


@pytest.fixture(scope="session")
def bracket_stl():
    from stamp.io.part_import import import_part

    return import_part(FIXTURES / "bracket.stl").part


@pytest.fixture(scope="session")
def logo_profile():
    from stamp.io.profile_import import import_profile

    return import_profile(FIXTURES / "logo.svg").profile


@pytest.fixture
def top_plane():
    """The sketch plane on the bracket's top face, at z = 8."""
    from stamp.core.document import Plane

    return Plane(origin=(30.0, 20.0, 8.0), normal=(0.0, 0.0, 1.0), u_axis=(1.0, 0.0, 0.0))
