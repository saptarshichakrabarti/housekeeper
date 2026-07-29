"""Scheduled scans: housekeeper emits scheduler text and stays daemon-free.

Golden-text tests, because the output is a contract with systemd/cron/Task Scheduler rather than
something a human reads loosely. Two invariants beyond the text itself: every scheduled command is
read-only (``quickstart`` plus the ``changes`` digest — never a move or delete), and the notification
is a command the operator supplies, so housekeeper never opens a network connection of its own.
"""

from __future__ import annotations

import shlex

import pytest

from housekeeper.schedules import (
    FORMATS,
    UNESCAPED_DOLLAR,
    scan_commands,
    schedule_text,
)

BINARY = "/usr/local/bin/housekeeper"


def _text(config, tmp_path, **kwargs):
    return schedule_text(config, tmp_path / "drive", executable=BINARY, **kwargs)


def test_systemd_unit_and_timer(config, tmp_path):
    files = _text(config, tmp_path, interval="weekly", output_format="systemd")
    assert set(files) == {"housekeeper-drive.service", "housekeeper-drive.timer"}
    service, timer = files["housekeeper-drive.service"], files["housekeeper-drive.timer"]
    assert service.startswith("[Unit]\n")
    assert "Type=oneshot" in service
    # One systemd argument: double-quoted, because the commands inside it are POSIX single-quoted.
    assert f'ExecStart=/bin/sh -c "{BINARY} --workspace' in service
    assert "quickstart" in service and "report changes" in service
    assert "OnCalendar=weekly" in timer
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer


def test_cron_line_is_one_line_with_the_right_schedule(config, tmp_path):
    line = _text(config, tmp_path, interval="daily", output_format="cron")[""]
    assert line.startswith("0 3 * * * ")
    assert line.count("\n") == 1
    assert _text(config, tmp_path, interval="weekly", output_format="cron")[""].startswith("0 3 * * 0 ")
    assert _text(config, tmp_path, interval="monthly", output_format="cron")[""].startswith("0 3 1 * * ")


def test_windows_task_xml(config, tmp_path):
    xml = _text(config, tmp_path, interval="weekly", output_format="windows")["housekeeper-drive.xml"]
    assert xml.startswith('<?xml version="1.0" encoding="UTF-16"?>')
    assert "<ScheduleByWeek>" in xml
    assert "<Command>cmd.exe</Command>" in xml
    # The command is XML-escaped, so the `&&` chain cannot break the document.
    assert "&amp;&amp;" in xml and " && " not in xml


def test_a_path_with_shell_metacharacters_cannot_run_a_command(config, tmp_path):
    """The emitted text runs unattended, so a path is a trust boundary even though it is the
    operator's own. ``drive;touch pwned`` must stay one argument, not two commands."""
    nasty = tmp_path / "drive;touch pwned && echo $(id) `id` #"
    nasty.mkdir()
    for output_format in ("systemd", "cron"):
        command = scan_commands(config, nasty, executable=BINARY, output_format=output_format)[0]
        quoted = shlex.quote(str(nasty))
        assert quoted in command
        # The shell sees exactly the words housekeeper intended: the path is one of them.
        assert shlex.split(command) == [
            BINARY, "--workspace", str(config.workspace), "quickstart", str(nasty),
            "--no-reports", "--json",
        ]
    # In cmd.exe single quotes are not quoting at all — the Windows form uses double quotes.
    windows = scan_commands(config, nasty, executable=BINARY, output_format="windows")[0]
    assert f'"{nasty}"' in windows


def _service(config, source):
    files = schedule_text(config, source, executable=BINARY)
    return next(text for name, text in files.items() if name.endswith(".service")), files


def test_systemd_nests_the_quoted_command_without_breaking_out(config, tmp_path):
    nasty = tmp_path / "drive'; touch pwned; '$HOME"
    nasty.mkdir()
    service, _files = _service(config, nasty)
    exec_line = next(line for line in service.splitlines() if line.startswith("ExecStart="))
    # One systemd argument, double-quoted, so the POSIX single quotes inside it stay literal. A
    # literal dollar survives as `$$` because systemd expands `$` in ExecStart.
    assert exec_line.startswith('ExecStart=/bin/sh -c "')
    assert exec_line.endswith('"')
    assert "$$HOME" in exec_line
    assert UNESCAPED_DOLLAR.search(exec_line) is None
    # What the shell would run: the path is still one argument, and nothing follows it.
    inner = exec_line[len('ExecStart=/bin/sh -c "') : -1].replace('\\"', '"').replace("$$", "$")
    assert shlex.split(inner)[:5] == [
        BINARY, "--workspace", str(config.workspace), "quickstart", str(nasty)
    ]


def test_a_path_with_a_line_break_is_refused(config, tmp_path):
    """A line break ends the record in every format — one crontab line, one unit directive — so it is
    refused rather than escaped into something that looks fine and is not."""
    nasty = tmp_path / "drive\nExecStartPre=touch pwned"
    nasty.mkdir()
    for output_format in FORMATS:
        with pytest.raises(ValueError, match="line break"):
            schedule_text(config, nasty, executable=BINARY, output_format=output_format)


def test_percent_in_a_path_is_escaped_for_cron(config, tmp_path):
    nasty = tmp_path / "50%25 backup"
    nasty.mkdir()
    line = schedule_text(config, nasty, executable=BINARY, output_format="cron")[""]
    assert r"\%" in line
    assert "%" not in line.replace(r"\%", "")


def test_a_double_quote_is_refused_for_windows(config, tmp_path):
    with pytest.raises(ValueError, match="double quote"):
        scan_commands(config, tmp_path / 'dri"ve', executable=BINARY, output_format="windows")


def test_scheduled_commands_are_read_only(config, tmp_path):
    commands = scan_commands(config, tmp_path / "drive", executable=BINARY)
    assert len(commands) == 2
    assert "quickstart" in commands[0] and "--no-reports --json" in commands[0]
    assert commands[1].endswith("report changes")
    for forbidden in ("move-to-review", "export-review", "purge", "restore"):
        assert not any(forbidden in command for command in commands)


def test_notification_command_is_appended_when_configured(config, tmp_path):
    config.section("notifications")["command"] = "notify-send 'Housekeeper'"
    commands = scan_commands(config, tmp_path / "drive", executable=BINARY)
    assert commands[-1].endswith("changes | notify-send 'Housekeeper'")
    # Nothing to notify with by default, so nothing is piped anywhere.
    config.section("notifications")["command"] = ""
    assert len(scan_commands(config, tmp_path / "drive", executable=BINARY)) == 2


def test_source_is_made_absolute(config, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "drive").mkdir()
    commands = scan_commands(config, "drive", executable=BINARY)
    assert str((tmp_path / "drive").resolve()) in commands[0]


@pytest.mark.parametrize("output_format", FORMATS)
def test_every_format_rejects_an_unknown_interval(config, tmp_path, output_format):
    with pytest.raises(ValueError, match="interval"):
        _text(config, tmp_path, interval="hourly", output_format=output_format)


def test_unknown_format_is_rejected(config, tmp_path):
    with pytest.raises(ValueError, match="format"):
        _text(config, tmp_path, output_format="launchd")
