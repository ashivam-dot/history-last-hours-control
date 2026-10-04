"""Offline control commands. All release switches remain disabled in tracked policy."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .release import Hold, _blob, candidate, episode_root, fetch_video, policy, publisher_preflight, read_object, sign
from .publisher import publish_draft_reviewed, publish_reviewed
from .intake import intake_draft, validated_packet
from .qa import qa_draft, verified_qa_report

HERE = Path(__file__).resolve().parents[1]


def write_new(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Independent History release control")
    parser.add_argument("--policy", type=Path, default=HERE / "policy.json")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "intake", "qa-draft", "sign", "sign-draft", "sign-qa-draft",
                 "publisher-preflight",
                 "publish", "publish-draft"):
        command = commands.add_parser(name)
        command.add_argument("--source", type=Path, required=True)
        command.add_argument("--commit", required=True)
        command.add_argument("--episode", required=True)
        if name in ("prepare", "intake"):
            command.add_argument("--output", type=Path, required=True)
        if name in ("sign", "sign-draft", "sign-qa-draft"):
            command.add_argument("--approval", type=Path, required=True)
            command.add_argument("--output", type=Path, required=True)
        if name in ("qa-draft", "sign-draft", "sign-qa-draft", "publish-draft"):
            command.add_argument("--packet", type=Path, required=True)
        if name == "sign-qa-draft":
            command.add_argument("--qa-report", type=Path, required=True)
        if name == "qa-draft":
            command.add_argument("--report", type=Path, required=True)
            command.add_argument("--approval", type=Path, required=True)
        if name == "publisher-preflight":
            command.add_argument("--review", type=Path, required=True)
            command.add_argument("--public-key", type=Path, required=True)
        if name in ("publish", "publish-draft"):
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
        elif args.command == "intake":
            intake_draft(args.source, args.commit, args.episode, config,
                         os.environ.get("HISTORY_INTAKE_CLOUDINARY_URL", ""), args.output)
            print(f"Prepared control-owned draft review packet in {args.output}")
        elif args.command == "qa-draft":
            qa_draft(args.source, args.commit, args.episode, args.packet, config,
                     args.report, args.approval)
            print(f"Independent automated QA passed; private report in {args.report}")
        elif args.command in ("sign", "sign-draft", "sign-qa-draft"):
            if config["signing_enabled"] is not True:
                raise Hold("independent signing is disabled")
            approval = read_object(args.approval.read_bytes(), "independent approval")
            if args.command in ("sign-draft", "sign-qa-draft"):
                subject, _ = validated_packet(args.source, args.commit, args.episode,
                                              config, args.packet)
                if args.command == "sign-qa-draft":
                    report = read_object(args.qa_report.read_bytes(), "private QA report")
                    verified_qa_report(report, approval, subject, config, args.packet)
            else:
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
            publisher = publish_draft_reviewed if args.command == "publish-draft" else publish_reviewed
            before = (args.packet,) if args.command == "publish-draft" else ()
            receipt = publisher(
                args.source, args.commit, args.episode, *before, review,
                args.public_key.read_text(encoding="ascii").strip(), config,
                os.environ.get("HISTORY_PUBLISHER_BUFFER_API_KEY", ""),
                os.environ.get("HISTORY_PUBLISHER_CLOUDINARY_URL", ""), args.due_at_utc)
            write_new(args.output, (json.dumps(receipt, indent=2, ensure_ascii=False) + "\n").encode())
            print(f"Control publisher receipt in {args.output}")
        return 0
    except (Hold, FileExistsError, FileNotFoundError) as exc:
        print(f"Control hold: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
