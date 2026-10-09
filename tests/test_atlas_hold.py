import json
import sys
from datetime import datetime, timedelta, timezone

from control import atlas as control

STUB_PUBLISH = """
import json, os
from pathlib import Path
LOG = Path(os.environ["ATLAS_TEST_LOG"])
def posts(since=None):
    return json.loads(os.environ["ATLAS_TEST_POSTS"])
def delete_post(post_id):
    LOG.write_text(LOG.read_text() + post_id + "\\n" if LOG.exists() else post_id + "\\n")
"""


def _producer(tmp_path):
    src = tmp_path / "producer" / "pipeline" / "src" / "ytc"
    (src / "atlas").mkdir(parents=True)
    (src / "__init__.py").write_text("")
    (src / "publish.py").write_text(STUB_PUBLISH)
    (src / "atlas" / "__init__.py").write_text("")
    for name, body in {"publish": "", "receipt": "", "data": "class Dataset: pass\n",
                       "episode": "class AtlasEpisode: pass\n"}.items():
        (src / "atlas" / f"{name}.py").write_text(body)
    waiting = tmp_path / "producer" / "content" / "atlas" / "atlas009"
    waiting.mkdir(parents=True)
    (waiting / "ready.json").write_text("{}")
    return tmp_path / "producer"


def test_hold_withdraws_future_posts_and_schedules_nothing(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    here = tmp_path / "atlas"
    here.mkdir()
    for name in ("STATE", "HEALTH", "RETRIES", "HOLD", "WITHDRAWN"):
        monkeypatch.setattr(control, name, here / getattr(control, name).name)
    control.HOLD.write_text(json.dumps({"reason": "format rework"}))
    control.STATE.write_text(json.dumps({
        "atlas007": {"buffer_post_id": "p7", "title": "Future", "due_at": "x"},
        "atlas006": {"buffer_post_id": "p6", "title": "Sent", "due_at": "y"},
    }))
    posts = [{"id": "p7", "status": "scheduled", "dueAt": (now + timedelta(hours=3)).isoformat()},
             {"id": "p6", "status": "sent", "dueAt": (now - timedelta(hours=3)).isoformat()}]
    monkeypatch.setenv("ATLAS_TEST_POSTS", json.dumps(posts))
    monkeypatch.setenv("ATLAS_TEST_LOG", str(tmp_path / "deleted.txt"))
    monkeypatch.setattr(control, "confirm_delivery", lambda record: {"confirmed": True})
    producer = _producer(tmp_path)
    for mod in [m for m in sys.modules if m == "ytc" or m.startswith("ytc.")]:
        monkeypatch.delitem(sys.modules, mod)

    assert control.main([str(producer)]) == 0

    assert (tmp_path / "deleted.txt").read_text().split() == ["p7"]
    state = json.loads(control.STATE.read_text())
    withdrawn = json.loads(control.WITHDRAWN.read_text())
    health = json.loads(control.HEALTH.read_text())
    assert list(state) == ["atlas006"]
    assert withdrawn["atlas007"]["status"] == "withdrawn"
    assert health["held"] == "format rework" and health["problem"] == "" and not health["errors"]


def test_old_format_shorts_wait_for_their_re_render(tmp_path, monkeypatch, capsys):
    here = tmp_path / "atlas"
    here.mkdir()
    for name in ("STATE", "HEALTH", "RETRIES", "HOLD", "WITHDRAWN"):
        monkeypatch.setattr(control, name, here / getattr(control, name).name)
    monkeypatch.setenv("ATLAS_TEST_POSTS", "[]")
    monkeypatch.setenv("ATLAS_TEST_LOG", str(tmp_path / "deleted.txt"))
    producer = _producer(tmp_path)
    (producer / "pipeline" / "src" / "ytc" / "atlas" / "receipt.py").write_text('FORMAT = "cinema-v2"\n')
    for mod in [m for m in sys.modules if m == "ytc" or m.startswith("ytc.")]:
        monkeypatch.delitem(sys.modules, mod)

    control.main([str(producer)])

    report = json.loads(capsys.readouterr().out)
    assert report["old_format"] == ["atlas009"] and report["failed"] == [] and report["scheduled"] == []
    assert json.loads(control.HEALTH.read_text())["errors"] == []
