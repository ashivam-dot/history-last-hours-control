from datetime import datetime,timezone
import pytest
from apps.atlas_analytics import CHANNEL_ID,collect

class Response:
    status_code=200
    def __init__(self,data):self.data=data
    def json(self):return self.data

class Session:
    def __init__(self,channel=CHANNEL_ID,video_channel=CHANNEL_ID,rows=None):
        self.channel=channel;self.video_channel=video_channel;self.rows=[] if rows is None else rows;self.calls=[]
    def get(self,url,**kwargs):
        self.calls.append(url)
        if url.endswith('/channels'):
            return Response({'items':[{'id':self.channel,'snippet':{'title':'Atlas in Numbers'}}]})
        if url.endswith('/videos'):
            return Response({'items':[{'id':'BCmvNfA7VnM','snippet':{'channelId':self.video_channel,'title':'137 Places','publishedAt':'2026-10-08T16:00:55Z'},'status':{'privacyStatus':'public','uploadStatus':'processed'},'statistics':{'viewCount':'67'}}]})
        return Response({'columnHeaders':[{'name':'video'},{'name':'views'}],'rows':self.rows})

LEDGER={'atlas001':{'status':'sent','youtube_url':'https://www.youtube.com/shorts/BCmvNfA7VnM'},'atlas002':{'status':'scheduled','youtube_url':None}}
NOW=datetime(2026,10,8,19,tzinfo=timezone.utc)

def test_rejects_other_oauth_channel_before_video_reads():
    session=Session(channel='other-channel')
    with pytest.raises(RuntimeError,match='pinned Atlas channel'):collect(session,'test-token',LEDGER,NOW)
    assert len(session.calls)==1

def test_rejects_video_from_another_channel():
    with pytest.raises(RuntimeError,match='another channel'):collect(Session(video_channel='other-channel'),'test-token',LEDGER,NOW)

def test_empty_analytics_is_pending_instead_of_zero_performance():
    result=collect(Session(),'test-token',LEDGER,NOW)
    assert result['analytics_pending'] is True
    assert result['analytics_rows']==[]
    assert result['published_videos'][0]['privacy_status']=='public'
    assert len(result['published_videos'])==1

def test_report_rows_use_api_column_names():
    result=collect(Session(rows=[['BCmvNfA7VnM',67]]),'test-token',LEDGER,NOW)
    assert result['analytics_pending'] is False
    assert result['analytics_rows']==[{'video':'BCmvNfA7VnM','views':67}]

def test_channel_growth_keeps_data_api_requests_within_fifty_ids():
    class BatchingSession(Session):
        def get(self,url,**kwargs):
            if url.endswith('/videos'):
                ids=kwargs['params']['id'].split(',')
                assert len(ids)<=50
                self.calls.append(url)
                return Response({'items':[{'id':video_id,'snippet':{'channelId':CHANNEL_ID,'title':'Atlas','publishedAt':'2026-10-08T16:00:55Z'}} for video_id in ids]})
            return super().get(url,**kwargs)
    ledger={f'atlas{i:03}':{'status':'sent','youtube_url':f'https://www.youtube.com/shorts/{i:011}'} for i in range(51)}
    result=collect(BatchingSession(),'test-token',ledger,NOW)
    assert len(result['published_videos'])==51
