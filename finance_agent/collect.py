import hashlib
import ipaddress
import json
import math
import re
import socket
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode
from urllib.robotparser import RobotFileParser

import feedparser
import httpx
from bs4 import BeautifulSoup
from .db import now, encode

USER_AGENT = 'GuanlanResearch/1.0 (+personal public-source research; respectful low-rate crawler)'
TRUSTED_FEED_HOSTS = {'www.gientech.com','www.federalreserve.gov','www.bis.org','www.ecb.europa.eu','api.fdic.gov'}


def public_url(url):
    p = urlsplit(url)
    if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or p.port not in (None,80,443):
        raise ValueError('仅允许公开 HTTP/HTTPS 地址，不允许凭据或自定义端口')
    ips = socket.getaddrinfo(p.hostname, p.port or 443, type=socket.SOCK_STREAM)
    def allowed(address):
        ip=ipaddress.ip_address(address)
        # Some local VPN clients map public names into the RFC 2544 fake-IP pool.
        # Only built-in official hosts may use that pool; RFC1918 and loopback
        # addresses remain blocked even for those hosts.
        fake_proxy=ip.version==4 and ip in ipaddress.ip_network('198.18.0.0/15') and p.hostname in TRUSTED_FEED_HOSTS
        return ip.is_global or fake_proxy
    if not ips or any(not allowed(x[4][0]) for x in ips):
        raise ValueError('不允许采集本机、内网或保留地址')
    return url


def canonical(url):
    p = urlsplit(url)
    query = [(k,v) for k,v in parse_qsl(p.query,keep_blank_values=True) if not k.lower().startswith('utm_') and k.lower() not in ('fbclid','gclid')]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or '/', urlencode(sorted(query)), ''))


class Fetcher:
    def __init__(self):
        self.client = httpx.Client(timeout=httpx.Timeout(20,connect=10), headers={'User-Agent':USER_AGENT}, follow_redirects=False)
        self.robots, self.last = {}, {}

    def raw(self, url):
        for _ in range(5):
            public_url(url)
            host = urlsplit(url).hostname
            time.sleep(max(0, .7-(time.monotonic()-self.last.get(host,0))))
            self.last[host] = time.monotonic()
            with self.client.stream('GET',url) as r:
                if r.is_redirect:
                    target = urljoin(url,r.headers.get('location',''))
                    if urlsplit(target).hostname != urlsplit(url).hostname:
                        raise ValueError('来源重定向到其他站点，请将最终公开网址单独配置为来源')
                    url = target
                    continue
                r.raise_for_status()
                chunks, total = [], 0
                for chunk in r.iter_bytes():
                    total += len(chunk)
                    if total>5_000_000: raise ValueError('响应超过 5 MB 限制')
                    chunks.append(chunk)
                return b''.join(chunks), str(r.url)
        raise ValueError('重定向次数超限')

    def get(self, url, robots=True):
        if robots:
            p=urlsplit(url); origin=f'{p.scheme}://{p.netloc}'
            if origin not in self.robots:
                parser=RobotFileParser()
                try:
                    body,_=self.raw(origin+'/robots.txt')
                    parser.parse(body.decode('utf-8',errors='replace').splitlines())
                except httpx.HTTPStatusError as e:
                    if e.response.status_code in (404,410): parser.parse([])
                    else: raise ValueError('无法确认 robots.txt 访问规则') from None
                self.robots[origin]=parser
            if not self.robots[origin].can_fetch('GuanlanResearch',url):
                raise ValueError('robots.txt 不允许采集此路径')
        return self.raw(url)


def date_value(text):
    m=re.search(r'(20\d{2})[年/.-](\d{1,2})[月/.-](\d{1,2})', text or '')
    if m:
        try: return datetime(*map(int,m.groups()),tzinfo=timezone.utc).isoformat()
        except ValueError: pass
    return None


def category(text):
    for label, words in [('财报业绩',['财报','业绩','净利润','earnings','financial results']),('政策监管',['监管','政策','regulat','monetary','supervis','policy']),('订单合作',['签约','合作','中标','partner','acquisition']),('产品技术',['平台','技术','产品','digital','technology'])]:
        if any(w.lower() in text.lower() for w in words): return label
    if re.search(r'(?<![a-z])ai(?![a-z])',text,re.I): return '产品技术'
    return '公司动态'


def text_from_html(html, selector=''):
    soup=BeautifulSoup(html,'html.parser')
    for node in soup.select('script,style,nav,header,footer,form,noscript'): node.decompose()
    main=soup.select_one(selector) if selector else None
    main=main or soup.select_one('article,main,.article-content,.content') or soup.body or soup
    return re.sub(r'\s+',' ',main.get_text(' ',strip=True)).strip()[:24000]


def fdic_metrics(payload, source):
    """FDIC bank entities, not consolidated listed holding companies. NETINC is YTD."""
    params={'filters':f"CERT:{source['config']['cert']}",'fields':'REPDTE,NAME,ASSET,DEP,NETINC,ROA','sort_by':'REPDTE','sort_order':'DESC','limit':12,'format':'json'}
    url=source['url']+'?'+urlencode(params)
    result=[]
    for entry in payload.get('data',[]):
        r=entry.get('data',{})
        raw=str(r.get('REPDTE',''))
        if not re.fullmatch(r'20\d{6}',raw): continue
        period=f'{raw[:4]}-{raw[4:6]}-{raw[6:]}'
        for key,label,unit,ptype in [('ASSET','总资产','千美元','期末'),('DEP','存款','千美元','期末'),('NETINC','净利润','千美元','年初至今累计'),('ROA','资产回报率','%','年初至今年化')]:
            value=r.get(key)
            if value is None: continue
            value=float(value)
            if not math.isfinite(value): continue
            result.append(dict(source_id=source['id'], company=source['config']['company'],metric=label,period=period,period_type=ptype,value=value,unit=unit,currency='USD' if key!='ROA' else '',source_url=url,evidence=f"FDIC CERT {source['config']['cert']} | {r.get('NAME','')} | {key}={value} | REPDTE={raw}",verified=1))
    return result


class Collector:
    def __init__(self,store,fetcher=None):
        self.store=store; self.fetcher=fetcher or Fetcher(); self.lock=threading.Lock(); self.running=False

    def collect(self, source_id=None):
        if not self.lock.acquire(blocking=False): return {'status':'busy'}
        self.running=True
        try:
            results=[]
            for source in self.store.sources():
                if not source['enabled'] or (source_id and source['id']!=source_id): continue
                counts=dict(added=0,updated=0,unchanged=0,metrics=0)
                with self.store.conn() as c:
                    rid=c.execute("INSERT INTO runs(source_id,started_at,status) VALUES(?,?,'running')",(source['id'],now())).lastrowid
                try:
                    if source['kind']=='fdic':
                        cfg=source['config']
                        query=urlencode({'filters':f"CERT:{cfg['cert']}",'fields':'REPDTE,NAME,ASSET,DEP,NETINC,ROA','sort_by':'REPDTE','sort_order':'DESC','limit':12,'format':'json'})
                        body,_=self.fetcher.get(source['url']+'?'+query,robots=False)
                        metrics=fdic_metrics(json.loads(body),source)
                        if not metrics: raise ValueError('来源没有返回有效财务指标')
                        for item in metrics: self.store.upsert_metric(item)
                        counts['metrics']=len(metrics)
                    else:
                        articles=self.read_source(source)
                        if not articles: raise ValueError('未找到有效条目，请检查来源或选择器')
                        for article in articles:
                            counts[self.store.upsert_article(article)] += 1
                    status,error='success',''
                except Exception as e:
                    status='failed'
                    error=f'HTTP {e.response.status_code}' if isinstance(e,httpx.HTTPStatusError) else (str(e) if isinstance(e,ValueError) else type(e).__name__)
                with self.store.conn() as c:
                    c.execute('UPDATE runs SET finished_at=?,status=?,added=?,updated=?,unchanged=?,metrics=?,error=? WHERE id=?',(now(),status,*counts.values(),error,rid))
                results.append(dict(source_id=source['id'],status=status,error=error,**counts))
            return {'status':'complete','results':results}
        finally:
            self.running=False; self.lock.release()

    def read_source(self, source):
        body,base=self.fetcher.get(source['url'])
        cfg=source['config']; items=[]
        if source['kind']=='rss':
            feed=feedparser.parse(body)
            for e in feed.entries[:30]:
                url=e.get('link','')
                if urlsplit(url).scheme not in ('http','https'): continue
                text=text_from_html(e.get('summary','') or e.get('description',''))
                parsed=e.get('published_parsed') or e.get('updated_parsed')
                published=datetime(*parsed[:6],tzinfo=timezone.utc).isoformat() if parsed else None
                items.append((e.get('title','').strip(),canonical(url),text,published))
        else:
            soup=BeautifulSoup(body,'html.parser'); seen=set()
            for a in soup.select(cfg.get('selector','a[href]')):
                url=canonical(urljoin(base,a.get('href','')))
                label=a.get_text(' ',strip=True)
                if url in seen or urlsplit(url).hostname!=urlsplit(base).hostname or cfg.get('link_contains','') not in url or len(label)<10: continue
                seen.add(url)
                published=date_value(label)
                title=re.sub(r'^\d{4}[/.-]\d{1,2}[/.-]\d{1,2}\s*','',label).removesuffix(' MORE').strip()
                cached=self.store.rows('SELECT * FROM articles WHERE url=?',(url,))
                if cached and cached[0]['fetched_at'][:10]==now()[:10]:
                    old=cached[0]; items.append((old['title'],url,old['body'],old['published_at']))
                else:
                    detail,_=self.fetcher.get(url)
                    doc=BeautifulSoup(detail,'html.parser')
                    head=doc.select_one('h1,h2.m3nLt')
                    if head: title=head.get_text(' ',strip=True)
                    text=text_from_html(detail,cfg.get('content_selector',''))
                    published=published or date_value(doc.get_text(' ',strip=True))
                    items.append((title,url,text,published))
                if len(items)>=12: break
        result=[]
        for title,url,text,published in items:
            if not title: continue
            text=text or title
            result.append(dict(source_id=source['id'],title=title[:500],url=url,body=text,summary=text[:280],category=category(title),industry=source['industry'],companies=self.store.companies_for(title+' '+text,cfg.get('company','')),published_at=published))
        return result
