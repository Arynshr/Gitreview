from __future__ import annotations

import os
import signal
import subprocess
import time

from gitscribe.validation.analysis.models import ProcessResult, ScannerCommand


class ScannerExecutionError(RuntimeError):
    """Raised only for infrastructure failures the runner itself can't
    recover from (binary not found, hung process that had to be killed
    after its timeout). A scanner reporting real findings via a non-zero
    exit code is NOT an error - interpreting a scanner's own exit-code
    conventions is each Scanner adapter's job in .parse(), not the
    runner's. The runner only reports what happened; it never decides
    what a given exit code means for a specific tool.
    """


def run_scanner_command(
    command: ScannerCommand,
    telemetry: list[dict] | None = None,
) -> ProcessResult:
    """Owns subprocess creation, timeout enforcement, output-size limits,
    process-group termination, environment isolation, and diagnostics for
    every scanner adapter.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        **command.env,
    }

    started = time.perf_counter()
    timed_out = False
    returncode: int | None = None
    stdout = ""
    stderr = ""

    try:
        proc = subprocess.Popen(
            command.args,
            cwd=command.cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,  
        )
    except FileNotFoundError as exc:
        raise ScannerExecutionError(
            f"required scanner not found: {command.args[0]}"
        ) from exc

    try:
        stdout, stderr = proc.communicate(timeout=command.timeout_seconds)
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_group(proc)
        stdout, stderr = proc.communicate()

    duration = time.perf_counter() - started

    truncated = False
    if len(stdout.encode("utf-8", "ignore")) > command.max_output_bytes:
        stdout = stdout[: command.max_output_bytes]
        truncated = True
    if len(stderr.encode("utf-8", "ignore")) > command.max_output_bytes:
        stderr = stderr[: command.max_output_bytes]
        truncated = True

    if telemetry is not None:
        telemetry.append(
            {
                "scanner_args": command.args,
                "returncode": returncode,
                "timed_out": timed_out,
                "truncated": truncated,
                "duration_seconds": duration,
            }
        )

    if timed_out:
        raise ScannerExecutionError(
            f"{command.args[0]} did not finish within "
            f"{command.timeout_seconds}s"
        )

    return ProcessResult(
        args=command.args,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        truncated=truncated,
        duration_seconds=duration,
    )


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGTERM the whole process group first (lets a scanner's own child
    processes exit cleanly), then SIGKILL anything still alive after a
    short grace period. Killing only proc.pid would leave orphaned
    children
    """
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return

    try:
        os.killpg(pgid, signal.SIGTERM)
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
