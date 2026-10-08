"""Create sanitized raw call artifacts and a compact manifest from JSONL exports."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys

from kp_agent_tooling._impl import leaf
from kp_agent_tooling_ops._impl.trial_response_capture import CaptureError, capture_call, compact_summary

MAX_INPUT_LINE_BYTES = 4_000_000
MAX_CALLS = 64


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="JSONL file, or - for stdin")
    parser.add_argument("output", type=Path)
    parser.add_argument("--transport", required=True, choices=("native_mcp", "facade_cli", "local_mcp"))
    args = parser.parse_args()
    if args.output.is_symlink():
        raise SystemExit("output directory must not be a symlink")
    if args.output.exists() and (not args.output.is_dir() or any(args.output.iterdir())):
        raise SystemExit("output directory must be absent or empty")
    stream = sys.stdin.buffer if args.input == "-" else open(args.input, "rb")
    try:
        records = []
        while True:
            line = stream.readline(MAX_INPUT_LINE_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_INPUT_LINE_BYTES:
                raise CaptureError("input record exceeds limit")
            if line.strip():
                records.append(json.loads(line.decode("utf-8")))
            if len(records) > MAX_CALLS:
                raise CaptureError("too many capture records")
    finally:
        if args.input != "-":
            stream.close()
    if not records:
        raise CaptureError("no capture records")
    captures = [capture_call(record, transport=args.transport) for record in records]
    leaf.mkdir_private(args.output, parents=True, exist_ok=True)
    leaf.chmod_private(args.output, directory=True)
    artifacts = []
    for index, capture in enumerate(captures, 1):
        path = args.output / f"call-{index:04d}.json"
        raw = json.dumps(capture, indent=2, sort_keys=True).encode() + b"\n"
        leaf.overwrite_private(path, raw)
        artifacts.append({"path": path.name, "sha256": hashlib.sha256(raw).hexdigest(),
                          "bytes": len(raw), "summary": compact_summary(capture)})
    manifest = {"schema": "ops.trial-response-capture-manifest.v1", "transport": args.transport,
                "call_count": len(artifacts), "artifacts": artifacts}
    manifest_path = args.output / "manifest.json"
    leaf.overwrite_private(manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CaptureError, json.JSONDecodeError) as exc:
        raise SystemExit(f"capture refused: {exc}") from exc
