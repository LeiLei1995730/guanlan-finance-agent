import json
import socket
from datetime import datetime,timezone
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from finance_agent.db import Store,now
from finance_agent.collect import canonical,public_url,fdic_metrics,Collector,date_value,category
from finance_agent.research import query,model_answer
from finance_agent.feishu import accepted_message
from finance_agent.app import create_app

@pytest.fixture
def store(tmp_path): return Store(tmp_path/'test.sqlite')

def article(url='https://example.com/a',body='中电金信与银行签署合作协议。',industry='金融科技',title='中电金信合作公告'):
    return dict(source_id='gientech',title=title,url=url,body=body,summary=body,category='订单合作',industry=industry,companies=['中电金信'] if industry=='金融科技' else [],published_at=now())

def test_dedup_and_version_history(store):
    a=article(); assert store.upsert_article(a)=='added'
    assert store.upsert_article(a)=='unchanged'
    a['body']='修订后的合作条款';assert store.upsert_article(a)=='updated'
    assert len(store.articles())==1
    assert len(store.rows('SELECT * FROM versions'))==2

def test_tracking_params_removed():
    assert canonical('https://EXAMPLE.com/a?utm_source=x&id=2#part')=='https://example.com/a?id=2'

def test_classification_uses_event_not_incidental_word():
    assert category('中电金信召开专题工作会议')=='公司动态'
    assert category('Chair delivers remarks')=='公司动态'
    assert category('AI原生软件工厂发布')=='产品技术'
    assert category('银行公布第三季度业绩')=='财报业绩'

def test_official_sdk_request_contract():
    from lark_oapi.api.im.v1 import ReplyMessageRequest,ReplyMessageRequestBody,CreateMessageRequest,CreateMessageRequestBody
    body=ReplyMessageRequestBody.builder().msg_type('text').content('{"text":"fixture"}').uuid('test-idempotency-key').build()
    reply=ReplyMessageRequest.builder().message_id('om_fixture').request_body(body).build()
    assert reply.body.uuid=='test-idempotency-key'
    digest=CreateMessageRequestBody.builder().receive_id('oc_fixture').msg_type('text').content('{"text":"fixture"}').uuid('test-digest').build()
    request=CreateMessageRequest.builder().receive_id_type('chat_id').request_body(digest).build()
    assert request.body.uuid=='test-digest'

@pytest.mark.parametrize('url',['http://localhost/a','http://127.0.0.1','https://10.0.0.1/a','http://169.254.169.254','https://user:pass@example.com','file:///etc/passwd','https://example.com:22'])
def test_private_urls_blocked(url):
    with pytest.raises((ValueError,socket.gaierror)): public_url(url)

def test_proxy_pool_only_for_builtin_hosts(monkeypatch):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(2,1,6,'',('198.18.6.1',443))])
    assert public_url('https://www.gientech.com/news.html')
    with pytest.raises(ValueError):public_url('https://unknown.example/a')
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(2,1,6,'',('10.0.0.1',443))])
    with pytest.raises(ValueError):public_url('https://www.gientech.com/news.html')

def test_fdic_units_period_and_zero(store):
    source=next(s for s in store.sources() if s['id']=='fdic-jpm')
    rows=fdic_metrics({'data':[{'data':{'REPDTE':'20260630','ASSET':100000,'NETINC':0,'ROA':None}}]},source)
    assert len(rows)==2
    assert rows[0]['value']==100000 and rows[0]['unit']=='千美元'
    assert rows[1]['value']==0 and rows[1]['period_type']=='年初至今累计'
    for r in rows:store.upsert_metric(r);store.upsert_metric(r)
    assert len(store.rows('SELECT * FROM metrics'))==2

def test_watch_delete_persists_across_restart(store):
    with store.conn() as c:c.execute("DELETE FROM watches WHERE name='中电金信'")
    assert not any(w['name']=='中电金信' for w in Store(store.path).watches())

def test_query_evidence_and_absence(store,monkeypatch):
    monkeypatch.delenv('LLM_BASE_URL',raising=False)
    store.upsert_article(article())
    result=query(store,'中电金信最近7天动态')
    assert result['mode']=='extractive' and result['sources'][0]['url']=='https://example.com/a'
    result=query(store,'不存在的公司最近7天动态')
    assert result['mode']=='no_evidence' and not result['sources']
    result=query(store,'招商银行总资产')
    assert result['mode']=='no_evidence'

def test_digest_covers_both_industries(store):
    store.upsert_article(article())
    store.upsert_article(article('https://example.com/b','银行监管公告','银行','监管公告'))
    assert len(query(store,'银行与金融科技最近7天动态',use_ai=False)['sources'])==2

def test_model_budget_and_unsupported_numbers(store,monkeypatch):
    monkeypatch.setenv('LLM_BASE_URL','https://model.example/v1');monkeypatch.setenv('LLM_MODEL','test');monkeypatch.setenv('LLM_DAILY_LIMIT','1')
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'choices':[{'message':{'content':'收入增长了999%。[1]'}}]}
    class Client:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def post(self,*a,**k):return Response()
    monkeypatch.setattr('finance_agent.research.httpx.Client',Client)
    refs=[{'title':'公告','published_at':None,'text':'收入为10亿元。'}]
    assert model_answer(store,'收入',refs,'fallback')[0]=='fallback'
    assert '额度' in model_answer(store,'收入',refs,'fallback')[2]

def test_outbox_dedup_retry(store):
    assert store.enqueue('event:1',{'question':'新闻'})
    assert not store.enqueue('event:1',{'question':'新闻'})
    job=store.claim_job();assert job['attempts']==1
    assert store.claim_job() is None
    store.finish_job(job,False,'Timeout')
    assert store.rows('SELECT status FROM jobs')[0]['status']=='queued'
    with store.conn() as c:c.execute("UPDATE jobs SET due_at='2000-01-01'")
    job=store.claim_job();assert job['attempts']==2
    store.finish_job(job,True)
    assert store.claim_job() is None
    assert store.rows('SELECT payload FROM jobs')[0]['payload']=='{}'

def test_feishu_scope_and_dedup_payload():
    event={'sender':{'sender_type':'user','sender_id':{'open_id':'u1'}},'message':{'message_type':'text','chat_id':'allowed','chat_type':'group','mentions':[{}],'message_id':'m1','content':json.dumps({'text':'@_user_1 中电金信动态'})}}
    assert accepted_message(event,{'allowed'})['question']=='中电金信动态'
    assert accepted_message(event,{'different'}) is None
    event['message']['mentions']=[];assert accepted_message(event,{'allowed'}) is None
    event['message']['chat_type']='p2p';assert accepted_message(event,{'allowed'})
    event['sender']['sender_type']='app';assert accepted_message(event,{'allowed'}) is None

def test_failed_source_does_not_stop_others(store):
    class Fetch:
        def get(self,url,robots=True):
            if 'gientech' in url:raise ValueError('fixture failure')
            if 'fdic.gov' in url:return json.dumps({'data':[{'data':{'REPDTE':'20260630','ASSET':0}}]}).encode(),url
            return b'<rss><channel><item><title>Policy update</title><link>https://example.com/update</link><description>Regulatory news</description></item></channel></rss>',url
    result=Collector(store,Fetch()).collect()
    assert result['results'][0]['status']=='failed'
    assert any(r['status']=='success' for r in result['results'][1:])
    assert len(store.rows('SELECT * FROM metrics'))==3

def test_app_boundaries_crud_export(tmp_path):
    app=create_app(tmp_path/'web.sqlite');headers={'X-Research-Client':'guanlan'}
    with TestClient(app) as c:
        assert c.get('/').status_code==200
        assert c.get('/api/health').json()['status']=='ok'
        assert c.post('/api/query',json={'question':'x'}).status_code==403
        assert c.post('/api/query',json={'question':'x'},headers={**headers,'Origin':'https://evil.example'}).status_code==403
        assert c.get('/api/health',headers={'Host':'evil.example'}).status_code==403
        assert c.post('/api/watches',json={'name':'测试银行','aliases':['Test Bank']},headers=headers).status_code==200
        dash=c.get('/api/dashboard').json();assert any(w['name']=='测试银行' for w in dash['watches'])
        assert c.post('/api/query',json={'question':'未知公司'},headers=headers).json()['mode']=='no_evidence'
        app.state.store.upsert_article(article(title='=HYPERLINK("evil")'))
        export=c.get('/api/export/articles');assert export.status_code==200 and "'=HYPERLINK" in export.text
        assert c.get('/api/export/private').status_code==404
