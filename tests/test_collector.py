import json,time,unittest
from unittest.mock import patch
from collector import run
from collector.relay import pack,unpack
from collector.sources import adapters,posting_dates,net,sites,indiafeeds

NOW=1793910000
BOARD={'ats':'greenhouse','token':'example','name':'Example','interval':300,'priority':True,'india_proven':True}
JOB={'url':'https://example.com/job/1','title':'Software Engineer','company':'Example','location':'Bengaluru, India','description':'Build APIs in Python, TypeScript, SQL and React. 3 years of software development experience.'}

class CollectorTests(unittest.TestCase):
 def test_inventory_is_complete_and_public(self):
  boards=json.loads((run.ROOT/'sources.json').read_text())
  self.assertGreaterEqual(len(boards),14530);self.assertEqual(len({run.board_key(b) for b in boards}),len(boards))
  self.assertEqual(sum(b['priority'] for b in boards),40)
  self.assertTrue(all(set(b)=={'ats','token','name','interval','priority','india_proven'} for b in boards))
  self.assertTrue(all(b['ats'] in adapters.ADAPTERS or b['ats']=='site' for b in boards))
 def test_schedule_picks_due_priority_before_wide_sources(self):
  wide={**BOARD,'token':'wide','priority':False,'india_proven':False}
  state={'boards':{},'seen':{}}
  self.assertEqual(run.choose([wide,BOARD],state,NOW,1),[BOARD])
  state['boards'][run.board_key(BOARD)]=[NOW+10,0]
  self.assertEqual(run.choose([wide,BOARD],state,NOW,1),[wide])
 def test_public_url_rejects_credentials_and_private_addresses(self):
  for value in ['file:///etc/passwd','http://127.0.0.1/','http://localhost/','http://169.254.169.254/','https://user:password@example.com/','http://[broken']:
   self.assertFalse(net.public_url(value))
 def test_unknown_date_never_becomes_discovery_time(self):
  j=run.normalize(JOB,BOARD,{},NOW)
  self.assertEqual(j['posted_at'],'');self.assertEqual(j['posted_field'],'')
  self.assertIsNone(posting_dates.publication(j));self.assertTrue(j['added_at'])
 def test_record_creation_date_is_not_publication(self):
  j=run.normalize({**JOB,'posted_at':'2026-11-05T12:00:00Z','posted_field':'lever.createdAt'},BOARD,{},NOW)
  self.assertEqual(j['posted_at'],'');self.assertIsNone(posting_dates.publication(j))
 def test_original_and_republish_evidence_stay_distinct(self):
  for field,kind in [('greenhouse.first_published','published'),('ashby.publishedAt','last_published')]:
   j=run.normalize({**JOB,'posted_at':'2026-11-05T12:00:00Z','posted_field':field},BOARD,{},NOW)
   p=posting_dates.publication(j);self.assertEqual(p['kind'],kind)
 def test_unknown_timezone_does_not_gain_hour_precision(self):
  j=run.normalize({**JOB,'posted_at':'2026-11-05','posted_field':'greenhouse.first_published'},BOARD,{},NOW)
  self.assertEqual(posting_dates.publication(j)['precision'],'day')
 def test_india_filter_does_not_assume_all_remote_jobs_eligible(self):
  for loc in ['Indiana, US','Remote - US only','London','Remote']:
   self.assertIsNone(run.normalize({**JOB,'location':loc},BOARD,{},NOW))
  self.assertIsNotNone(run.normalize({**JOB,'location':'Remote, Worldwide'},BOARD,{},NOW))
 def test_nonsoftware_and_management_are_excluded(self):
  for title in ['Mechanical Engineer','Engineering Manager','Recruiter']:
   self.assertIsNone(run.normalize({**JOB,'title':title},BOARD,{},NOW))
 def test_unchanged_content_does_not_republish_every_check(self):
  a=run.normalize(JOB,BOARD,{},NOW);b=run.normalize(JOB,BOARD,{},NOW+300)
  self.assertEqual(run.fingerprint(a),run.fingerprint(b))
  b['title']='Senior Software Engineer';self.assertNotEqual(run.fingerprint(a),run.fingerprint(b))
 def test_existing_conflict_is_not_erased_by_unverified_api(self):
  j=run.normalize(JOB,BOARD,{},NOW);self.assertNotIn('posted_conflict',j)
  j=run.normalize({**JOB,'posted_comparison':json.dumps({'status':'unavailable'})},BOARD,{},NOW)
  self.assertNotIn('posted_conflict',j)
 def test_large_multibyte_description_stays_in_relay_limit(self):
  j=run.normalize({**JOB,'description':JOB['description']+'漢'*50000},BOARD,{},NOW)
  self.assertLess(run.event_size(j),60000);self.assertLessEqual(len(j['description'].encode()),32000)
 def test_no_application_or_identity_fields_leave_normalization(self):
  j=run.normalize({**JOB,'app_status':'applied','password':'secret','identity':{'name':'secret'}},BOARD,{},NOW)
  self.assertFalse({'app_status','password','identity'}&j.keys())
 def test_checkpoint_round_trip(self):
  s={'v':1,'boards':{'x':[NOW,0]},'seen':{'hash':['digest','2026-11-05 12:00',NOW]}}
  self.assertEqual(unpack(pack(s)),s)
  with self.assertRaises(ValueError):unpack(pack({'v':2}))
 def test_malformed_source_values_do_not_poison_a_batch(self):
  self.assertIsNone(run.normalize([],BOARD,{},NOW))
  self.assertIsNone(run.normalize({**JOB,'location':{}},BOARD,{},NOW))
  j=run.normalize({**JOB,'description':'','salary_min':float('nan'),'salary_max':float('inf')},BOARD,{},NOW)
  json.dumps(j,allow_nan=False)
 def test_rendered_sources_remain_explicitly_unavailable(self):
  b={**BOARD,'ats':'site'}
  result=run.fetch(b,{'example':{'method':'render'}})
  self.assertFalse(result[2]);self.assertEqual(result[3],'browser_required')

if __name__=='__main__':unittest.main()
