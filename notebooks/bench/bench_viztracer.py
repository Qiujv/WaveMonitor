"""Run WaveMonitor benchmarks with VizTracer.

Examples:

    uv run python notebooks/bench/bench_viztracer.py client
    uv run python notebooks/bench/bench_viztracer.py server

Client mode profiles the public API path used by ``bench_client.py``.
Server mode starts the monitor window under VizTracer; run client mode or
another client workload while it is open, then close the window to write the
server trace.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TRACE_DIR = Path(__file__).resolve().parent / "results"
DEFAULT_CLIENT_SCRIPT = Path(__file__).resolve().parent / "bench_client.py"
EXCLUDE_FILES = [
    str(ROOT / ".venv"),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="mode", required=True)

    client_parser = add_common_options(subparsers.add_parser("client"))
    client_parser.add_argument(
        "--script",
        type=Path,
        default=DEFAULT_CLIENT_SCRIPT,
        help="Client workload script to profile.",
    )

    add_common_options(subparsers.add_parser("server"))

    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_file = args.output or default_output_path(args.output_dir, args.mode)

    cmd = build_viztracer_cmd(args, output_file)
    print("Running:", " ".join(str(part) for part in cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)
    print(f"Trace written to {output_file}", flush=True)


def add_common_options(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Trace output file (.html, .json, or .gz).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_TRACE_DIR,
        help="Directory used when --output is omitted.",
    )
    parser.add_argument(
        "--min-duration",
        default="10us",
        help="Minimum function duration for VizTracer to record, e.g. 10us or 1ms.",
    )
    parser.add_argument(
        "--tracer-entries",
        type=int,
        default=5_000_000,
        help="VizTracer circular buffer size.",
    )
    return parser


def default_output_path(output_dir: Path, mode: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return output_dir / f"viztracer-{mode}-{timestamp}.json"


def build_viztracer_cmd(args: argparse.Namespace, output_file: Path) -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        "viztracer",
    ]

    cmd.extend(["--exclude_files", *EXCLUDE_FILES])

    cmd.extend(
        [
            "--min_duration",
            args.min_duration,
            "--tracer_entries",
            str(args.tracer_entries),
            "-o",
            str(output_file),
        ]
    )

    if args.mode == "client":
        cmd.append(str(args.script))
    elif args.mode == "server":
        cmd.extend(["-c", "from wave_monitor.window import start; start()"])
    else:
        raise ValueError(f"Unknown mode: {args.mode}")

    return cmd


if __name__ == "__main__":
    main()
