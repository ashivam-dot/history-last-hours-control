"""Atlas-only authenticated YouTube analytics, scheduled on Modal with private snapshots."""
from __future__ import annotations
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import modal

CHANNEL_ID = 'UC6e6OB3iw3yp8JnnBYxLItA'
LEDGER_URL = 'https://raw.githubusercontent.com/ashivam-dot/history-last-hours-control/main/atlas/published.json'
METRICS = 'views,engagedViews,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,likes,comments,shares,subscribersGained,subscribersLost'
app = modal.App('atlas-in-numbers-analytics')
image = modal.Image.debian_slim(python_version='3.12').pip_install('requests==2.32.5')
volume = modal.Volume.from_name('atlas-in-numbers-analytics', create_if_missing=True)
secret = modal.Secret.from_name('atlas-in-numbers-analytics-oauth')


def _read(session, endpoint, token, params):
    response = session.get(endpoint, params=params, headers={'Authorization': f'Bearer {token}'}, timeout=30)
    if response.status_code != 200:
        raise RuntimeError(f'YouTube API read failed with HTTP {response.status_code}')
    return response.json()


def collect(session, token, ledger, now):
    channel = _read(session, 'https://www.googleapis.com/youtube/v3/channels', token,
                    {'part': 'id,snippet,statistics', 'mine': 'true'})
    rows = channel.get('items', [])
    if len(rows) != 1 or rows[0].get('id') != CHANNEL_ID:
        raise RuntimeError('OAuth identity does not match the pinned Atlas channel')
    ids = []
    episodes = {}
    for episode_id, record in ledger.items():
        if record.get('status') != 'sent':
            continue
        import re
        match = re.fullmatch(r'https://(?:www\.)?youtube\.com/(?:shorts/([\w-]{11})|watch\?v=([\w-]{11}))', record.get('youtube_url') or '')
        if not match:
            continue
        video_id = match.group(1) or match.group(2)
        ids.append(video_id)
        episodes[video_id] = episode_id
    videos = []
    analytics_rows = []
    if ids:
        result = {'items': []}
        for start in range(0, len(ids), 50):
            batch = _read(session, 'https://www.googleapis.com/youtube/v3/videos', token,
                          {'part': 'snippet,statistics,status,contentDetails', 'id': ','.join(ids[start:start+50])})
            result['items'].extend(batch.get('items', []))
        for video in result.get('items', []):
            if video.get('snippet', {}).get('channelId') != CHANNEL_ID:
                raise RuntimeError('Published video belongs to another channel')
            videos.append({'id': episodes[video['id']], 'video_id': video['id'],
                           'title': video['snippet']['title'], 'published_at': video['snippet']['publishedAt'],
                           'statistics': video.get('statistics', {}),
                           'privacy_status': video.get('status', {}).get('privacyStatus'),
                           'upload_status': video.get('status', {}).get('uploadStatus')})
        for start in range(0, len(ids), 500):
            report = _read(session, 'https://youtubeanalytics.googleapis.com/v2/reports', token,
                           {'ids': 'channel==MINE', 'startDate': (now-timedelta(days=28)).date().isoformat(),
                            'endDate': now.date().isoformat(), 'metrics': METRICS,
                            'dimensions': 'video', 'filters': 'video=='+','.join(ids[start:start+500])})
            names = [header['name'] for header in report.get('columnHeaders', [])]
            analytics_rows.extend(dict(zip(names, row)) for row in report.get('rows', []))
    return {'collected_at': now.isoformat(), 'channel_id': CHANNEL_ID,
            'channel_title': rows[0]['snippet']['title'], 'channel_statistics': rows[0].get('statistics', {}),
            'authenticated': True, 'published_videos': videos, 'analytics_rows': analytics_rows,
            'analytics_pending': bool(ids) and not analytics_rows,
            'note': 'Analytics may lag new uploads; missing rows are not zero performance. Private volume stores these snapshots.'}


@app.function(image=image, cpu=0.25, memory=256, timeout=180,
              secrets=[secret], volumes={'/analytics': volume}, schedule=modal.Cron('42 */3 * * *'))
def snapshot() -> dict:
    import requests
    session = requests.Session()
    response = session.post('https://oauth2.googleapis.com/token', data={
        'client_id': os.environ['ATLAS_YOUTUBE_CLIENT_ID'],
        'client_secret': os.environ['ATLAS_YOUTUBE_CLIENT_SECRET'],
        'refresh_token': os.environ['ATLAS_YOUTUBE_REFRESH_TOKEN'], 'grant_type': 'refresh_token'}, timeout=30)
    if response.status_code != 200:
        raise RuntimeError(f'Atlas OAuth refresh failed with HTTP {response.status_code}')
    token = response.json()['access_token']
    response = session.get(LEDGER_URL, timeout=30)
    response.raise_for_status()
    result = collect(session, token, response.json(), datetime.now(timezone.utc))
    volume.reload()
    root = Path('/analytics')
    serialized = json.dumps(result, indent=2)+'\n'
    (root/'latest.json').write_text(serialized)
    (root/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.json')).write_text(serialized)
    volume.commit()
    return result


@app.function(image=image, cpu=0.25, memory=128, timeout=30, volumes={'/analytics': volume})
def latest() -> dict:
    volume.reload()
    return json.loads(Path('/analytics/latest.json').read_text())


@app.function(image=image, cpu=0.25, memory=128, timeout=30, volumes={'/analytics': volume})
def public_metrics() -> list[dict]:
    """Only public view/like counts for the producer; OAuth and private Analytics stay isolated."""
    volume.reload()
    result=json.loads(Path('/analytics/latest.json').read_text())
    return [{'video_id':v['video_id'],'title':v['title'],'published':v['published_at'],
             'views':int(v['statistics']['viewCount']) if 'viewCount' in v['statistics'] else None,
             'likes':int(v['statistics']['likeCount']) if 'likeCount' in v['statistics'] else None,
             'observed_at':result['collected_at'],'metric_source':'authenticated-data-api'}
            for v in result['published_videos'] if v.get('privacy_status')=='public']
