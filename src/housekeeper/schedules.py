"""Emit the scheduler text for a recurring scan. Housekeeper itself stays daemon-free.

The tool does not run resident and does not talk to the network. A recurring scan is therefore the
platform's job: this module prints a systemd user timer, a crontab line, or a Windows Task Scheduler
task, all of which invoke the ordinary read-only ``quickstart`` and then the ``changes`` digest.

Nothing here writes to a system directory. The text goes to stdout for the operator to place — a
scheduler unit is theirs to install, and printing it keeps the tool's "never surprises you" contract.
"""

from __future__ import annotations

import re
import shlex
import shutil
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from .path_utils import sanitize_report_filename

INTERVALS = ("daily", "weekly", "monthly")
FORMATS = ("systemd", "cron", "windows")

# 03:00 local time: after midnight log rotation, before a working day.
_HOUR = 3
_CRON_SCHEDULE = {"daily": f"0 {_HOUR} * * *", "weekly": f"0 {_HOUR} * * 0", "monthly": f"0 {_HOUR} 1 * *"}


def housekeeper_command() -> str:
    """How to invoke this installation. Absolute, because a scheduler has no PATH worth trusting."""
    found = shutil.which("housekeeper")
    return found if found else f"{sys.executable} -m housekeeper"


def scan_commands(
    config, source_root: Path, executable: str | None = None, output_format: str = "systemd"
) -> list[str]:
    """The commands a scheduled run executes, in order.

    A scan and the digest of what it found — both read-only. The optional notification is a command
    the operator supplies (``notifications.command``): housekeeper pipes the digest into it and never
    speaks to the network itself.

    Every path is quoted for the shell that will run it. This is a trust boundary even though the
    input is the operator's own path: a directory called ``drive;rm -rf ~`` is a legal directory name,
    and the text this function returns ends up in a crontab or a unit file that runs unattended.
    """
    quote = _QUOTERS[output_format]
    _reject_control_characters(config.workspace, source_root)
    binary = executable or housekeeper_command()
    base = f"{binary} --workspace {quote(config.workspace)}"
    # Absolute: a scheduled command starts in whatever directory the scheduler chose.
    source = Path(source_root).expanduser().resolve()
    commands = [f"{base} quickstart {quote(source)} --no-reports --json", f"{base} report changes"]
    # Deliberately NOT quoted: this key is a shell command the operator wrote (`notify-send …`,
    # `mail -s … me@example.com`, a curl). Quoting it would break every use of it.
    notify = str(config.section("notifications")["command"]).strip()
    if notify:
        commands.append(f"{base} changes | {notify}")
    return commands


def _reject_control_characters(*paths: object) -> None:
    """Refuse a path with a newline, carriage return or NUL rather than emitting one.

    Quoting handles metacharacters, but a line break is not a metacharacter — it ends the record in
    every format here: a crontab entry is one line, and a unit-file line is one directive. Such a path
    is legal on POSIX and pathological in practice, so this says no instead of getting clever.
    """
    for path in paths:
        text = str(path)
        if any(character in text for character in "\n\r\x00"):
            raise ValueError(f"refusing to schedule a path containing a line break: {text!r}")


def _posix_quote(value: object) -> str:
    return shlex.quote(str(value))


def _windows_quote(value: object) -> str:
    """Quote for ``cmd.exe``: double quotes, inside which ``& | < > ^`` lose their meaning.

    ``shlex.quote`` is wrong here — cmd.exe does not treat single quotes as quoting at all. A double
    quote cannot appear in a Windows path, so one in the input means something is being smuggled and
    is refused rather than escaped.
    """
    text = str(value)
    if '"' in text:
        raise ValueError(f"refusing to schedule a path containing a double quote: {text}")
    return f'"{text}"'


_QUOTERS = {"systemd": _posix_quote, "cron": _posix_quote, "windows": _windows_quote}


def _systemd_argument(command: str) -> str:
    """The shell command as one double-quoted systemd argument.

    The commands are already POSIX-quoted, which means single quotes — so the ``sh -c`` argument
    cannot itself be single-quoted. systemd's own double-quoted form takes ``\\`` and ``"`` escapes,
    and expands ``$``, which a literal dollar in a path must survive as ``$$``.
    """
    escaped = command.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$")
    return f'"{escaped}"'


#: Unescaped ``$`` in an ExecStart line — systemd would expand it. Used by the tests as the check
#: that the escaping above actually held.
UNESCAPED_DOLLAR = re.compile(r"(?<!\$)\$(?!\$)")


def _cron_line(command: str) -> str:
    """A crontab command field: ``%`` is cron's own escape and must be neutralised."""
    return command.replace("%", r"\%")


def schedule_text(
    config,
    source_root: Path,
    interval: str = "weekly",
    output_format: str = "systemd",
    executable: str | None = None,
) -> dict[str, str]:
    """Scheduler text keyed by the file name it belongs in (a crontab line has none)."""
    if interval not in INTERVALS:
        raise ValueError(f"interval must be one of {', '.join(INTERVALS)}")
    if output_format not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")
    commands = scan_commands(config, source_root, executable, output_format)
    # Both the file name and the human description are derived from a path the operator chose, and a
    # path may legally contain a newline — which in a unit file would be a new directive.
    name = f"housekeeper-{sanitize_report_filename(Path(source_root).name or 'scan')}"
    description = " ".join(str(source_root).split())
    if output_format == "cron":
        return {"": f"{_CRON_SCHEDULE[interval]} {_cron_line(' && '.join(commands))}\n"}
    if output_format == "systemd":
        return {
            f"{name}.service": (
                "[Unit]\n"
                f"Description=Housekeeper read-only scan of {description}\n\n"
                "[Service]\n"
                "Type=oneshot\n"
                f"ExecStart=/bin/sh -c {_systemd_argument(' && '.join(commands))}\n"
            ),
            f"{name}.timer": (
                "[Unit]\n"
                f"Description=Housekeeper {interval} scan of {description}\n\n"
                "[Timer]\n"
                f"OnCalendar={interval}\n"
                "Persistent=true\n\n"
                "[Install]\n"
                "WantedBy=timers.target\n"
            ),
        }
    return {f"{name}.xml": _windows_task(commands, interval, description)}


_WINDOWS_TRIGGER = {
    "daily": "      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>\n",
    "weekly": (
        "      <ScheduleByWeek><WeeksInterval>1</WeeksInterval>"
        "<DaysOfWeek><Sunday /></DaysOfWeek></ScheduleByWeek>\n"
    ),
    "monthly": (
        "      <ScheduleByMonth><DaysOfMonth><Day>1</Day></DaysOfMonth>"
        "<Months><January /><February /><March /><April /><May /><June /><July /><August />"
        "<September /><October /><November /><December /></Months></ScheduleByMonth>\n"
    ),
}


def _windows_task(commands: list[str], interval: str, source_root: str) -> str:
    # cmd.exe rather than sh: the same read-only commands, chained so a failure stops the sequence.
    arguments = escape("/c " + " && ".join(commands))
    trigger = _WINDOWS_TRIGGER[interval]
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        f"    <Description>Housekeeper {interval} read-only scan of {escape(str(source_root))}"
        "</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <CalendarTrigger>\n"
        f"      <StartBoundary>2024-01-01T0{_HOUR}:00:00</StartBoundary>\n"
        f"{trigger}"
        "    </CalendarTrigger>\n"
        "  </Triggers>\n"
        "  <Settings>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "  </Settings>\n"
        "  <Actions>\n"
        "    <Exec>\n"
        "      <Command>cmd.exe</Command>\n"
        f"      <Arguments>{arguments}</Arguments>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


INSTALL_HINTS = {
    "systemd": (
        "Save both files under ~/.config/systemd/user/, then: "
        "systemctl --user daemon-reload && systemctl --user enable --now <name>.timer"
    ),
    "cron": "Append the line to your crontab: housekeeper schedule ... --format cron | crontab -",
    "windows": 'Import the task: schtasks /Create /TN Housekeeper /XML "<file>.xml"',
}
