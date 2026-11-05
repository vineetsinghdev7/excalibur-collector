"""Scoped job-only RPCs. Errors never print a provider body, key, or matching preferences."""
import base64,json,os,time,urllib.request,urllib.error,zlib

def pack(state):return base64.b64encode(zlib.compress(json.dumps(state,separators=(',',':'),sort_keys=True).encode(),6)).decode()
def unpack(value):
    if not value:return {'v':1,'boards':{},'seen':{}}
    data=zlib.decompress(base64.b64decode(value,validate=True))
    if len(data)>20_000_000:raise ValueError('Checkpoint too large')
    result=json.loads(data)
    if result.get('v')!=1 or not isinstance(result.get('boards'),dict) or not isinstance(result.get('seen'),dict):raise ValueError('Invalid collector checkpoint')
    return result

class Relay:
    def __init__(self):
        self.url=os.environ['SUPABASE_URL'].rstrip('/');self.publishable=os.environ['SUPABASE_PUBLISHABLE_KEY'];self.key=os.environ['COLLECTOR_KEY']
        if not self.url.startswith('https://') or not self.url.endswith('.supabase.co'):raise ValueError('Invalid relay project URL')
    def call(self,name,body):
        data=json.dumps({'p_key':self.key,**body},separators=(',',':')).encode()
        for attempt in range(3):
            try:
                req=urllib.request.Request(self.url+'/rest/v1/rpc/exc_collect_'+name,data=data,
                    headers={'apikey':self.publishable,'Content-Type':'application/json'})
                with urllib.request.urlopen(req,timeout=30) as r:return json.loads(r.read(4_000_000))
            except urllib.error.HTTPError as e:
                if e.code not in (429,500,502,503,504) or attempt==2:
                    raise RuntimeError(f'Relay {name} refused the request (HTTP {e.code}); checkpoint was not advanced locally.') from None
            except OSError:
                if attempt==2:raise RuntimeError('Relay unavailable; retry from the last committed checkpoint.') from None
            time.sleep(2**attempt)
    def state(self):return self.call('state',{})
    def publish(self,counter,jobs,state):
        reply=self.call('publish',{'p_counter':counter,'p_jobs':jobs,'p_checkpoint':pack(state)})
        if reply.get('accepted_through')!=counter:raise RuntimeError('Relay did not confirm this checkpoint')
        return reply
