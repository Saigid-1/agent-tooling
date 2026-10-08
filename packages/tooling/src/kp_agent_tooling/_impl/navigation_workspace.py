"""Current source profiles and bounded, live workspace observations.

The profile is operator-published. Observations never merge work, promote evidence,
or infer semantic conflicts from overlapping file paths.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import math
from datetime import datetime, timezone

SHA = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
DEFAULT_REFRESH_MAX_AGE_SECONDS = 300


def _source_identity(repos):
    """Return the identity fields emitted by the offline refresh check writer."""
    return {
        key: {field: row.get(field) for field in ('revision', 'default_ref')}
        for key, row in repos.items()
    }


def _response_freshness(status, *, now, max_age_seconds, expected_source_revisions=None,
                        expected_profile_sha256=None):
    """Qualify a persisted refresh observation without performing a refresh.

    ``max_age_seconds`` defaults to five minutes.  The threshold is inclusive:
    an observation is current through exactly the configured age.
    """
    if (isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, (int, float))
            or not math.isfinite(max_age_seconds) or max_age_seconds <= 0):
        raise ValueError('refresh age threshold must be a finite positive number')
    result = dict(status) if isinstance(status, dict) else {'status': 'unavailable'}
    result['age_seconds'] = None
    checked_at = result.get('checked_at')
    checked = None
    if isinstance(checked_at, str):
        try:
            checked = datetime.fromisoformat(checked_at.replace('Z', '+00:00'))
        except ValueError:
            pass
    if checked is not None and checked.tzinfo is not None:
        checked = checked.astimezone(timezone.utc)
        age = (now.astimezone(timezone.utc) - checked).total_seconds()
        if age >= 0:
            result['age_seconds'] = age
            if result.get('status') in {'current', 'published'}:
                observed = result.get('source_revisions')
                expected = expected_source_revisions
                matches = (
                    isinstance(observed, dict)
                    and isinstance(expected, dict)
                    and set(observed) == set(expected)
                    and all(
                        isinstance(observed[key], dict)
                        and all(observed[key].get(field) == value
                                for field, value in expected[key].items())
                        for key in expected
                    )
                )
                if result.get('profile_sha256') != expected_profile_sha256:
                    result['status'] = 'unverified_profile_identity'
                    result['expected_profile_sha256'] = expected_profile_sha256
                elif not matches:
                    result['status'] = 'unverified_source_identity'
                    result['expected_source_revisions'] = expected
                else:
                    if result.get('status') == 'current':
                        result['status'] = ('current_as_of_check' if age <= max_age_seconds
                                            else 'unverified_stale')
            return result
    if result.get('status') in {'current', 'published'}:
        if checked_at is None:
            result['status'] = 'unverified_missing_checked_at'
        elif checked is not None and checked.tzinfo is not None:
            result['status'] = 'unverified_future_checked_at'
        else:
            result['status'] = 'unverified_invalid_checked_at'
    return result


def active(config, *, now=None):
    now = datetime.now(timezone.utc) if now is None else now
    if now.tzinfo is None:
        raise ValueError('response clock must be timezone-aware')
    threshold = config.get('navigation_refresh_max_age_seconds',
                           DEFAULT_REFRESH_MAX_AGE_SECONDS)
    # Validate even when no status file is configured so bad operator policy
    # cannot silently become active on a later publication.
    _response_freshness({'status': 'not-configured'}, now=now,
                        max_age_seconds=threshold)
    path = config.get('navigation_profile')
    if not path:
        return {'profile': 'configured-pins', 'repos': config['repos'],
                'knowledge_config': config.get('local_knowledge_config'),
                'workspace_repositories': {}, 'published_at': None}
    raw = Path(path).read_bytes()
    if len(raw) > 131072:
        raise ValueError('navigation profile exceeds budget')
    value = json.loads(raw)
    if value.get('schema_version') != 'ops.navigation-profile.v1':
        raise ValueError('unsupported navigation profile')
    if set(value['repos']) != set(config['repos']):
        raise ValueError('profile repository membership changed; host refresh required')
    for row in value['repos'].values():
        if not SHA.fullmatch(row['revision']) or not Path(row['path']).is_absolute():
            raise ValueError('profile requires absolute snapshots and exact revisions')
    if value.get('knowledge_config'):
        catalog_raw=Path(value['knowledge_config']).read_bytes()
        if len(catalog_raw)>131072 or hashlib.sha256(catalog_raw).hexdigest()!=value.get('knowledge_config_sha256'):
            raise ValueError('navigation catalog identity mismatch')
        catalog=json.loads(catalog_raw)
        if set(catalog['repositories'])!=set(value['repos']):raise ValueError('catalog repository membership mismatch')
        for key,row in value['repos'].items():
            if catalog['repositories'][key]['ref']!=row['revision'] or catalog['repositories'][key]['path']!=row['path']:
                raise ValueError('catalog source differs from active profile')
        for platform in catalog['platforms'].values():
            for key,row in platform['sources'].items():
                if key not in value['repos'] or row['revision']!=value['repos'][key]['revision']:
                    raise ValueError('platform source differs from active profile')
    status={'status':'not-configured'}
    if value.get('check_status'):
        try:
            status_raw=Path(value['check_status']).read_bytes()
            if len(status_raw)>131072:raise ValueError('refresh status exceeds budget')
            status=json.loads(status_raw)
        except (OSError,ValueError):status={'status':'unavailable'}
    profile_sha256 = hashlib.sha256(raw).hexdigest()
    status = _response_freshness(
        status, now=now, max_age_seconds=threshold,
        expected_source_revisions=_source_identity(value['repos']),
        expected_profile_sha256=profile_sha256)
    return dict(value, profile_sha256=profile_sha256,refresh_status=status)


def git(root, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    result = subprocess.run(['git', '--no-optional-locks', '-c', 'core.fsmonitor=false', *args],
                            cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            timeout=10, check=True)
    if len(result.stdout) > 2_000_000:
        raise ValueError('workspace observation exceeds budget')
    return result.stdout.decode('utf-8')


def _worktrees(root):
    rows=[]
    for block in git(root, 'worktree', 'list', '--porcelain', '-z').split('\0\0'):
        row={}
        for line in block.split('\0'):
            key, _, value = line.partition(' ')
            if key in ('worktree', 'HEAD', 'branch'): row[key]=value
        if row.get('worktree'): rows.append(row)
    return rows


def _dirty_paths(root):
    parts=iter(git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=normal').split('\0'))
    paths=[]
    for item in parts:
        if not item: continue
        paths.append(item[3:])
        if 'R' in item[:2] or 'C' in item[:2]: paths.append(next(parts, ''))
    return sorted(set(paths))


def workspace(config, repo_key, limit=64, path=None):
    if path is not None and (not path or path.startswith('/') or any(p in ('','.','..') for p in path.split('/'))):
        raise ValueError('canonical relative focus path required')
    focus=path
    profile=active(config); pinned=profile['repos'][repo_key]
    roots=profile.get('workspace_repositories',{}).get(repo_key, [])
    roots = roots if isinstance(roots,list) else [roots]
    rows=[]; gaps=[]; seen=set(); omitted=0;filtered=0;started=time.monotonic()
    for root in roots:
        try: candidates=_worktrees(root)
        except (OSError, ValueError, subprocess.SubprocessError):
            gaps.append('workspace_root_unavailable');continue
        for item in candidates:
            path=item['worktree']
            if path in seen: continue
            seen.add(path)
            if len(rows)>=limit or time.monotonic()-started>20: omitted+=1;continue
            row={'worktree':path,'branch':item.get('branch'),'head_revision':item.get('HEAD'),
                 'scope':'live-worktree-observation','conflict_verdict':'not-assessed'}
            dirty=None
            try:
                before=git(path,'rev-parse','HEAD').strip()
                dirty=_dirty_paths(path)
                # The published source repository may resolve both the refreshed
                # default tip and local branch tips through its object store.
                base=git(pinned['path'],'merge-base',pinned['revision'],before).strip()
                committed=git(pinned['path'],'diff','--name-only','-z',base,before,'--').split('\0')
                changed=sorted(set(dirty)|{x for x in committed if x})
                row.update(merge_base=base,dirty=bool(dirty),changed_paths=changed[:200],
                           omitted_paths=max(0,len(changed)-200),head_revision=before)
                after=git(path,'rev-parse','HEAD').strip()
                if before!=after:
                    row['observation_status']='changed_during_read';row['changed_paths']=[]
                else:row['observation_status']='observed'
            except (OSError, ValueError, subprocess.SubprocessError):
                row.update(observation_status='partial',reason='comparison_unavailable')
                if dirty is not None:row.update(dirty=bool(dirty),changed_paths=dirty[:200],omitted_paths=max(0,len(dirty)-200))
            if focus is not None and row.get('observation_status')=='observed':
                relevant=[p for p in changed if p==focus or p.startswith(focus+'/')]
                if not relevant:filtered+=1;continue
                row['changed_paths']=relevant[:200];row['omitted_paths']=max(0,len(relevant)-200)
            rows.append(row)
    # Untracked install/build output (node_modules, dist, __pycache__ ...) is real
    # dirt in a worktree but never a conflict between two of them. Classify it
    # with the manifest's own heuristic and keep it out of the overlap set.
    from kp_agent_tooling._impl.repository_manifest import _GENERATED_PARTS
    def _generated(path, worktree):
        parts=[p for p in path.lower().split('/') if p]
        if any(part in _GENERATED_PARTS for part in parts):
            return True
        if path.endswith('/'):
            # `git status` collapses an untracked directory to one entry; a directory
            # whose only contents are generated (service/ holding node_modules) is generated.
            try:
                children=[c for c in os.listdir(os.path.join(worktree, path)) if not c.startswith('.')]
            except OSError:
                return False
            return bool(children) and all(c.lower() in _GENERATED_PARTS for c in children)
        return False
    for row in rows:
        paths=row.get('changed_paths',[])
        generated=[p for p in paths if _generated(p, row['worktree'])]
        if generated:
            row['changed_paths']=[p for p in paths if not _generated(p, row['worktree'])]
            row['untracked_generated']=generated[:20]
            row['untracked_generated_count']=len(generated)
    overlaps=[]
    for i,left in enumerate(rows):
        for right in rows[i+1:]:
            common=sorted(set(left.get('changed_paths',[]))&set(right.get('changed_paths',[])))
            if common:
                overlaps.append({'left':left['worktree'],'right':right['worktree'],'paths':common[:30],
                                 'omitted_paths':max(0,len(common)-30),'verdict':'potential_file_overlap'})
    return {'schema_version':'ops.navigation-workspace.v1','profile':profile['profile'],
            'profile_sha256':profile.get('profile_sha256'),'published_at':profile.get('published_at'),
            'refresh_status':profile.get('refresh_status'),
            'repo_key':repo_key,'default_ref':pinned.get('default_ref'),'default_revision':pinned['revision'],
            'observed_at':datetime.now(timezone.utc).isoformat(),'worktrees':rows,'omitted_worktrees':omitted,
            'focus_path':focus,'filtered_worktrees':filtered,
            'overlaps':overlaps[:100],'omitted_overlaps':max(0,len(overlaps)-100),'gaps':gaps,
            'limitations':['Default revision is the last published remote snapshot, not a live remote query.',
                          'Worktree status is read on every call; the cross-worktree read is not atomic.',
                          'File overlap is not proof of a merge conflict, dependency incompatibility or active session ownership.',
                          'Uncommitted content is not promoted to source or execution evidence.']}


def observe_active_revision(config, repo_key):
    """Observe the moving published profile without treating configured pins as live."""
    observed_at=datetime.now(timezone.utc).isoformat()
    if not config.get('navigation_profile'):
        return {'status':'not_observed','reason':'navigation_profile_not_configured',
                'observed_at':observed_at,'scope':'live-navigation-profile-observation'}
    try:
        profile=active(config)
        row=profile['repos'][repo_key]
    except (OSError,ValueError,KeyError,json.JSONDecodeError) as error:
        return {'status':'unavailable','reason':'active_navigation_profile_unavailable',
                'detail':str(error)[:200],'observed_at':observed_at,
                'scope':'live-navigation-profile-observation'}
    return {'status':'observed','revision':row['revision'],'observed_at':observed_at,
            'profile':profile['profile'],'profile_sha256':profile.get('profile_sha256'),
            'published_at':profile.get('published_at'),'scope':'live-navigation-profile-observation'}


def read_source(config, repo_key, target_revision, path, start_line=1, line_count=80,
                selected_snapshot_id=None, active_navigation_revision=None):
    from kp_agent_tooling._impl.source_citations import source
    profile=active(config)
    if not SHA.fullmatch(target_revision):raise ValueError('explicit full commit required')
    profile_revision=profile['repos'][repo_key]['revision']
    selected_revision=profile_revision if selected_snapshot_id else target_revision
    selected_scope='frozen-navigation-snapshot' if selected_snapshot_id else 'explicit-requested-revision'
    selection={'snapshot_id':selected_snapshot_id,'revision':selected_revision,
               'profile':profile['profile'],'profile_sha256':profile.get('profile_sha256'),
               'published_at':profile.get('published_at'),'scope':selected_scope}
    live=active_navigation_revision or observe_active_revision(config,repo_key)
    metadata={'repo_key':repo_key,'profile':profile['profile'],
              'selected_snapshot_revision':selected_revision,
              'requested_revision':target_revision,
              'selected_snapshot':selection,
              'profile_revision_at_read':profile_revision,
              'profile_revision_role':'lookup context only; not the requested source',
              'active_navigation_revision':live,
              # Compatibility only. This used to be ambiguous for frozen snapshots.
              'default_revision':selected_revision,
              'default_revision_role':'deprecated alias for selected_snapshot_revision'}
    roots=[profile['repos'][repo_key]['path']]
    extra=profile.get('workspace_repositories',{}).get(repo_key,[])
    roots.extend(extra if isinstance(extra,list) else [extra])
    for root in roots:
        try:
            blob,text=source(root,target_revision,path,max_bytes=2_000_000)
            break
        except (OSError,ValueError,subprocess.SubprocessError):continue
    else:return {'status':'unavailable','reason':'source_not_available_at_requested_revision',
                 'source_revision':target_revision,**metadata}
    lines=text.splitlines();excerpt='\n'.join(lines[start_line-1:start_line-1+line_count])
    return {'status':'ok','source_revision':target_revision,'path':path,'blob_sha':blob,
            'start_line':start_line,'end_line':min(len(lines),start_line+line_count-1),'excerpt':excerpt,
            'excerpt_sha256':hashlib.sha256(excerpt.encode()).hexdigest(),'total_lines':len(lines),
            **metadata,
            'scope':'committed-source; requested revision may be an in-flight branch, not accepted default',
            'runtime_execution':'not-assessed'}
