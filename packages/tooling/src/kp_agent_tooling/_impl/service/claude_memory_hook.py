"""Local Claude lifecycle adapter: capture/enqueue only, never model inference."""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.service.episodic_capture_policy import evaluate_capture_trigger


class HookCaptureIncomplete(RuntimeError):
    pass


class HookTelemetry:
    def __init__(self,path):
        self.path=leaf.as_path(path)

    def initialize(self):
        leaf.touch_new_private(self.path)
        with closing(leaf.sqlite_connect(self.path, mode='rwc', resolve=True)) as db, db:
            db.execute('CREATE TABLE receipts (id INTEGER PRIMARY KEY,binding TEXT NOT NULL,session TEXT NOT NULL,payload TEXT NOT NULL)')

    def _db(self):
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError('hook telemetry unavailable; initialize explicitly')
        return leaf.sqlite_connect(self.path, mode='rw', resolve=True)

    def record(self,store,session,payload):
        binding=store._binding(session)
        raw=leaf.canonical_json(payload, ascii=True, allow_nan=True)
        if len(raw.encode())>16384:
            raise ValueError('hook receipt exceeds bound')
        with closing(self._db()) as db, db:
            return db.execute('INSERT INTO receipts(binding,session,payload) VALUES (?,?,?)',
                              (binding,session,raw)).lastrowid

    def recent(self,store,session,limit=20):
        if type(limit) is not int or not 1<=limit<=100:
            raise ValueError('bounded receipt limit required')
        binding=store._binding(session)
        with closing(self._db()) as db:
            rows=db.execute('SELECT id,payload FROM receipts WHERE binding=? ORDER BY id DESC LIMIT ?',
                            (binding,limit)).fetchall()
        return [{'receipt_id':row[0],**json.loads(row[1])} for row in rows]


def handle_hook(payload, *, session, transcript_path, store, queue, capture, telemetry,
                source_batch_bytes=32768, max_pages=4, hook_transcript_path=None):
    """Trusted config fixes session/path; payload cannot select another source."""
    store._binding(session)
    native = capture.identity(session, store=store, transcript_path=transcript_path)
    expected_hook_path = hook_transcript_path or transcript_path
    if (not isinstance(payload,dict) or payload.get('session_id')!=native
            or payload.get('hook_event_name') not in {'Stop','PreCompact','SessionEnd'}
            or not isinstance(payload.get('transcript_path'),str)
            or Path(payload['transcript_path']).resolve()!=Path(expected_hook_path).resolve()):
        raise ValueError('hook session/path/event differs from operator binding')
    if type(max_pages) is not int or not 1<=max_pages<=16:
        raise ValueError('bounded capture page count required')
    event=payload['hook_event_name']
    lifecycle={'PreCompact':'pre_compact','SessionEnd':'session_end'}.get(event)
    started=time.monotonic()
    report={'schema_version':'ops.claude-capture-hook.v1','event':event,
            'observed_at':datetime.now(timezone.utc).isoformat(),
            'model_identity':'unavailable','occupancy_source':'unavailable',
            'model_requests':0,'batches':[]}
    try:
        before=capture.status(session,store=store,transcript_path=transcript_path)
        decision=evaluate_capture_trigger(session_id=session,model_id='unknown',
            context_epoch='unknown',pending_source_bytes=before['pending_source_bytes'],
            event=lifecycle,source_batch_bytes=source_batch_bytes,usage=None)
        report['decision']=decision
        replay=before['has_pending_batch']
        after=before
        if decision['trigger'] or replay or (event=='Stop' and capture.native_session_id is not None):
            for _ in range(max_pages):
                result=capture.capture(session,store=store,queue=queue,transcript_path=transcript_path,
                                       reason=decision['reason'] or 'batch')
                # No transcript text or native summary is copied to hook telemetry.
                report['batches'].append({key:result[key] for key in
                    ('state','episode_id','job_id','event_count','offset','has_more',
                     'incomplete_tail','verified_bytes','remaining_bytes')
                    if key in result})
                report['batches'][-1]['omission_count']=len(result.get('omissions',[]))
                after=capture.status(session,store=store,transcript_path=transcript_path)
                if not after['pending_source_bytes'] and not after['has_pending_batch']:
                    break
                if result['state']=='idle':
                    break
        report['pending_source_bytes']=after['pending_source_bytes']
        report['queue']=queue.metrics(session)
        report['status']='captured' if report['batches'] else 'not_due'
        if lifecycle and (after['pending_source_bytes'] or after['has_pending_batch']):
            report['status']='capture_incomplete'
            raise HookCaptureIncomplete('lifecycle flush incomplete; source retained; retry capture before compaction')
    except Exception as error:
        report.setdefault('status','failed')
        report['error_category']=type(error).__name__
        raise
    finally:
        report['elapsed_ms']=int((time.monotonic()-started)*1000)
        telemetry.record(store,session,report)
    return report
