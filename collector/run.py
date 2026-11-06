"""Bounded software-job collection for applicants based in India."""
import argparse, concurrent.futures, copy, hashlib, json, math, os, time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from collector.relay import Relay, pack, unpack
from collector.sources import adapters, date_verification, enrich, indiafeeds, net, posting_dates, regions, scorer, sites, webscout, eligibility

ROOT=Path(__file__).resolve().parents[1]
FIELDS='title source company location score posted_at posted_field posted_checked_at posted_conflict posted_comparison added_at description salary_min salary_max salary_cur salary_period yoe sponsor edu tech emp_type department last_seen'.split()
NUMBERS={'score','salary_min','salary_max','yoe'}
VOLATILE={'added_at','last_seen','posted_checked_at','posted_comparison'}

def identifier(text):return hashlib.sha256(text.encode()).hexdigest()[:32]
def fingerprint(job):
    stable={k:job.get(k,0 if k in NUMBERS else '') for k in FIELDS if k not in VOLATILE}
    for k in NUMBERS:
        if isinstance(stable[k],float) and stable[k].is_integer():stable[k]=int(stable[k])
    return identifier(json.dumps(stable,ensure_ascii=False,sort_keys=True,separators=(',',':')))
def board_key(board):return identifier(board['ats']+':'+board['token'])

def choose(boards,state,now,limit):
    def due_at(board):
        checkpoint=state['boards'].get(board_key(board),[0,0])
        # A newly shortened interval must not inherit a healthy board's old daily
        # deadline. Retain failure backoff; do not hammer unavailable publishers.
        return 0 if not checkpoint[1] and checkpoint[0]>now+board['interval'] else checkpoint[0]
    due=[b for b in boards if due_at(b)<=now]
    due.sort(key=lambda b:(not b['priority'],not b['india_proven'],due_at(b),board_key(b)))
    return due[:limit]

def normalize(job,board,profile,now):
    if not isinstance(job,dict) or not isinstance(job.get('location',''),str):return None
    job=copy.deepcopy(job)
    for k,v in job.items():
        if isinstance(v,str):job[k]=v.replace('\x00','\ufffd').encode('utf-8','replace').decode()
    if not net.public_url(job.get('url','')) or not eligibility.evidence(job):return None
    if job.get('description'):enrich.enrich(job)
    score,_=scorer.score(job,profile)
    if score<=0:return None
    out={k:job.get(k,0 if k in NUMBERS else '') for k in FIELDS}
    out.update(url=job['url'],source=board['ats'],score=score)
    out['company']=job.get('company') or board['name'];out['title']=str(out['title'])[:500]
    out['description']=str(out['description'])[:20000].encode()[:32000].decode('utf-8','ignore')
    out['posted_checked_at']=datetime.fromtimestamp(now,timezone.utc).isoformat()
    # A missing date/provenance remains unknown. Never substitute ingestion or record creation.
    if out['posted_field'] not in posting_dates.FIELDS:out['posted_at']='';out['posted_field']=''
    local=datetime.fromtimestamp(now,ZoneInfo(os.environ.get('JOB_TIMEZONE','Asia/Kolkata'))).strftime('%Y-%m-%d %H:%M')
    out['added_at']=local;out['last_seen']=local
    # An unchecked source response cannot erase a conflict established by a canonical page.
    comparison=job.get('posted_comparison')
    if not comparison:
        out.pop('posted_conflict',None);out.pop('posted_comparison',None)
    else:
        try:verified=json.loads(comparison).get('status') in ('agrees','conflict')
        except (ValueError,AttributeError):verified=False
        if not verified:out.pop('posted_conflict',None)
    for k in NUMBERS:
        if type(out[k]) not in (int,float) or not math.isfinite(out[k]) or abs(out[k])>=1e12:out[k]=0
    for k in (set(FIELDS)-NUMBERS)&out.keys():
        if out[k] is None:out[k]=''
        if not isinstance(out[k],str):out[k]=str(out[k])
    return out

def event_size(job):
    event={'v':1,'entity':'job','key':job['url'],'fields':{k:{'value':v,'clock':4503599627370495,'device':'0'*36} for k,v in job.items() if k!='url'}}
    return len(json.dumps(event,ensure_ascii=False).encode())+200

def fetch(board,recipes):
    if board['ats']=='site':
        recipe=recipes.get(board['token'])
        if not recipe:return board,[],False,'missing_recipe'
        if recipe.get('method')=='render' or recipe.get('job_pages')=='render':return board,[],False,'browser_required'
        rows=sites.run(recipe,board['name'],board['token'])
    else:rows=adapters.fetch(board['ats'],board['token'],board['name'])
    date_verification.check(rows,limit=1)
    return board,rows,adapters.read_ok(rows),'ok' if adapters.read_ok(rows) else 'unavailable'

def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('--dry-run',action='store_true');parser.add_argument('--limit',type=int,default=180);parser.add_argument('--seconds',type=int,default=190);parser.add_argument('--max-jobs',type=int,default=600)
    args=parser.parse_args(argv);started=time.monotonic();now=int(time.time())
    profile=json.loads(os.environ.get('MATCHING_RULES','{}'))
    if not args.dry_run and not profile.get('finder'):raise ValueError('Matching rules secret is missing')
    relay=None if args.dry_run else Relay();remote=relay.state() if relay else {'counter':0,'checkpoint':''}
    state=unpack(remote['checkpoint']);counter=remote['counter']
    boards=json.loads((ROOT/'sources.json').read_text());recipes=json.loads((ROOT/'recipes.json').read_text())
    selected=choose(boards,state,now,max(1,min(args.limit,400)))
    net.DEADLINE=started+max(10,min(args.seconds,240))
    stats={'selected':len(selected),'checked':0,'successful':0,'unavailable':0,'browser_required':0,'matched':0,'published':0,'deferred':0}
    batch=[];size=0
    def flush():
        nonlocal counter,batch,size
        if relay:
            counter+=1;relay.publish(counter,batch,state)
        stats['published']+=len(batch);batch=[];size=0
    def process(board,rows,ok,reason):
        nonlocal batch,size
        stats['checked']+=1;stats['successful' if ok else 'unavailable']+=1
        if reason=='browser_required':stats['browser_required']+=1
        key=board_key(board);previous=state['boards'].get(key,[0,0]);failures=0 if ok else min(8,previous[1]+1)
        interval=board['interval'] if ok else min(86400,max(300,board['interval']//4)*2**failures)
        if reason=='browser_required':interval=86400
        complete=True
        # Keep the four-detail budget, prioritizing India while also checking overseas
        # software postings whose visa/relocation terms are only on the detail page.
        needs=[raw for raw in rows if not raw.get('description') and raw.get('detail') and scorer.is_software_title(raw.get('title',''))]
        needs.sort(key=lambda raw:not indiafeeds.india_eligible(raw.get('location','')))
        for raw in needs[:4]:
            if time.monotonic()>net.DEADLINE:break
            raw['description']=adapters.fetch_detail(raw)
        for raw in rows:
            if time.monotonic()>net.DEADLINE+60:complete=False;break
            job=normalize(raw,board,profile,now)
            if not job:continue
            stats['matched']+=1;identity=identifier(job['url']);old=state['seen'].get(identity)
            if old:job['added_at']=old[1]
            digest=fingerprint(job);publication=posting_dates.publication(job)
            recent=publication and publication['precision']=='instant' and now-publication['ts']<=3600
            if old and old[0]==digest and (not recent or now-old[2]<600):continue
            if stats['published']+len(batch)>=args.max_jobs:
                complete=False;stats['deferred']+=1;continue
            n=event_size(job)
            if n>60000:stats['deferred']+=1;complete=False;continue
            if batch and (size+n>58000 or len(batch)>=12):flush()
            state['seen'][identity]=[digest,job['added_at'],now]
            batch.append(job);size+=n
        state['boards'][key]=[now+interval if complete else now+300,failures]
    # Process results as they arrive. Never wait for one failed source before using others.
    pool=concurrent.futures.ThreadPoolExecutor(max_workers=12)
    futures={pool.submit(fetch,b,recipes):b for b in selected}
    try:
        for future in concurrent.futures.as_completed(futures,timeout=max(1,net.DEADLINE-time.monotonic()+15)):
            try:result=future.result()
            except Exception:result=(futures[future],[],False,'unavailable')
            process(*result)
    except concurrent.futures.TimeoutError:pass
    finally:
        net.DEADLINE=time.monotonic()
        for future in futures:future.cancel()
        pool.shutdown(wait=True,cancel_futures=True)
    # Bound dedupe metadata; this is not a second job database.
    if len(state['seen'])>50000:
        state['seen']=dict(sorted(state['seen'].items(),key=lambda pair:pair[1][2],reverse=True)[:50000])
    flush();net.compact()
    stats.update(seconds=round(time.monotonic()-started,2),http=net.STATS,mode='dry-run' if args.dry_run else 'published',inventory=len(boards))
    if args.dry_run:stats['would_publish']=stats.pop('published')
    print(json.dumps(stats,sort_keys=True))
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as f:
            f.write('### Run summary\n\n'+ '\n'.join(f'- {k}: {v}' for k,v in stats.items() if k!='http')+'\n')
    if selected and not stats['successful']:raise RuntimeError('No source completed successfully; check public source availability.')
    return stats

if __name__=='__main__':main()
