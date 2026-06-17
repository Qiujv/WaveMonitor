"""Run the WaveMonitor client/server VizTracer workflow in one command."""

from __future__ import annotations

import argparse
import gzip
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from bench_viztracer import (
    DEFAULT_CLIENT_SCRIPT,
    DEFAULT_TRACE_DIR,
    ROOT,
    build_viztracer_cmd,
)

from wave_monitor import WaveMonitor
from wave_monitor.constants import PIPE_NAME
from wave_monitor.ipc.launcher import can_connect_to_server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--script",
        type=Path,
        default=DEFAULT_CLIENT_SCRIPT,
        help="Client workload script to profile.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_TRACE_DIR,
        help="Directory for generated trace files.",
    )
    parser.add_argument(
        "--min-duration",
        default="10us",
        help="Minimum function duration for VizTracer to record.",
    )
    parser.add_argument(
        "--tracer-entries",
        type=int,
        default=5_000_000,
        help="VizTracer circular buffer size.",
    )
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for the server window to start listening.",
    )
    parser.add_argument(
        "--shutdown-timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for the server trace to flush after shutdown.",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open the combined trace with vizviewer.",
    )
    parser.add_argument(
        "--no-combine",
        action="store_true",
        help="Do not combine the server and client traces.",
    )
    args = parser.parse_args()

    if can_connect_to_server(PIPE_NAME, 100):
        raise RuntimeError(
            f'Another WaveMonitor server is already listening on "{PIPE_NAME}". '
            "Close it before running this benchmark."
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    server_output = args.output_dir / f"viztracer-server-{timestamp}.json"
    client_output = args.output_dir / f"viztracer-client-{timestamp}.json"
    combined_output = args.output_dir / f"viztracer-combined-{timestamp}.json"

    server_proc = start_server_trace(args, server_output)
    try:
        wait_for_server(server_proc, args.startup_timeout)
        run_client_trace(args, client_output)
    finally:
        stop_server(server_proc, args.shutdown_timeout)

    print(f"Server trace: {server_output}", flush=True)
    print(f"Client trace: {client_output}", flush=True)
    if args.no_combine:
        if not args.no_open:
            open_traces([server_output, client_output])
        return

    combine_traces([server_output, client_output], combined_output)
    print(f"Combined trace: {combined_output}", flush=True)
    if not args.no_open:
        open_traces([combined_output])


def start_server_trace(
    args: argparse.Namespace, output_file: Path
) -> subprocess.Popen[bytes]:
    cmd = build_viztracer_cmd(trace_args(args, "server", None), output_file)
    print("Starting server:", " ".join(str(part) for part in cmd), flush=True)
    return subprocess.Popen(cmd, cwd=ROOT, **popen_group_kwargs())


def run_client_trace(args: argparse.Namespace, output_file: Path) -> None:
    cmd = build_viztracer_cmd(trace_args(args, "client", args.script), output_file)
    print("Running client:", " ".join(str(part) for part in cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def trace_args(
    args: argparse.Namespace, mode: str, script: Path | None
) -> SimpleNamespace:
    return SimpleNamespace(
        mode=mode,
        script=script,
        min_duration=args.min_duration,
        tracer_entries=args.tracer_entries,
    )


def wait_for_server(proc: subprocess.Popen[bytes], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Server exited early with code {proc.returncode}.")
        if can_connect_to_server(PIPE_NAME, 100):
            return
        time.sleep(0.1)
    raise TimeoutError("Timed out waiting for the WaveMonitor server to start.")


def stop_server(proc: subprocess.Popen[bytes], timeout: float) -> None:
    if proc.poll() is not None:
        return

    print("Stopping server...", flush=True)
    try:
        request_server_shutdown()
        proc.wait(timeout=timeout)
        return
    except Exception as exc:
        print(f"Graceful shutdown failed: {exc}", flush=True)

    interrupt_process_group(proc)
    try:
        proc.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass

    proc.terminate()
    try:
        proc.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def popen_group_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def interrupt_process_group(proc: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        proc.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        os.killpg(proc.pid, signal.SIGINT)


def request_server_shutdown() -> None:
    monitor = WaveMonitor(create_window=False)
    try:
        monitor.close_window()
        monitor.close(timeout=None)
    finally:
        monitor.close()


def combine_traces(inputs: list[Path], output_file: Path) -> None:
    json_inputs = [gunzip_trace(path) for path in inputs]
    cmd = [
        sys.executable,
        "-m",
        "viztracer",
        "--combine",
        *[str(path) for path in json_inputs],
        "-o",
        str(output_file),
    ]
    print("Combining traces:", " ".join(str(part) for part in cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def gunzip_trace(path: Path) -> Path:
    if path.suffix != ".gz":
        return path
    output_path = path.with_suffix(".json")
    with gzip.open(path, "rb") as f_in, output_path.open("wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    return output_path


def open_traces(paths: list[Path]) -> None:
    vizviewer = shutil.which("vizviewer")
    if vizviewer is None:
        print("vizviewer not found on PATH; open traces manually.", flush=True)
        return
    for path in paths:
        subprocess.Popen([vizviewer, str(path)], cwd=ROOT)


if __name__ == "__main__":
    main()
