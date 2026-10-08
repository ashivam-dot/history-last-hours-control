"""Atlas in Numbers publisher: schedule the producer's parked Shorts on Buffer with keys only this job holds.

The producer (ashivam-dot/creature-receipts, `ytc.atlas.pipeline`) renders each Short on Modal, parks the MP4 in the
creature-receipts-outbox volume and commits content/atlas/<id>/ready.json with its hash. This job reads those
records from a checkout of the producer, fetches the exact bytes, and calls the producer's own `ytc.atlas.publish`
to host and schedule them. Each post is recorded in atlas/published.json, which the producer copies back.

Usage: python -m control.atlas PRODUCER_CHECKOUT [--list]
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE = Path(__file__).resolve().parents[1] / "atlas" / "published.json"
WORKSPACE = "aksha-shivam18"
VOLUME = "creature-receipts-outbox"
# Future Atlas posts kept in Buffer: a day of the producer's SLOTS, so a missed run costs no post.
AHEAD = 3
MAX_BYTES = 200 * 1024 * 1024


def _load_state() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}


def _save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _video(path: str, sha: str) -> bytes:
    import modal

    if modal.Workspace.from_context().hydrate().name != WORKSPACE:
        raise RuntimeError("the Modal token belongs to a different workspace")
    volume = modal.Volume.from_name(VOLUME, create_if_missing=False)
    data = bytearray()
    for chunk in volume.read_file(path):
        data += chunk
        if len(data) > MAX_BYTES:
            raise RuntimeError(f"{path} is larger than {MAX_BYTES} bytes")
    if hashlib.sha256(data).hexdigest() != sha or data[4:8] != b"ftyp":
        raise RuntimeError(f"{path} differs from the producer's record")
    return bytes(data)


def main(argv: list[str]) -> int:
    producer = Path(argv[0]).resolve()
    sys.path.insert(0, str(producer / "pipeline" / "src"))
    from ytc import publish as pub
    from ytc.atlas import publish as atlas
    from ytc.atlas.data import Dataset
    from ytc.atlas.episode import AtlasEpisode

    now = datetime.now(timezone.utc)
    queue = [p for p in pub.posts(since=now - timedelta(days=30)) if p["status"] not in ("sent", "error", "draft")]
    future = [p for p in queue if p.get("dueAt") and datetime.fromisoformat(p["dueAt"]) > now]
    report: dict = {"queue": [{"id": p["id"], "status": p["status"], "due": p.get("dueAt"),
                               "text": (p.get("text") or "")[:70]} for p in queue]}
    if "--list" in argv:
        print(json.dumps(report, indent=2))
        return 0
    state = _load_state()
    waiting = [p.parent for p in sorted((producer / "content" / "atlas").glob("atlas*/ready.json"))
               if not (p.parent / "publish.json").exists() and p.parent.name not in state]
    report |= {"future_posts": len(future), "waiting": [w.name for w in waiting], "scheduled": [], "failed": []}
    for folder in waiting[:max(AHEAD - len(future), 0)]:
        ready = json.loads((folder / "ready.json").read_text(encoding="utf-8"))
        try:
            if ready["id"] != folder.name or ready["modal_volume"] != VOLUME:
                raise RuntimeError("ready.json doesn't match its folder")
            ep = AtlasEpisode.load(folder / "atlas.yaml")
            ds = Dataset.load(folder / "data.json")
            with tempfile.TemporaryDirectory() as tmp:
                video = Path(tmp) / "short.mp4"
                video.write_bytes(_video(ready["modal_path"], ready["media_sha256"]))
                record = atlas.schedule(ep, Path(tmp), ds, video)
        except Exception as err:  # one bad Short mustn't stop the rest
            report["failed"].append({"id": folder.name, "error": f"{type(err).__name__}: {str(err)[:300]}"})
            continue
        state[folder.name] = record | {"media_sha256": ready["media_sha256"]}
        _save_state(state)
        report["scheduled"].append({"id": folder.name, "title": record["title"], "due_at": record["due_at"]})
    print(json.dumps(report, indent=2))
    return 1 if report["failed"] and not report["scheduled"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
