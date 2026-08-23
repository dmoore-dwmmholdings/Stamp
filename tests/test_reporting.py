"""Crash reports and bug reports - the email a user sends back."""

from __future__ import annotations

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

    def test_a_long_report_still_fits_the_command_line(self, started):
        """Windows drops a link past about 2 kB, so a long report is cut."""
        report = reporting.Report(kind="bug", summary="s" * 300, detail="x" * 8000)
        url = reporting.mailto_url(report, None)
        assert len(url) <= reporting.MAX_URL

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
    def test_send_writes_the_full_copy(self, started, qapp):
        result = reporting.send(reporting.Report(kind="bug", detail="hello"))
        assert result.path is not None and result.path.exists()
        assert "hello" in result.path.read_text(encoding="utf-8")


class TestReportFile:
    def test_a_file_is_written_next_to_the_log(self, started):
        target = reporting.write_report_file(reporting.Report(kind="crash"))
        assert target is not None
        assert target.exists()
        assert target.parent == started
        assert "Stamp crash report" in target.read_text(encoding="utf-8")


class TestCrashFlag:
    def test_a_clean_exit_leaves_no_crash_behind(self, tmp_path, monkeypatch):
        import logging

        monkeypatch.setattr(diagnostics, "log_dir", lambda: tmp_path)
        monkeypatch.setattr(diagnostics, "_started", False)
        monkeypatch.setattr(diagnostics, "_crashed_before", False)
        monkeypatch.setattr(diagnostics, "_log", logging.getLogger("stamp.flag.test"))
        diagnostics.start()
        assert (tmp_path / diagnostics.RUNNING_NAME).exists()
        diagnostics.mark_clean_exit()
        assert not (tmp_path / diagnostics.RUNNING_NAME).exists()

    def test_a_flag_left_behind_reports_a_crash(self, tmp_path, monkeypatch):
        """The flag stays when the process dies, thus the next start sees it."""
        import logging

        (tmp_path / diagnostics.RUNNING_NAME).write_text("running", encoding="utf-8")
        monkeypatch.setattr(diagnostics, "log_dir", lambda: tmp_path)
        monkeypatch.setattr(diagnostics, "_started", False)
        monkeypatch.setattr(diagnostics, "_crashed_before", False)
        monkeypatch.setattr(diagnostics, "_log", logging.getLogger("stamp.flag2.test"))
        diagnostics.start()
        assert diagnostics.previous_run_crashed()

    def test_a_second_window_is_not_a_crash(self, tmp_path, monkeypatch):
        """Two windows at once must not make either report a crash."""
        import logging
        import os

        (tmp_path / diagnostics.RUNNING_NAME).write_text(
            str(os.getpid()), encoding="utf-8"
        )
        monkeypatch.setattr(diagnostics, "log_dir", lambda: tmp_path)
        monkeypatch.setattr(diagnostics, "_started", False)
        monkeypatch.setattr(diagnostics, "_crashed_before", False)
        monkeypatch.setattr(diagnostics, "_log", logging.getLogger("stamp.flag3.test"))
        diagnostics.start()
        assert not diagnostics.previous_run_crashed()

    def test_a_flag_from_a_dead_process_is_a_crash(self, tmp_path, monkeypatch):
        import logging

        # A process id that is certainly not running.
        (tmp_path / diagnostics.RUNNING_NAME).write_text("999999", encoding="utf-8")
        monkeypatch.setattr(diagnostics, "log_dir", lambda: tmp_path)
        monkeypatch.setattr(diagnostics, "_started", False)
        monkeypatch.setattr(diagnostics, "_crashed_before", False)
        monkeypatch.setattr(diagnostics, "_log", logging.getLogger("stamp.flag4.test"))
        diagnostics.start()
        assert diagnostics.previous_run_crashed()


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
