"""Offline control commands. All release switches remain disabled in tracked policy."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .release import Hold, _blob, candidate, episode_root, fetch_video, policy, publisher_preflight, read_object, sign
from .publisher import publish_reviewed

HERE = Path(__file__).resolve().parents[1]


def write_new(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Independent History release control")
    parser.add_argument("--policy", type=Path, default=HERE / "policy.json")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "sign", "publisher-preflight", "publish"):
        command = commands.add_parser(name)
        command.add_argument("--source", type=Path, required=True)
        command.add_argument("--commit", required=True)
        command.add_argument("--episode", required=True)
        if name == "prepare":
            command.add_argument("--output", type=Path, required=True)
        if name == "sign":
            command.add_argument("--approval", type=Path, required=True)
            command.add_argument("--output", type=Path, required=True)
        if name == "publisher-preflight":
            command.add_argument("--review", type=Path, required=True)
            command.add_argument("--public-key", type=Path, required=True)
        if name == "publish":
            command.add_argument("--review", type=Path, required=True)
            command.add_argument("--public-key", type=Path, required=True)
            command.add_argument("--due-at-utc", required=True)
            command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = policy(args.policy)
        if args.command == "prepare":
            hold = read_object(_blob(args.source, args.commit,
                                     episode_root(args.commit, args.episode) + "hold.json"), "committed hold")
            video = fetch_video(hold.get("media_url", ""), config)
            subject = candidate(args.source, args.commit, args.episode, video, config)
            args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
            write_new(args.output / f"{args.episode}.mp4", video)
            write_new(args.output / "subject.json", (json.dumps(subject, indent=2, ensure_ascii=False) + "\n").encode())
            print(f"Prepared exact media and review subject in {args.output}")
        elif args.command == "sign":
            if config["signing_enabled"] is not True:
                raise Hold("independent signing is disabled")
            approval = read_object(args.approval.read_bytes(), "independent approval")
            hold = read_object(_blob(args.source, args.commit,
                                     episode_root(args.commit, args.episode) + "hold.json"), "committed hold")
            subject = candidate(args.source, args.commit, args.episode,
                                fetch_video(hold.get("media_url", ""), config), config)
            result = sign(approval, subject, os.environ.get("HISTORY_REVIEW_SIGNING_KEY", ""), config)
            write_new(args.output, (json.dumps(result, indent=2, ensure_ascii=False) + "\n").encode())
            print(f"Signed exact candidate review in {args.output}")
        elif args.command == "publisher-preflight":
            review = read_object(args.review.read_bytes(), "signed review")
            plan = publisher_preflight(args.source, args.commit, args.episode, review,
                                       args.public_key.read_text(encoding="ascii").strip(), config)
            print(json.dumps(plan, sort_keys=True))
        else:
            review = read_object(args.review.read_bytes(), "signed review")
            receipt = publish_reviewed(
                args.source, args.commit, args.episode, review,
                args.public_key.read_text(encoding="ascii").strip(), config,
                os.environ.get("HISTORY_PUBLISHER_BUFFER_API_KEY", ""), args.due_at_utc)
            write_new(args.output, (json.dumps(receipt, indent=2, ensure_ascii=False) + "\n").encode())
            print(f"Control publisher receipt in {args.output}")
        return 0
    except (Hold, FileExistsError, FileNotFoundError) as exc:
        print(f"Control hold: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
