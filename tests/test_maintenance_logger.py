"""Tests for maintenance_logger timeout and slowdown tracking."""
import json
import subprocess
import time
from pathlib import Path

import maintenance_logger as logger


def test_log_writes_to_specified_path(tmp_path: Path):
    target_log = tmp_path / "test.log"
    logger.log("hello world", path=str(target_log))
    assert target_log.exists()
    content = target_log.read_text(encoding="utf-8")
    assert "hello world" in content


def test_log_slowdown_writes_both_logs(tmp_path: Path):
    main_log = tmp_path / "IdleMaintenance.log"
    slow_log = tmp_path / "slowdown.log"

    msg = logger.log_slowdown(
        "test_component",
        "test_operation",
        duration=4.5,
        threshold=2.0,
        details="extra context",
        log_path=str(main_log),
        slow_log_path=str(slow_log),
    )

    assert "[SLOW] test_component: test_operation took 4.50s (threshold 2.00s): extra context" in msg
    assert main_log.exists()
    assert "[SLOW]" in main_log.read_text(encoding="utf-8")

    assert slow_log.exists()
    records = logger.load_recent_slowdowns(limit=5, path=slow_log)
    assert len(records) == 1
    assert records[0]["type"] == "slowdown"
    assert records[0]["component"] == "test_component"
    assert records[0]["operation"] == "test_operation"
    assert records[0]["duration"] == 4.5
    assert records[0]["threshold"] == 2.0
    assert records[0]["details"] == "extra context"


def test_log_timeout_writes_both_logs(tmp_path: Path):
    main_log = tmp_path / "IdleMaintenance.log"
    slow_log = tmp_path / "slowdown.log"

    msg = logger.log_timeout(
        "test_comp",
        "long_running_job",
        timeout=10.0,
        details="timed out after 10 seconds",
        log_path=str(main_log),
        slow_log_path=str(slow_log),
    )

    assert "[TIMEOUT] test_comp: long_running_job timed out after 10.00s: timed out after 10 seconds" in msg
    assert main_log.exists()
    assert "[TIMEOUT]" in main_log.read_text(encoding="utf-8")

    records = logger.load_recent_slowdowns(limit=5, path=slow_log)
    assert len(records) == 1
    assert records[0]["type"] == "timeout"
    assert records[0]["component"] == "test_comp"
    assert records[0]["operation"] == "long_running_job"
    assert records[0]["timeout"] == 10.0


def test_track_slowdown_logs_when_exceeding_threshold(tmp_path: Path):
    main_log = tmp_path / "IdleMaintenance.log"
    slow_log = tmp_path / "slowdown.log"

    with logger.track_slowdown(
        "comp",
        "sleepy_task",
        threshold=0.01,
        log_path=str(main_log),
        slow_log_path=str(slow_log),
    ) as ctx:
        time.sleep(0.02)
        ctx["details"] = "slept 20ms"

    records = logger.load_recent_slowdowns(limit=5, path=slow_log)
    assert len(records) == 1
    assert records[0]["type"] == "slowdown"
    assert records[0]["component"] == "comp"
    assert records[0]["operation"] == "sleepy_task"
    assert "slept 20ms" in records[0]["details"]


def test_track_slowdown_catches_timeout_expired(tmp_path: Path):
    main_log = tmp_path / "IdleMaintenance.log"
    slow_log = tmp_path / "slowdown.log"

    try:
        with logger.track_slowdown(
            "subproc",
            "hang_cmd",
            threshold=1.0,
            log_path=str(main_log),
            slow_log_path=str(slow_log),
        ):
            raise subprocess.TimeoutExpired(cmd=["sleep", "5"], timeout=0.5)
    except subprocess.TimeoutExpired:
        pass

    records = logger.load_recent_slowdowns(limit=5, path=slow_log)
    assert len(records) == 1
    assert records[0]["type"] == "timeout"
    assert records[0]["component"] == "subproc"
    assert records[0]["operation"] == "hang_cmd"
    assert records[0]["timeout"] == 0.5
