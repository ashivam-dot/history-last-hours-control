"""Atlas in Numbers publisher: schedule the producer's parked Shorts on Buffer with keys only this job holds.

The producer (ashivam-dot/creature-receipts, `ytc.atlas.pipeline`) renders each Short on Modal, parks the MP4 in the
creature-receipts-outbox volume and commits content/atlas/<id>/ready.json with its hash. This job reads those
records from a checkout of the producer, fetches the exact bytes, and calls the producer's own `ytc.atlas.publish`
to host and schedule them. Each post is recorded in atlas/published.json, which the producer copies back, and its
status is refreshed from Buffer on every run. A post Buffer failed to send is deleted and scheduled again, at most
MAX_RETRIES times. atlas/health.json tells the producer's watchdog how the last run went.

The run fails (and GitHub emails the owner) when nothing is queued and nothing is waiting: the producer has stopped.

Usage: python -m control.atlas PRODUCER_CHECKOUT [--list]
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parents[1] / "atlas"
STATE = HERE / "published.json"
HEALTH = HERE / "health.json"
RETRIES = HERE / "retries.json"
WORKSPACE = "aksha-shivam18"
VOLUME = "creature-receipts-outbox"
# Future Atlas posts kept in Buffer: a day of the producer's SLOTS, so a missed run costs no post.
AHEAD = 3
MAX_RETRIES = 2
MAX_BYTES = 200 * 1024 * 1024


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
    posts = {p["id"]: p for p in pub.posts(since=now - timedelta(days=30))}
    queue = [p for p in posts.values() if p["status"] not in ("sent", "error", "draft")]
    future = [p for p in queue if p.get("dueAt") and datetime.fromisoformat(p["dueAt"]) > now]
    report: dict = {"queue": [{"id": p["id"], "status": p["status"], "due": p.get("dueAt"),
                               "text": (p.get("text") or "")[:70]} for p in queue]}
    if "--list" in argv:
        print(json.dumps(report, indent=2))
        return 0

    state, retries = _load(STATE), _load(RETRIES)
    errors = []
    for episode_id, record in list(state.items()):
        post = posts.get(record.get("buffer_post_id"))
        if post is None:
            continue
        record.update({"status": post["status"], "youtube_url": post.get("externalLink") or record.get("youtube_url")})
        if post.get("sentAt"):
            record["sent_at"] = post["sentAt"]
        if post["status"] == "error":
            message = (post.get("error") or {}).get("message") or "unknown"
            if retries.get(episode_id, 0) < MAX_RETRIES:
                pub.delete_post(post["id"])
                retries[episode_id] = retries.get(episode_id, 0) + 1
                del state[episode_id]
            else:
                errors.append({"id": episode_id, "error": message})
    _save(STATE, state)
    _save(RETRIES, retries)

    waiting = [p.parent for p in sorted((producer / "content" / "atlas").glob("atlas*/ready.json"))
               if p.parent.name not in state]
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
        _save(STATE, state)
        report["scheduled"].append({"id": folder.name, "title": record["title"], "due_at": record["due_at"]})

    starving = not future and not report["scheduled"]
    problem = ("Nothing is queued in Buffer and the producer has no Short waiting: check Modal atlas_run."
               if starving else "")
    errors += report["failed"]
    _save(HEALTH, {"checked_at": now.isoformat(timespec="seconds"), "future_posts": len(future) + len(report["scheduled"]),
                   "waiting": len(waiting) - len(report["scheduled"]), "errors": errors, "problem": problem})
    print(json.dumps(report | {"problem": problem, "errors": errors}, indent=2))
    return 1 if starving or (report["failed"] and not report["scheduled"]) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
