"""Reports that a user sends back - a crash report and a bug report.

The report goes out as an email that the user's own mail program opens, already
addressed and already filled in.  Nothing leaves the machine until the user
pushes send, thus the user always sees what they send.

A ``mailto:`` link has no field for an attachment, thus Stamp cannot attach the
log.  The link is also limited to about 2 kB, and that limit applies after the
escaping, which is about half of what it looks like.

Thus the email is made to stand on its own inside that limit.  The most useful
part goes in first, and the rest of the log fills what space is left.  The user
sends the email and does nothing else.  A complete copy goes to a file as well,
for the reports where the last lines are not sufficient.
"""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from stamp import diagnostics

#: Where a report goes.
SUPPORT_EMAIL = "dmoore@dwmmholdings.com"

#: Windows stops at about 2 kB of command line.  The budget is on the *encoded*
#: link, because a newline becomes three characters once it is escaped.
MAX_URL = 1900

#: Lines of the log put in the report file.
LOG_TAIL_LINES = 400

#: Lines of the log put in the email itself.
BODY_TAIL_LINES = 25


def app_version() -> str:
    try:
        from importlib.metadata import version

        return version("stamp")
    except Exception:
        return "unknown"


@dataclass
class Report:
    """One report, before it becomes an email."""

    kind: str  # "crash" or "bug"
    summary: str = ""
    detail: str = ""
    steps: str = ""
    expected: str = ""
    part: str = ""
    answers: dict[str, str] = field(default_factory=dict)

    @property
    def subject(self) -> str:
        head = "Stamp crash report" if self.kind == "crash" else "Stamp bug report"
        if self.summary.strip():
            return f"{head}: {self.summary.strip()[:80]}"
        return head


def environment_lines() -> list[str]:
    """What Stamp knows about the machine, which a user cannot be asked for."""
    lines = [
        f"Stamp version : {app_version()}",
        f"When          : {datetime.now().isoformat(timespec='seconds')}",
        f"System        : {platform.platform()}",
        f"Processor     : {platform.machine()}",
        f"Python        : {sys.version.split()[0]}",
    ]
    try:
        from PySide6 import __version__ as pyside_version
        from PySide6.QtCore import qVersion

        lines.append(f"Qt            : {qVersion()} (PySide6 {pyside_version})")
    except Exception:
        pass
    return lines


def environment_line() -> str:
    """The same facts on one line.  The email has no room for six."""
    parts = [f"Stamp {app_version()}", platform.platform(), f"Py{sys.version.split()[0]}"]
    try:
        from PySide6.QtCore import qVersion

        parts.append(f"Qt{qVersion()}")
    except Exception:
        pass
    return " | ".join(parts)


def _log_tail(path: Path | None, lines: int) -> list[str]:
    """The last lines of the log, from the file or from memory.

    The file is the fuller record, but it can be missing.  This run keeps its own
    lines as well, thus a report is never empty because of a file.
    """
    text: list[str] = []
    if path is not None and path.exists():
        try:
            text = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as exc:
            text = [f"(the log file could not be read: {exc})"]
    if not text:
        remembered = diagnostics.recent_lines()
        if remembered:
            return ["(from memory, the log file is not readable)", *remembered[-lines:]]
        return ["(no log)"]
    return text[-lines:]


def source_log(report: Report) -> Path | None:
    """The log to send: the crashed run's log for a crash, else this run's."""
    if report.kind == "crash":
        previous = diagnostics.previous_log()
        if previous is not None:
            return previous
    return diagnostics.log_path()


def build_text(report: Report) -> str:
    """The full report, which is what the file holds."""
    parts: list[str] = []
    title = "Stamp crash report" if report.kind == "crash" else "Stamp bug report"
    parts.append(title)
    parts.append("=" * len(title))
    parts.append("")

    if report.kind == "crash":
        if diagnostics.previous_run_crashed():
            parts.append("Stamp did not stop cleanly the last time it ran.")
        else:
            parts.append("The user sent this report by hand.")
        parts.append("")

    for caption, value in (
        ("What happened", report.detail),
        ("What was expected", report.expected),
        ("Steps", report.steps),
        ("Part and artwork", report.part),
    ):
        if value.strip():
            parts.append(f"{caption}:")
            parts.append(value.strip())
            parts.append("")

    for question, answer in report.answers.items():
        parts.append(f"{question}: {answer}")
    if report.answers:
        parts.append("")

    parts.append("Environment")
    parts.append("-" * 11)
    parts.extend(environment_lines())
    parts.append("")

    log = source_log(report)
    parts.append(f"Log ({log})")
    parts.append("-" * 11)
    parts.extend(_log_tail(log, LOG_TAIL_LINES))
    parts.append("")
    return "\n".join(parts)


def write_report_file(report: Report) -> Path | None:
    """Write the full report next to the log, and give back its path."""
    directory = diagnostics.log_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = directory / f"stamp-{report.kind}-{stamp}.txt"
        target.write_text(build_text(report), encoding="utf-8")
        return target
    except Exception as exc:
        diagnostics.note_exception("write_report_file", exc)
        return None


def _encoded_length(text: str) -> int:
    return len(quote(text, safe=""))


def build_body(report: Report, attachment: Path | None, budget: int | None = None) -> str:
    """The email itself, which has to stand on its own.

    Sections go in by importance and stop when the budget is spent, thus a report
    that is too long loses the least useful part rather than its last half.
    """
    def fits(candidate: list[str]) -> bool:
        return budget is None or _encoded_length("\n".join(candidate)) <= budget

    lines: list[str] = []
    if report.kind == "crash":
        if diagnostics.previous_run_crashed():
            lines.append("Stamp stopped without warning. The last run did not end cleanly.")
        else:
            lines.append("A user reported this by hand after Stamp stopped.")
    else:
        lines.append("A report about Stamp follows.")
    lines.append("")

    # 1. What the person actually said.  Nothing replaces it.
    for caption, value in (
        ("What happened", report.detail),
        ("What was expected", report.expected),
        ("Steps", report.steps),
        ("Part and artwork", report.part),
    ):
        text = " ".join(value.split())
        if not text:
            continue
        candidate = [*lines, f"{caption}: {text}"]
        if not fits(candidate):
            room = 120
            candidate = [*lines, f"{caption}: {text[:room]}..."]
            if not fits(candidate):
                continue
        lines = candidate
    lines.append("")

    # 2. The machine, on one line.
    candidate = [*lines, environment_line(), ""]
    if fits(candidate):
        lines = candidate

    # 3. Keep room for the path of the complete copy before the log takes the
    #    rest, because that path is short and it is how the full detail is found.
    footer: list[str] = []
    if attachment is not None:
        footer = ["", f"Full log: {attachment}"]
        if budget is not None:
            budget -= _encoded_length("\n".join(footer))

    # 4. The log, newest last.  Take as many lines as the rest of the budget holds.
    tail = _log_tail(source_log(report), BODY_TAIL_LINES)
    if tail and tail != ["(no log)"]:
        header = [*lines, "Log, last lines:"]
        if fits(header):
            lines = header
            kept: list[str] = []
            for line in reversed(tail):
                trimmed = line.rstrip()
                if fits([*lines, trimmed, *kept]):
                    kept.insert(0, trimmed)
                else:
                    break
            lines.extend(kept)

    # 5. The path, on the room kept for it above.
    lines.extend(footer)
    return "\n".join(lines)


def mailto_url(report: Report, attachment: Path | None) -> str:
    """Build the link, with the body already sized to fit the limit."""
    subject = quote(report.subject, safe="")
    head = f"mailto:{SUPPORT_EMAIL}?subject={subject}&body="
    body = build_body(report, attachment, budget=MAX_URL - len(head))
    return head + quote(body, safe="")


@dataclass
class SendResult:
    """What happened when Stamp tried to hand the report over."""

    path: Path | None = None
    opened: bool = False


def send(report: Report) -> SendResult:
    """Write the full report to a file, then open the email that stands alone."""
    result = SendResult(path=write_report_file(report))
    try:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        result.opened = QDesktopServices.openUrl(QUrl(mailto_url(report, result.path)))
    except Exception as exc:
        diagnostics.note_exception("reporting.send", exc)
    diagnostics.breadcrumb(
        "report: kind=%s file=%s mail_opened=%s",
        report.kind, result.path, result.opened,
    )
    return result


__all__ = [
    "SUPPORT_EMAIL",
    "Report",
    "SendResult",
    "build_body",
    "environment_line",
    "build_text",
    "environment_lines",
    "mailto_url",
    "send",
    "write_report_file",
]
