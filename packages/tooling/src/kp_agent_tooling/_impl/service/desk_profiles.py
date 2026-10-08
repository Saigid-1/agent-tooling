"""Repository-independent desk profiles and metadata views over immutable sources.

Operator UI/CLI owns mutations. These annotations never grant memory admission.
With an ``agent-tooling.desk-registry.v1`` authority the profiles are the
registry's desks: roles come from the configured roster and each desk carries
its one binding, which only an operator or launcher bind can admit a session to.
"""
from contextlib import closing
from datetime import datetime, timezone
from importlib.resources import files
import json
import uuid

from kp_agent_tooling._impl.service.episodic_memory import _bytes, _id, EpisodeUnavailable
from kp_agent_tooling._impl.service.session_sources import SessionSources
from kp_agent_tooling._impl.service.desk_registry import (
    CONTEXT_DOC_BYTES, MAX_REPOS, ROSTER_SCHEMA, RoleRoster, desk_binding, is_registry, new_desk_binding,
)

_SCHEMA='''
CREATE TABLE IF NOT EXISTS desk_profiles (
 tenant TEXT, id TEXT, version INTEGER, payload BLOB, digest TEXT,
 PRIMARY KEY(tenant,id,version));
CREATE TABLE IF NOT EXISTS desk_session_contexts (
 tenant TEXT, session_id TEXT, version INTEGER, desk_id TEXT, payload BLOB, digest TEXT,
 PRIMARY KEY(tenant,session_id,version));
CREATE TABLE IF NOT EXISTS desk_session_repos (
 tenant TEXT, session_id TEXT, version INTEGER, repo TEXT,
 PRIMARY KEY(tenant,session_id,version,repo));
CREATE INDEX IF NOT EXISTS desk_context_profile ON desk_session_contexts(tenant,desk_id);
CREATE INDEX IF NOT EXISTS desk_context_repo ON desk_session_repos(tenant,repo);
'''


def _text(value, bound, name):
    if not isinstance(value,str) or not value.strip() or len(value)>bound:
        raise ValueError(f'{name} must contain 1..{bound} characters')
    return value.strip()


def _checked(row,kind):
    p=json.loads(row[0])
    if _id(kind,p)!=row[1]:raise ValueError('registry record integrity mismatch')
    return p


def latest_profiles(db,tenant):
    """Latest verified profile of every desk in one tenant."""
    rows=db.execute('SELECT p.payload,p.digest,p.tenant,p.id,p.version FROM desk_profiles p WHERE tenant=? AND version=(SELECT MAX(q.version) FROM desk_profiles q WHERE q.tenant=p.tenant AND q.id=p.id) ORDER BY p.id',(tenant,)).fetchall()
    result=[]
    for row in rows:
        p=_checked(row,'desk-profile')
        if (p['tenant_id'],p['desk_id'],p['version']) != tuple(row[2:]):raise ValueError('desk profile coordinates mismatch')
        result.append(p)
    return result


_LEGACY_FIELDS={'desk_id','name','description','role','expected_version'}
_DESK_FIELDS=_LEGACY_FIELDS|{'repos','capture','memory_write'}
_ANNOTATE_FIELDS={'source_session_id','desk_id','repos','adrs','cards','account_ref','provider','model','expected_version','source_ref'}

# Request errors name the offending field and are ValueErrors, so the registry CLI
# reports them as field errors, never as an unavailable registry.


def _fields(kind,request,required,optional=frozenset()):
    """The request as a dict with exactly ``required`` (plus any of ``optional``) fields."""
    if not isinstance(request,dict):raise ValueError(f'{kind} request must be a JSON object')
    missing=sorted(required-set(request));unexpected=sorted(set(request)-required-optional)
    if missing or unexpected:
        detail='; '.join(part for part in ('missing '+', '.join(missing) if missing else '',
                                            'unexpected '+', '.join(unexpected) if unexpected else '') if part)
        raise ValueError(f'exact {kind} fields required: {detail}')
    return request


def _canonical_desk_id(value):
    if isinstance(value,str):
        try:
            if value=='desk:'+str(uuid.UUID(value.removeprefix('desk:'))):return value
        except ValueError:pass
    raise ValueError('desk_id must be a canonical desk:<uuid> (canonical desk UUID required)')


def _expected_version(value):
    if type(value) is not int or value<0:raise ValueError('expected_version must be a nonnegative integer')
    return value


def _repos(value):
    if not isinstance(value,list) or len(value)>MAX_REPOS:raise ValueError('repos must be a list of at most 32 entries')
    return sorted({_text(x,512,'repos entry') for x in value})


def _flag(value,name):
    if type(value) is not bool:raise ValueError(f'{name} must be true or false')
    return value


def _context_doc(value):
    if value is None:return None
    if not isinstance(value,str):raise ValueError('context_doc must be text')
    if len(value.encode('utf-8'))>CONTEXT_DOC_BYTES:raise ValueError('context_doc exceeds 16 KiB')
    return value if value.strip() else None


class DeskProfiles:
    def __init__(self,store,session):
        self.store,self.session=store,session
        self.registry=store.registry if is_registry(store.registry) else None
        # A registry names its tenant; other authorities take it from the admitted operator session.
        self.tenant=self.registry.tenant_id if self.registry else store.sessions.resolve(session,store.registry).tenant_id

    def initialize(self):
        with closing(self.store._connect()) as db,db:db.executescript(_SCHEMA)
        return {'status':'ready','authorization_changed':False}

    def _ready(self,db):
        return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='desk_profiles'").fetchone())

    def roles(self):
        if self.registry:return self.roster()['roles']
        return json.loads(files('kp_agent_tooling').joinpath('assets/desk_roles.json').read_text())['roles']

    def roster(self):
        """The configured roster; legacy authorities keep their bundled role list."""
        if self.registry:return RoleRoster(self.registry.roster_path).read()
        roles=[{k:r[k] for k in ('role_id','label','purpose')} for r in self.roles()]
        return {'schema_version':ROSTER_SCHEMA,'version':0,'roles':roles,'source':'legacy'}

    def list(self):
        with closing(self.store._connect(readonly=True)) as db:
            if not self._ready(db):return []
            return latest_profiles(db,self.tenant)

    def view(self,p):
        """A profile with every desk field present; older profiles show their defaults."""
        return {**p,'repos':p.get('repos',[]),'capture':p.get('capture',False),'memory_write':p.get('memory_write',False),
                'context_doc':p.get('context_doc'),'binding_key':desk_binding(p,self.tenant)['binding_key'] if self.registry else None}

    def get(self,desk_id):
        return next((p for p in self.list() if p['desk_id']==desk_id),None)

    def save(self,request):
        """Save a desk. The five-field profile shape remains accepted and keeps any stored desk fields."""
        keys=set(request) if isinstance(request,dict) else set()
        full=_DESK_FIELDS<=keys<=_DESK_FIELDS|{'context_doc'}
        if keys!=_LEGACY_FIELDS and not full:
            # Name what is missing or unexpected; the desk-setting fields come together.
            settings=_DESK_FIELDS-_LEGACY_FIELDS
            _fields('desk',request,_DESK_FIELDS if keys&(settings|{'context_doc'}) else _LEGACY_FIELDS,
                    settings|{'context_doc'})
        desk_id=_canonical_desk_id(request['desk_id'])
        role=_text(request['role'],128,'role')
        if role not in {r['role_id'] for r in self.roles()}:raise ValueError('role: select a role from the configured roster')
        version=_expected_version(request['expected_version'])
        values={'name':_text(request['name'],120,'name'),'description':_text(request['description'],4000,'description'),'role':role}
        if full:
            values.update(repos=_repos(request['repos']),capture=_flag(request['capture'],'capture'),
                          memory_write=_flag(request['memory_write'],'memory_write'),context_doc=_context_doc(request.get('context_doc')))
        shown=self.view if self.registry else (lambda p:p)
        with closing(self.store._connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT payload,digest FROM desk_profiles WHERE tenant=? AND id=? ORDER BY version DESC LIMIT 1',(self.tenant,desk_id)).fetchone()
            old=_checked(row,'desk-profile') if row else None
            if old and old['version']==version+1 and all(k in old and old[k]==v for k,v in values.items()):return shown(old)
            if (old['version'] if old else 0)!=version:raise ValueError('desk changed; reload before saving')
            kept={k:old[k] for k in ('repos','capture','memory_write','context_doc','binding') if old and k in old}
            p={'desk_id':desk_id,'tenant_id':self.tenant,'version':version+1,**kept,**values}
            if self.registry:
                # Every registry desk has exactly one binding, fixed when the desk is created.
                for key,default in (('repos',[]),('capture',False),('memory_write',False),('context_doc',None)):p.setdefault(key,default)
                p.setdefault('binding',new_desk_binding(self.tenant,desk_id))
            p.update(recorded_at=datetime.now(timezone.utc).isoformat(),
                     authority='operator desk registry record; a binding admits a session only through an operator or launcher bind' if self.registry else 'operator profile metadata; not session admission')
            db.execute('INSERT INTO desk_profiles VALUES (?,?,?,?,?)',(self.tenant,desk_id,p['version'],_bytes(p),_id('desk-profile',p)))
        return shown(p)

    def contexts(self):
        with closing(self.store._connect(readonly=True)) as db:
            if not self._ready(db):return []
            rows=db.execute('SELECT c.payload,c.digest,c.tenant,c.session_id,c.version,c.desk_id FROM desk_session_contexts c WHERE tenant=? AND version=(SELECT MAX(q.version) FROM desk_session_contexts q WHERE q.tenant=c.tenant AND q.session_id=c.session_id)',(self.tenant,)).fetchall()
        result=[]
        for row in rows:
            p=_checked(row,'desk-session-context')
            if (p['tenant_id'],p['source_session_id'],p['version'],p['desk_id']) != tuple(row[2:]):raise ValueError('session context coordinates mismatch')
            result.append(p)
        return result

    def annotate(self,request):
        _fields('session metadata',request,_ANNOTATE_FIELDS)
        for k in ('source_session_id','desk_id'):
            if not isinstance(request[k],str) or not request[k]:raise ValueError(f'{k} must be a non-empty string')
        profile=self.get(request['desk_id'])
        if profile is None:raise ValueError('desk_id: desk is not registered in this tenant')
        sources=SessionSources(self.store)
        with closing(self.store._connect()) as db:
            known=sources.available(db) and db.execute('SELECT 1 FROM source_sessions WHERE id=? AND tenant=?',
                                                       (request['source_session_id'],self.tenant)).fetchone()
            if not known:raise ValueError('source_session_id: no captured source session of this tenant has this ID')
            sources._session(db,request['source_session_id'],self.tenant)
        values={k:request[k] for k in ('source_session_id','desk_id')}
        for k in ('repos','adrs','cards'):
            v=request[k]
            if not isinstance(v,list) or len(v)>32:raise ValueError(f'{k} must be a list of at most 32 entries')
            values[k]=sorted({_text(x,512,k) for x in v})
        for k in ('account_ref','provider','model'):
            values[k]=None if request[k] is None else _text(request[k],512,k)
        values['source_ref']=_text(request['source_ref'],1024,'source_ref')
        version=_expected_version(request['expected_version'])
        with closing(self.store._connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT payload,digest FROM desk_session_contexts WHERE tenant=? AND session_id=? ORDER BY version DESC LIMIT 1',(self.tenant,values['source_session_id'])).fetchone()
            old=_checked(row,'desk-session-context') if row else None
            if old and old['version']==version+1 and all(old[k]==v for k,v in values.items()):return old
            if (old['version'] if old else 0)!=version:raise ValueError('session context changed; reload before saving')
            p={**values,'tenant_id':self.tenant,'role':profile['role'],'version':version+1,'recorded_at':datetime.now(timezone.utc).isoformat(),'authority':'operator session annotation; not authenticated model testimony or access grant'}
            db.execute('INSERT INTO desk_session_contexts VALUES (?,?,?,?,?,?)',(self.tenant,p['source_session_id'],p['version'],p['desk_id'],_bytes(p),_id('desk-session-context',p)))
            db.executemany('INSERT INTO desk_session_repos VALUES (?,?,?,?)',[(self.tenant,p['source_session_id'],p['version'],repo) for repo in p['repos']])
        return p

    def source_ids(self,view):
        if not isinstance(view,dict) or set(view)-{'kind','value'}:raise ValueError('invalid memory view')
        kind=view.get('kind');value=view.get('value')
        if kind=='global':
            if value is not None:raise ValueError('global view takes no value')
            return None
        if kind not in ('agent','repo'):raise ValueError('view must be agent, repo or global')
        _text(value,512,'view value')
        contexts=self.contexts() # Verify immutable source metadata before selecting.
        if kind=='agent' and self.get(value) is None:raise EpisodeUnavailable('desk profile unavailable')
        # Relational projection, not a grant. Both arms join the current context version.
        with closing(self.store._connect(readonly=True)) as db:
            if not self._ready(db):
                return (SessionSources(self.store).project_ids(tenant_id=self.tenant,repo_key=value)
                        if kind=='repo' else set())
            if kind=='agent':
                rows=db.execute('SELECT c.session_id FROM desk_session_contexts c WHERE c.tenant=? AND c.desk_id=? AND c.version=(SELECT MAX(q.version) FROM desk_session_contexts q WHERE q.tenant=c.tenant AND q.session_id=c.session_id)',(self.tenant,value)).fetchall()
            else:
                rows=db.execute('SELECT c.session_id FROM desk_session_contexts c JOIN desk_session_repos r ON r.tenant=c.tenant AND r.session_id=c.session_id AND r.version=c.version WHERE c.tenant=? AND r.repo=? AND c.version=(SELECT MAX(q.version) FROM desk_session_contexts q WHERE q.tenant=c.tenant AND q.session_id=c.session_id)',(self.tenant,value)).fetchall()
        expected={p['source_session_id'] for p in contexts if (p['desk_id']==value if kind=='agent' else value in p['repos'])}
        if {r[0] for r in rows}!=expected:raise ValueError('session metadata projection mismatch')
        if kind=='repo':
            expected |= SessionSources(self.store).project_ids(tenant_id=self.tenant,repo_key=value)
        return expected

    def charter_aliases(self, desk_id):
        """Verify imported alias provenance before using it as a view filter."""
        with closing(self.store._connect(readonly=True)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='desk_charter_aliases'").fetchone():return set()
            rows=db.execute('SELECT a.alias,i.payload,i.digest FROM desk_charter_aliases a JOIN desk_charter_imports i ON i.digest=a.import_digest WHERE a.tenant=? AND a.desk_id=? AND i.tenant=a.tenant',(self.tenant,desk_id)).fetchall()
        import hashlib
        aliases=set()
        for alias,raw,digest in rows:
            if hashlib.sha256(raw).hexdigest()!=digest:raise EpisodeUnavailable('charter provenance mismatch')
            manifest=json.loads(raw)
            if not any(p['profile']['desk_id']==desk_id and alias in p['aliases'] for p in manifest['profiles']):raise EpisodeUnavailable('charter alias mismatch')
            aliases.add(alias)
        return aliases

    def view_filter(self, view):
        """A view as a filter over the scope projection: verified session IDs and agent aliases.

        ``None`` IDs (the global view) filter nothing. The projection evaluates
        the same rule as ``filter_episodes``; returned records are re-checked
        against their sealed claims.
        """
        ids=self.source_ids(view)
        if ids is None:return {'ids':None,'agent':False,'aliases':set()}
        agent=view['kind']=='agent'
        return {'ids':ids,'agent':agent,'aliases':self.charter_aliases(view['value']) if agent else set()}

    def filter_episodes(self, view, selected):
        ids=self.source_ids(view)
        if ids is None:return selected,0
        kept={key:item for key,item in selected.items() if item['source_session_id'] in ids}
        if view['kind']!='agent':return kept,0
        aliases=self.charter_aliases(view['value']);unverified=0
        for key,item in selected.items():
            if key in kept or item['binding_key'] not in aliases or item['attribution_status']!='resolved':continue
            # A captured desk binding is direct provenance. A relayed bounded
            # session claim cannot assign an entire transcript to that desk.
            if item['storage_binding']==item['binding_key']:
                kept[key]=item;continue
            meta=item['metadata']
            owners=[c for c in meta['active_claims'] if c['predicate']=='session.owner'] if meta else []
            if owners and all(c['valid_from'] is None and c['valid_until'] is None for c in owners):
                kept[key]=item
            else:unverified+=1
        return kept,unverified

    def directory(self):
        sources=SessionSources(self.store)
        with closing(self.store._connect()) as db:
            rows=db.execute('SELECT id,payload FROM source_sessions WHERE tenant=? ORDER BY id LIMIT 501',(self.tenant,)).fetchall() if sources.available(db) else []
            sessions=[{'source_session_id':r[0],**sources._session(db,r[0],self.tenant)} for r in rows[:500]]
        roster=self.roster()
        if self.registry:
            from kp_agent_tooling._impl.service.session_bindings import list_records
            bindings=list_records(self.store.sessions)
        else:bindings=[]
        return {'desks':[self.view(p) for p in self.list()],'roles':self.roles(),'roster_version':roster['version'],'roster_source':roster['source'],
                'registry_mode':'registry' if self.registry else 'legacy','bindings':bindings,
                'contexts':self.contexts(),'sessions':sessions,'session_limit_reached':len(rows)>500,'tenant_id':self.tenant,'authorization_changed':False}
