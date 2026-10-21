from pathlib import Path
from types import SimpleNamespace

from control import atlas
from control.atlas import ordered


def test_two_posts_ahead_for_two_slots_a_day():
    assert atlas.AHEAD == 2


def test_waiting_shorts_follow_the_producers_order_after_the_last_post(tmp_path):
    waiting = [tmp_path / "atlas081", tmp_path / "atlas082"]
    seen = {}
    def order(folders, last):
        seen.update(folders=folders, last=last)
        return list(reversed(folders))
    state = {"atlas079": {"due_at": "2026-10-22T12:00:00-04:00"}, "atlas080": {"due_at": "2026-10-22T19:00:00-04:00"},
             "atlas078": {}}
    out = ordered(waiting, SimpleNamespace(order=order), state, tmp_path)
    assert out == [tmp_path / "atlas082", tmp_path / "atlas081"]
    assert seen["last"] == tmp_path / "content" / "atlas" / "atlas080"


def test_name_order_without_a_producer_order_or_when_it_fails(tmp_path):
    waiting = [Path("atlas081"), Path("atlas082")]
    assert ordered(waiting, SimpleNamespace(), {}, tmp_path) == waiting
    def broken(folders, last):
        raise ValueError("bad ready.json")
    assert ordered(waiting, SimpleNamespace(order=broken), {}, tmp_path) == waiting
    got = {}
    ordered(waiting, SimpleNamespace(order=lambda f, last: got.update(last=last) or f), {}, tmp_path)
    assert got["last"] is None
