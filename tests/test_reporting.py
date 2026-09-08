"""Crash reports and bug reports - the email a user sends back."""

from __future__ import annotations

import sys

import pytest

pytest.importorskip("PySide6")

from stamp import diagnostics, reporting  # noqa: E402


@pytest.fixture
def started(tmp_path, monkeypatch):
    """A diagnostics log that lives in a temporary directory."""
    import importlib
    import logging

    monkeypatch.setattr(diagnostics, "log_dir", lambda: tmp_path)
    monkeypatch.setattr(diagnostics, "_started", False)
    monkeypatch.setattr(diagnostics, "_crashed_before", False)
    monkeypatch.setattr(diagnostics, "_log", logging.getLogger("stamp.report.test"))
    importlib.reload  # noqa: B018 - kept for clarity, nothing to reload
    diagnostics.start()
    return tmp_path


class TestReportText:
    def test_the_subject_names_the_kind_and_the_summary(self, started):
        crash = reporting.Report(kind="crash", summary="stopped on a fillet")
        bug = reporting.Report(kind="bug", summary="text is upside down")
        assert crash.subject.startswith("Stamp crash report")
        assert "stopped on a fillet" in crash.subject
        assert bug.subject.startswith("Stamp bug report")

    def test_a_report_with_no_summary_still_has_a_subject(self, started):
        assert reporting.Report(kind="bug").subject == "Stamp bug report"

    def test_the_report_carries_the_environment(self, started):
        text = reporting.build_text(reporting.Report(kind="bug"))
        assert "Stamp version" in text
        assert "Python" in text
        assert "System" in text

    def test_the_report_carries_what_the_user_typed(self, started):
        report = reporting.Report(
            kind="bug",
            detail="the fillet did nothing",
            expected="a rounded edge",
            steps="1. open a part",
            part="bracket.step",
        )
        text = reporting.build_text(report)
        for expected in ("the fillet did nothing", "a rounded edge",
                         "1. open a part", "bracket.step"):
            assert expected in text

    def test_the_report_carries_the_log(self, started):
        diagnostics.breadcrumb("modifier: kind=FILLET edges=3")
        text = reporting.build_text(reporting.Report(kind="crash"))
        assert "modifier: kind=FILLET edges=3" in text


class TestMailtoLink:
    def test_the_link_is_addressed_to_support(self, started):
        url = reporting.mailto_url(reporting.Report(kind="bug"), None)
        assert url.startswith(f"mailto:{reporting.SUPPORT_EMAIL}?")

    def test_a_long_report_still_fits_the_command_line(self, started, tmp_path):
        """Windows drops a link past about 2 kB, so a long report is cut."""
        report = reporting.Report(kind="bug", summary="s" * 300, detail="x" * 8000)
        url = reporting.mailto_url(report, None)
        assert len(url) <= reporting.MAX_URL

        # And with the path of the full copy in the footer.  A log of short
        # lines packs the body to within a character or two of the budget,
        # which is where the newline joining the footer to the body used to be
        # spent without ever having been counted - three characters once
        # encoded, so the link came out over the limit it was measured against.
        for line in range(200):
            diagnostics.breadcrumb("%d", line)
        attachment = tmp_path / "stamp-crash-20260101-120000.txt"
        for budget in range(1400, 1500):
            body = reporting.build_body(report, attachment, budget=budget)
            assert reporting._encoded_length(body) <= budget, budget
        assert len(reporting.mailto_url(report, attachment)) <= reporting.MAX_URL

    def test_what_the_user_typed_survives_the_budget(self, started):
        """The words a person wrote matter more than the log, so they go in first."""
        report = reporting.Report(kind="bug", detail="the counter of the O is filled")
        body = reporting.build_body(report, None, budget=reporting.MAX_URL)
        assert "the counter of the O is filled" in body

    def test_the_body_needs_nothing_from_the_user(self, started, tmp_path):
        """The email must stand alone - a user who only pushes send is enough."""
        body = reporting.build_body(reporting.Report(kind="crash"), tmp_path / "r.txt")
        assert "Ctrl+V" not in body
        assert "clipboard" not in body.lower()
        assert "Stamp " in body            # the environment
        assert "Log, last lines:" in body  # and the log

    def test_the_path_of_the_full_copy_is_always_kept(self, started, tmp_path):
        """Room is kept for the path before the log fills the rest."""
        target = tmp_path / "stamp-crash.txt"
        report = reporting.Report(kind="crash", detail="y" * 6000)
        body = reporting.build_body(report, target, budget=reporting.MAX_URL)
        assert str(target) in body

    def test_the_environment_fits_on_one_line(self, started):
        line = reporting.environment_line()
        assert "\n" not in line
        assert "Stamp" in line


class TestSend:
    def test_send_writes_the_full_copy(self, started, qapp, monkeypatch):
        """The file half of send(), with the mail client held shut.

        send() ends in QDesktopServices.openUrl, which hands a real ``mailto:``
        to the desktop and takes over the screen with a compose window.  That is
        the point of the function and it is worth testing, but not on every run
        of the suite - see the opens_email test below.  What this one is about is
        the file that stands alone when the mail never opens, so the mail is
        stubbed out and the file is checked.
        """
        opened = []
        from PySide6.QtGui import QDesktopServices

        monkeypatch.setattr(
            QDesktopServices, "openUrl", lambda url: opened.append(url) or True
        )
        result = reporting.send(reporting.Report(kind="bug", detail="hello"))
        assert result.path is not None and result.path.exists()
        assert "hello" in result.path.read_text(encoding="utf-8")
        assert result.opened is True
        assert opened and opened[0].toString().startswith("mailto:")

    @pytest.mark.opens_email
    def test_send_really_opens_the_mail_client(self, started, qapp):
        """The whole path, including the compose window the user would see.

        Deselected by default: it opens a real email.  Run it deliberately, on a
        machine you are not sitting at or in a container - see README.
        """
        result = reporting.send(reporting.Report(kind="bug", detail="hello"))
        assert result.path is not None and result.path.exists()
        assert result.opened is True


class TestReportFile:
    def test_a_file_is_written_next_to_the_log(self, started):
        target = reporting.write_report_file(reporting.Report(kind="crash"))
        assert target is not None
        assert target.exists()
        assert target.parent == started
        assert "Stamp crash report" in target.read_text(encoding="utf-8")


class TestCrashFlag:
    @staticmethod
    def _fresh(tmp_path, monkeypatch, name):
        import logging

        monkeypatch.setattr(diagnostics, "log_dir", lambda: tmp_path)
        monkeypatch.setattr(diagnostics, "_started", False)
        monkeypatch.setattr(diagnostics, "_crashed_before", False)
        monkeypatch.setattr(diagnostics, "_log", logging.getLogger(name))
        diagnostics.start()

    def test_a_clean_exit_leaves_no_crash_behind(self, tmp_path, monkeypatch):
        self._fresh(tmp_path, monkeypatch, "stamp.flag.test")
        assert diagnostics.running_flag(tmp_path).exists()
        diagnostics.mark_clean_exit()
        assert not diagnostics.running_flag(tmp_path).exists()

    def test_a_flag_left_behind_reports_a_crash(self, tmp_path, monkeypatch):
        """The flag stays when the process dies, thus the next start sees it."""
        (tmp_path / f"{diagnostics.RUNNING_PREFIX}999999{diagnostics.RUNNING_SUFFIX}").write_text(
            "999999", encoding="utf-8"
        )
        self._fresh(tmp_path, monkeypatch, "stamp.flag2.test")
        assert diagnostics.previous_run_crashed()

    def test_a_second_window_is_not_a_crash(self, tmp_path, monkeypatch):
        """Two windows at once must not make either report a crash."""
        import os

        diagnostics.running_flag(tmp_path, os.getpid()).write_text(
            str(os.getpid()), encoding="utf-8"
        )
        self._fresh(tmp_path, monkeypatch, "stamp.flag3.test")
        assert not diagnostics.previous_run_crashed()

    def test_a_flag_from_a_dead_process_is_a_crash(self, tmp_path, monkeypatch):
        # A process id that is certainly not running.
        diagnostics.running_flag(tmp_path, 999999).write_text("999999", encoding="utf-8")
        self._fresh(tmp_path, monkeypatch, "stamp.flag4.test")
        assert diagnostics.previous_run_crashed()

    def test_a_second_window_does_not_take_the_first_ones_flag(
        self, tmp_path, monkeypatch
    ):
        """One shared file let the second window overwrite the first one's.

        The first window then crashed and nobody was ever told: its flag had
        already been replaced by a live process id, so the next start read the
        flag as somebody else's window rather than as a crash.
        """
        import os

        first = diagnostics.running_flag(tmp_path, 999999)
        first.write_text("999999", encoding="utf-8")
        self._fresh(tmp_path, monkeypatch, "stamp.flag5.test")
        assert diagnostics.running_flag(tmp_path, os.getpid()).exists()
        # The dead run was reported, and cleared so it is not reported twice.
        assert diagnostics.previous_run_crashed()
        assert not first.exists()

    def test_a_live_flag_survives_this_window(self, tmp_path, monkeypatch):
        """A window that is still open keeps its own flag when another starts."""
        import os

        other = diagnostics.running_flag(tmp_path, os.getpid())
        other.write_text(str(os.getpid()), encoding="utf-8")
        self._fresh(tmp_path, monkeypatch, "stamp.flag6.test")
        assert other.exists()

    @pytest.mark.skipif(
        sys.platform == "win32", reason="Windows asks the kernel, not os.kill"
    )
    def test_a_flag_from_another_user_is_not_a_crash(self, tmp_path, monkeypatch):
        """os.kill on somebody else's process raises rather than answering.

        Read as "not running", a process owned by another user on a shared
        machine turns into a crash report about a run that never happened.
        """
        import os

        def refuse(pid, signal):
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(os, "kill", refuse)
        diagnostics.running_flag(tmp_path, 4242).write_text("4242", encoding="utf-8")
        self._fresh(tmp_path, monkeypatch, "stamp.flag7.test")
        assert not diagnostics.previous_run_crashed()

    def test_the_flag_an_older_stamp_left_still_reports_a_crash(
        self, tmp_path, monkeypatch
    ):
        """Upgrading over a crashed run must not lose the crash."""
        legacy = tmp_path / diagnostics.RUNNING_NAME
        legacy.write_text("999999", encoding="utf-8")
        self._fresh(tmp_path, monkeypatch, "stamp.flag8.test")
        assert diagnostics.previous_run_crashed()
        assert not legacy.exists()


class TestLogFallback:
    def test_a_report_survives_a_missing_log_file(self, started, tmp_path):
        """A report must still carry the history when the log file is gone.

        The folder can be removed between runs, or the disk can refuse the write.
        The run keeps its own lines, so the report is never empty.
        """
        import pathlib

        diagnostics.breadcrumb("modifier: feature=logo kind=fillet edges=979")
        diagnostics._log_path = pathlib.Path(tmp_path / "not-here" / "stamp.log")

        text = reporting.build_text(reporting.Report(kind="crash"))
        assert "kind=fillet edges=979" in text
        assert "from memory" in text

    def test_the_file_is_preferred_when_it_is_there(self, started):
        diagnostics.breadcrumb("boolean: feature=logo kind=cut")
        text = reporting.build_text(reporting.Report(kind="crash"))
        assert "boolean: feature=logo kind=cut" in text
        assert "from memory" not in text
