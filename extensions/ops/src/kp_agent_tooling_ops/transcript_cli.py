"""Write private sanitized evidence for explicitly selected host transcript calls."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from kp_agent_tooling._impl import leaf
from kp_agent_tooling_ops._impl.host_transcript_capture import extract_host_transcript_file
from kp_agent_tooling_ops._impl.trial_response_capture import CaptureError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transcript", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--call-id", action="append", required=True, dest="call_ids")
    parser.add_argument("--transport", default="native_mcp",
                        choices=("native_mcp", "facade_cli", "local_mcp"))
    args = parser.parse_args()
    if args.output.is_symlink():
        raise SystemExit("output directory must not be a symlink")
    if args.output.exists() and (not args.output.is_dir() or any(args.output.iterdir())):
        raise SystemExit("output directory must be absent or empty")
    bundle = extract_host_transcript_file(args.transcript, session_id=args.session_id,
                                          call_ids=args.call_ids, transport=args.transport)
    leaf.mkdir_private(args.output, parents=True)
    leaf.chmod_private(args.output, directory=True)
    captures = bundle.pop("captures")
    artifacts = []
    for index, capture in enumerate(captures, 1):
        path = args.output / f"call-{index:04d}.json"
        raw = json.dumps(capture, indent=2, sort_keys=True).encode() + b"\n"
        leaf.overwrite_private(path, raw)
        artifacts.append({"path": path.name, "sha256": hashlib.sha256(raw).hexdigest(),
                          "bytes": len(raw)})
    bundle["artifacts"] = artifacts
    manifest = args.output / "manifest.json"
    raw = json.dumps(bundle, indent=2, sort_keys=True).encode() + b"\n"
    leaf.overwrite_private(manifest, raw)
    print(json.dumps({"manifest": manifest.name, "sha256": hashlib.sha256(raw).hexdigest(),
                      "call_count": len(artifacts),
                      "extraction_sha256": bundle["extraction_sha256"]}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CaptureError, json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"capture refused: {exc}") from exc
