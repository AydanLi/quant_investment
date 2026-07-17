from __future__ import annotations

import argparse

from research.protocol import build_protocol


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create an immutable v3 core-strategy preregistration."
    )
    parser.add_argument("--version", required=True)
    parser.add_argument("--code-commit", required=True)
    parser.add_argument("--dataset-snapshot-id", required=True, type=int)
    parser.add_argument("--universe-version", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    protocol = build_protocol(
        protocol_version=args.version,
        code_commit=args.code_commit,
        dataset_snapshot_id=args.dataset_snapshot_id,
        universe_version=args.universe_version,
    )
    destination = protocol.write_once(args.output)
    print(f"Created immutable protocol {destination} ({protocol.content_hash}).")


if __name__ == "__main__":
    main()
