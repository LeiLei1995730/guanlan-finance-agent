import csv
import io
import json
import os
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime,timezone,timedelta
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse,JSONResponse,Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field
from .db import Store,now,encode
from .collect import Collector,public_url
from .research import query,integration_status


class Question(BaseModel):
    question:str=Field(min_length=1,max_length=500)
    actor:str=Field(default='local',max_length=100)

class Watch(BaseModel):
    name:str=Field(min_length=1,max_length=60)
    aliases:list[str]=Field(default_factory=list,max_length=20)
    industry:str=Field(default='金融科技',max_length=40)

class Source(BaseModel):
    name:str=Field(min_length=1,max_length=100)
    kind:str
    url:str=Field(max_length=2000)
    industry:str=Field(default='金融科技',max_length=40)
    company:str=Field(default='',max_length=60)
    selector:str=Field(default='a[href]',max_length=200)
    link_contains:str=Field(default='',max_length=200)
    content_selector:str=Field(default='',max_length=200)


def create_app(db_path=None):
    store=Store(db_path); collector=Collector(store)
    stop=threading.Event()
    @asynccontextmanager
    async def lifespan(app):
        interval=max(0,int(os.getenv('COLLECT_INTERVAL_MINUTES','0')))
        def scheduled():
            while not stop.wait(interval*60): collector.collect()
        if interval:
            threading.Thread(target=scheduled,daemon=True).start()
        yield
        stop.set()
        collector.fetcher.client.close()

    app=FastAPI(title='观澜金融研究助手',version='1.0.0',lifespan=lifespan)
    app.state.store=store; app.state.collector=collector
    static=Path(__file__).parent/'static'

    @app.middleware('http')
    async def local_boundary(request:Request,call_next):
        host=(request.headers.get('host','').split(':')[0]).lower()
        if host not in ('127.0.0.1','localhost','testserver'):
            return JSONResponse({'detail':'此版本仅供本机使用'},status_code=403)
        if request.method in ('POST','PUT','DELETE','PATCH'):
            origin=request.headers.get('origin')
            if request.headers.get('x-research-client')!='guanlan' or (origin and urlsplit(origin).netloc!=request.headers.get('host')):
                return JSONResponse({'detail':'请求来源校验失败'},status_code=403)
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
        return response

    @app.get('/')
    def home(): return FileResponse(static/'index.html')

    @app.get('/api/health')
    def health(): return {'status':'ok','version':'1.0.0','app':'guanlan'}

    @app.get('/api/dashboard')
    def dashboard():
        articles=store.articles(limit=5000)
        metrics=store.rows('SELECT * FROM metrics WHERE verified=1 ORDER BY period,company')
        cutoff=(datetime.now(timezone.utc)-timedelta(days=30)).isoformat()
        usage=store.rows("SELECT count(*) AS queries,count(DISTINCT actor) AS users,count(DISTINCT NULLIF(group_hash,'')) AS groups FROM usage WHERE kind='query' AND channel!='verification' AND created_at>=?",(cutoff,))[0]
        usage['pushes']=store.rows("SELECT count(*) n FROM usage WHERE kind='digest' AND created_at>=?",(cutoff,))[0]['n']
        return dict(articles=articles,metrics=metrics,watches=store.watches(),sources=store.sources(),runs=store.rows('SELECT r.*,s.name FROM runs r LEFT JOIN sources s ON r.source_id=s.id ORDER BY r.id DESC LIMIT 100'),usage=usage,integrations=integration_status(),collecting=collector.running,updated_at=now())

    @app.post('/api/collect')
    def collect(source_id:str|None=None):
        if source_id and not any(s['id']==source_id for s in store.sources()): raise HTTPException(404,'来源不存在')
        if collector.running: return {'status':'busy'}
        threading.Thread(target=collector.collect,args=(source_id,),daemon=True).start()
        return {'status':'started'}

    @app.post('/api/query')
    def ask(data:Question): return query(store,data.question,data.actor)

    @app.get('/api/digest')
    def digest(days:int=7): return query(store,f'最近{max(1,min(days,365))}天动态',use_ai=False,record=False)

    @app.post('/api/watches')
    def add_watch(data:Watch):
        if any(len(x)>100 for x in data.aliases): raise HTTPException(400,'别名过长')
        with store.conn() as c:
            c.execute('INSERT INTO watches(name,aliases,industry) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET aliases=excluded.aliases,industry=excluded.industry',(data.name,encode(data.aliases),data.industry))
        # Retag existing records when the watchlist changes.
        for a in store.articles(limit=10000):
            force=next((s['config'].get('company','') for s in store.sources() if s['id']==a['source_id']),'')
            with store.conn() as c:
                c.execute('UPDATE articles SET companies=? WHERE id=?',(encode(store.companies_for(a['title']+' '+a['body'],force)),a['id']))
        return {'status':'saved'}

    @app.delete('/api/watches/{watch_id}')
    def remove_watch(watch_id:int):
        with store.conn() as c: c.execute('DELETE FROM watches WHERE id=?',(watch_id,))
        return {'status':'deleted'}

    @app.post('/api/sources')
    def add_source(data:Source):
        if data.kind not in ('rss','html'): raise HTTPException(400,'支持 RSS 或 HTML 来源')
        try: public_url(data.url)
        except Exception: raise HTTPException(400,'请输入可访问的公开网址')
        if data.kind=='html' and not data.link_contains: raise HTTPException(400,'HTML 来源需要填写文章链接特征')
        sid='custom-'+uuid.uuid4().hex[:12]
        cfg={k:getattr(data,k) for k in ('company','selector','link_contains','content_selector')}
        with store.conn() as c:
            c.execute('INSERT INTO sources(id,name,kind,url,industry,config) VALUES(?,?,?,?,?,?)',(sid,data.name,data.kind,data.url,data.industry,encode(cfg)))
        return {'status':'saved','id':sid}

    @app.post('/api/sources/{source_id}/toggle')
    def toggle(source_id:str):
        with store.conn() as c: c.execute('UPDATE sources SET enabled=1-enabled WHERE id=?',(source_id,))
        return {'status':'saved'}

    @app.get('/api/export/{kind}')
    def export(kind:str):
        if kind=='articles':
            rows=store.articles(limit=10000); fields=['title','published_at','source_name','industry','category','url','summary']
        elif kind=='metrics':
            rows=store.rows('SELECT * FROM metrics WHERE verified=1 ORDER BY company,metric,period'); fields=['company','metric','period','period_type','value','unit','currency','source_url','evidence']
        else: raise HTTPException(404,'未知导出类型')
        out=io.StringIO(); writer=csv.DictWriter(out,fieldnames=fields); writer.writeheader()
        for row in rows:
            # Spreadsheet formula injection guard for externally supplied text.
            writer.writerow({k: ('\''+str(row.get(k,'')) if str(row.get(k,'')).startswith(('=','+','-','@')) else row.get(k,'')) for k in fields})
        return Response('\ufeff'+out.getvalue(),media_type='text/csv',headers={'Content-Disposition':f'attachment; filename="{kind}.csv"'})

    app.mount('/static',StaticFiles(directory=static),name='static')
    return app
