"""Bounded public-response cache, conditional requests and per-host pacing. No credentials."""
import hashlib, ipaddress, json, os, sqlite3, threading, time, urllib.error, urllib.parse, urllib.request, zlib
from pathlib import Path
from collector.config import user_agent

UA=user_agent(); DEADLINE=float('inf'); CACHE=Path('.cache/http.sqlite')
_lock=threading.Lock(); _last={}; _blocked={}; _init=False
STATS={'requests':0,'revalidated':0,'failures':0}

def public_url(value):
    if not isinstance(value,str):return False
    try:p=urllib.parse.urlsplit(value)
    except ValueError:return False
    if p.scheme not in ('https','http') or not p.hostname or p.username or p.password:return False
    if p.hostname in ('localhost','metadata.google.internal') or p.hostname.endswith(('.local','.internal')):return False
    try:return ipaddress.ip_address(p.hostname).is_global
    except ValueError:return '.' in p.hostname

def db():
    global _init
    CACHE.parent.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(CACHE,timeout=10)
    with _lock:
        if not _init:
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('CREATE TABLE IF NOT EXISTS responses(k TEXT PRIMARY KEY,etag TEXT,modified TEXT,body BLOB,at REAL)')
            c.commit();_init=True
    return c

def request(url,body=None,timeout=12,retries=1):
    if not public_url(url) or time.monotonic()>=DEADLINE:return 0,url,b''
    host=urllib.parse.urlsplit(url).netloc
    with _lock:
        if _blocked.get(host,0)>time.time():return 429,url,b''
        at=max(time.monotonic(),_last.get(host,0)+.3);_last[host]=at
    if at>=DEADLINE:return 0,url,b''
    time.sleep(max(0,at-time.monotonic()))
    encoded=None if body is None else json.dumps(body,separators=(',',':')).encode()
    key=hashlib.sha256(url.encode()+(encoded or b'')).hexdigest();cached=None
    if body is None:
        with db() as c:cached=c.execute('SELECT etag,modified,body FROM responses WHERE k=?',(key,)).fetchone()
    headers={'User-Agent':UA,'Accept':'application/json,text/html,application/xml,*/*'}
    if encoded is not None:headers['Content-Type']='application/json'
    if cached:
        if cached[0]:headers['If-None-Match']=cached[0]
        if cached[1]:headers['If-Modified-Since']=cached[1]
    for attempt in range(min(2,retries+1)):
        if time.monotonic()>=DEADLINE:return 0,url,b''
        try:
            STATS['requests']+=1
            with urllib.request.urlopen(urllib.request.Request(url,data=encoded,headers=headers),timeout=max(1,min(timeout,12,DEADLINE-time.monotonic()))) as r:
                data=r.read(12_000_001)
                if len(data)>12_000_000:return 0,url,b''
                if body is None and len(data)<=4_000_000:
                    with db() as c:c.execute('INSERT OR REPLACE INTO responses VALUES(?,?,?,?,?)',(key,r.headers.get('ETag'),r.headers.get('Last-Modified'),zlib.compress(data,3),time.time()))
                return r.status,r.geturl(),data
        except urllib.error.HTTPError as e:
            if e.code==304 and cached:
                STATS['revalidated']+=1;return 200,url,zlib.decompress(cached[2])
            if e.code in (403,429):
                try:delay=float(e.headers.get('Retry-After','900'))
                except ValueError:delay=900
                with _lock:_blocked[host]=time.time()+max(60,min(delay,86400))
            if e.code not in (500,502,503,504) or attempt>=retries:
                STATS['failures']+=1;return e.code,url,b''
        except (OSError,ValueError):
            if attempt>=retries:STATS['failures']+=1;return 0,url,b''
        time.sleep(min(1, max(0,DEADLINE-time.monotonic())))
    return 0,url,b''

def get_bytes(url,timeout=12,retries=1):return request(url,timeout=timeout,retries=retries)[2]
def get_json(url,timeout=12,retries=1):
    try:return json.loads(get_bytes(url,timeout,retries))
    except (ValueError,UnicodeError):return None
def post_json(url,body,timeout=12,retries=1):
    try:return json.loads(request(url,body,timeout,retries)[2])
    except (ValueError,UnicodeError):return None

def compact():
    with db() as c:
        c.execute('DELETE FROM responses WHERE at<?',(time.time()-7*86400,))
        total=c.execute('SELECT coalesce(sum(length(body)),0) FROM responses').fetchone()[0]
        for key,size in c.execute('SELECT k,length(body) FROM responses ORDER BY at').fetchall():
            if total<=64*1024*1024:break
            c.execute('DELETE FROM responses WHERE k=?',(key,));total-=size
    with db() as c:c.execute('PRAGMA wal_checkpoint(TRUNCATE)');c.execute('VACUUM')
