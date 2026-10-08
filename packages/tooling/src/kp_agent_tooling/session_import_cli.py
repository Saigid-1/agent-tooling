#!/usr/bin/env python3
"""Explicit, resumable native Codex session import for a configured desk host."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.service.desk_memory_runtime import components
from kp_agent_tooling._impl.service.session_import_job import ImportJobError, SessionImportJobs
from kp_agent_tooling._impl.service.desk_binding import DeskLaunchUnavailable
from kp_agent_tooling._impl.service.episodic_memory import EpisodeUnavailable, EpisodeConflict


_INPUT_LIMIT = 262_144
_REF_SCHEMA = 'ops.session-import.job-ref.v1'


def _input(value):
    if value == '-':
        raw = sys.stdin.buffer.read(_INPUT_LIMIT + 1)
    else:
        path = Path(value)
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise ImportJobError('invalid_input', 'Select an absolute regular JSON request file.')
        if path.stat().st_size > _INPUT_LIMIT:
            raise ImportJobError('invalid_input', 'JSON request exceeds its bounded size.')
        raw = path.read_bytes()
    if len(raw) > _INPUT_LIMIT:
        raise ImportJobError('invalid_input', 'JSON request exceeds its bounded size.')
    try:
        result = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        raise ImportJobError('invalid_input', 'Request must be valid JSON.') from None
    if not isinstance(result, dict):
        raise ImportJobError('invalid_input', 'Request must be a JSON object.')
    return result


def _ref(request):
    if set(request) != {'schema_version', 'job_id'} or request['schema_version'] != _REF_SCHEMA:
        raise ImportJobError('invalid_request', 'Exact job reference is required.')
    job_id = request['job_id']
    if not isinstance(job_id, str) or len(job_id) > 256 or not job_id:
        raise ImportJobError('invalid_request', 'Exact job reference is required.')
    return job_id


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('action', choices=('desks', 'preview', 'apply', 'status', 'continue',
                                           'follow', 'stop', 'resume', 'list-following',
                                           'assert-owner'))
    parser.add_argument('--input', default='-')
    args = parser.parse_args(argv)
    stage = 'configuration'
    try:
        config, registry, ledger, store = components(args.config)
        stage = 'admission'
        admission = ledger.resolve(config['provider_session_id'], registry)
        jobs = SessionImportJobs(store, admission.tenant_id)
        stage = 'request'
        request = {} if args.action in ('desks', 'list-following') else _input(args.input)
        if args.action == 'desks':
            result = jobs.desks()
        elif args.action == 'list-following':
            result = jobs.list_following()
        elif args.action == 'preview':
            result = jobs.preview(request)
        elif args.action == 'apply':
            if set(request) != {'schema_version', 'plan_token', 'consent'} or request['schema_version'] != 'ops.session-import.apply.v1':
                raise ImportJobError('invalid_request', 'Exact reviewed plan and consent are required.')
            result = jobs.apply(request['plan_token'], consent=request['consent'])
        elif args.action == 'assert-owner':
            expected = {'schema_version', 'job_id', 'selected_desk_id', 'asserted_by', 'recorded_at', 'evidence'}
            if set(request) != expected or request['schema_version'] != 'ops.session-import.owner-assertion.v1':
                raise ImportJobError('invalid_request', 'Exact owner assertion request is required.')
            result = jobs.assert_owner(**{key: request[key] for key in expected if key != 'schema_version'})
        else:
            identity = _ref(request)
            result = {'status': jobs.status, 'continue': jobs.advance,
                      'follow': jobs.follow, 'stop': jobs.stop,
                      'resume': jobs.resume}[args.action](identity)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0
    except ImportJobError as error:
        print(json.dumps({'schema_version': 'ops.session-import.result.v1',
                          'status': 'error', 'code': error.code, 'message': str(error)}))
        return 1
    except (DeskLaunchUnavailable, EpisodeUnavailable):
        print(json.dumps({'schema_version': 'ops.session-import.result.v1',
                          'status': 'error', 'code': 'admission_unavailable',
                          'message': 'Configured desk session is not admitted or available.'}))
        return 1
    except EpisodeConflict:
        print(json.dumps({'schema_version': 'ops.session-import.result.v1',
                          'status': 'error', 'code': 'sealed_source_conflict',
                          'message': 'Native row differs from previously sealed evidence; review source integrity.'}))
        return 1
    except leaf.StoreOutsideVolume as error:
        print(json.dumps({'schema_version': 'ops.session-import.result.v1',
                          'status': 'error', 'code': error.category, 'message': str(error)}))
        return 1
    except (OSError, ValueError, TypeError, sqlite3.Error):
        code = ('invalid_configuration' if stage == 'configuration' else
                'admission_unavailable' if stage == 'admission' else 'import_unavailable')
        print(json.dumps({'schema_version': 'ops.session-import.result.v1',
                          'status': 'error', 'code': code,
                          'message': ('Check private host configuration.' if code == 'invalid_configuration' else
                                      'Configured desk session is not admitted or available.' if code == 'admission_unavailable' else
                                      'Session import could not continue; inspect the job status and source.') }))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
