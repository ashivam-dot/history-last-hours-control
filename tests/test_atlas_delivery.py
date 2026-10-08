from control.atlas import confirm_delivery


class Response:
    def raise_for_status(self): pass
    def json(self):
        return {'title': 'Expected', 'author_url': 'https://www.youtube.com/@AtlasInNumbers'}


def test_confirmation_requires_exact_video_channel_and_title():
    record = {'youtube_url': 'https://www.youtube.com/shorts/abcdefghijk', 'title': 'Expected'}
    assert confirm_delivery(record, lambda *a, **k: Response())['confirmed']
    assert not confirm_delivery(record | {'title': 'Different'}, lambda *a, **k: Response())['confirmed']
    assert not confirm_delivery(record | {'youtube_url': 'https://example.com/abcdefghijk'})['confirmed']
