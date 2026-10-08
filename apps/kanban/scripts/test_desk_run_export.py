import importlib.util
import json
import copy
import unittest
from pathlib import Path

spec=importlib.util.spec_from_file_location('projection',Path(__file__).with_name('project-desk-run-export.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

def fixture():
    a=dict(host_session_id='host',tenant_id='tenant',role='Coordinator',repo_key='repo',status='active',begin_at='2026-09-24T01:00:00Z',begin_summary='Session begin')
    a.update(run_key='deskrun:'+m.digest(b'host'),begin_content_digest=m.digest(a['begin_summary'].encode()))
    r=dict(id=a['run_key'],entity_type='DeskRun',attributes=a)
    n=dict(id='note',entity_type='DeskNote',attributes=dict(tenant_id='tenant',role='Coordinator',repo_key='repo',note_key='note',text='host claim',content_digest=m.digest(b'host claim'),authored_at='2026-09-24T01:01:00Z',trust_class='desk_authored'))
    return dict(records=dict(desk_runs=[r],claim_note=n),selection=dict(subject_host_session_id='host'),exported_at_utc='2026-09-24T02:00:00Z',db_clock_at_read='2026-09-24T02:00:00Z')

def encode(d):
    d['per_record_export_digest_sha256']={r['id']:m.digest(json.dumps(r,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()) for r in [*d['records']['desk_runs'],d['records']['claim_note']]}
    return json.dumps(d).encode()

def run(d):
    b=encode(d);return m.project(b,m.digest(b),'tenant','Coordinator','repo','host')

class Tests(unittest.TestCase):
    def test_absent_provenance_and_open_end(self):
        p=run(fixture());self.assertFalse(p['rows'][0]['provenance_present']);self.assertIsNone(p['rows'][0]['end']);self.assertEqual(p['authorization'],'not_assessed')
    def test_concurrent_unretired_sessions_remain_discoverable(self):
        d=fixture();second=copy.deepcopy(d['records']['desk_runs'][0])
        second['attributes']['host_session_id']='host-two'
        second['id']='deskrun:'+m.digest(b'host-two')
        second['attributes']['run_key']=second['id']
        d['records']['desk_runs'].append(second)
        p=run(d)
        self.assertEqual(len(p['directory']['sessions']),2)
        self.assertEqual(p['directory']['concurrency'],'allowed')
        self.assertFalse(p['directory']['unretired_predecessor_blocks_discovery'])
        self.assertEqual(len(p['concurrency_observations']),1)
        self.assertNotIn('possible_overlaps',p)
        for session in p['directory']['sessions']:
            self.assertEqual(session['reachability'],'not_checked')
            self.assertIsNone(session['contact_route'])

    def test_wrong_export_hash(self):
        with self.assertRaises(ValueError):m.project(encode(fixture()),'0'*64,'tenant','Coordinator','repo','host')
    def test_wrong_scope(self):
        d=fixture();d['records']['claim_note']['attributes']['tenant_id']='other'
        with self.assertRaises(ValueError):run(d)
    def test_bad_original_digest(self):
        d=fixture();d['records']['desk_runs'][0]['attributes']['begin_summary']='changed'
        with self.assertRaises(ValueError):run(d)
    def test_mechanical_marker_is_not_explicit_retirement(self):
        d=fixture();a=d['records']['desk_runs'][0]['attributes'];a.update(status='retired',end_at='2026-09-24T02:00:00Z',end_summary='mechanical SessionEnd hook; no /retire ran, or it ran later');a['end_content_digest']=m.digest(a['end_summary'].encode());self.assertEqual(run(d)['rows'][0]['end_qualification'],'mechanical_marker_in_summary')
    def test_reconstructed_begin_is_unknown(self):
        d=fixture();a=d['records']['desk_runs'][0]['attributes'];a['begin_summary']='Session begin — reconstructed at retirement';a['begin_content_digest']=m.digest(a['begin_summary'].encode());self.assertIsNone(run(d)['rows'][0]['supported_begin'])
    def test_active_with_end_refused(self):
        d=fixture();d['records']['desk_runs'][0]['attributes']['end_at']='2026-09-24T02:00:00Z'
        with self.assertRaises(ValueError):run(d)

if __name__=='__main__':unittest.main()
