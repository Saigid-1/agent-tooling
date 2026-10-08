"""Pinned-source TypeScript import context, with explicit compiler configuration."""
import hashlib
import json
from pathlib import Path
import subprocess
from kp_agent_tooling._impl.source_citations import source


def context(repo, revision, repo_key, path, config):
    if not path.endswith(('.ts','.tsx','.js','.jsx','.mts','.cts','.mjs','.cjs')):
        return {'status':'unavailable','reason':'language_not_supported'}
    if not config:
        return {'status':'unavailable','reason':'typescript_runtime_not_configured'}
    blob, text = source(repo, revision, path)
    if len(text.encode()) > 1_000_000:
        return {'status':'unavailable','reason':'source_budget_exceeded'}
    compiler = Path(config['module'])
    digest = hashlib.sha256(compiler.read_bytes()).hexdigest()
    if digest != config['sha256']:
        return {'status':'unavailable','reason':'compiler_digest_mismatch'}
    module_path = Path(__file__).resolve()
    script = (module_path.parents[1]/'assets/typescript_import_context.cjs'
              if module_path.parent.name == '_impl'
              else module_path.parents[1]/'scripts/typescript_import_context.cjs')
    run = subprocess.run([config['node'],str(script),str(compiler)],
        input=json.dumps({'path':path,'text':text}),text=True,capture_output=True,timeout=20)
    if run.returncode or len(run.stdout.encode()) > 100_000:
        return {'status':'unavailable','reason':'compiler_process_failed'}
    result = json.loads(run.stdout)
    if hashlib.sha256(compiler.read_bytes()).hexdigest() != digest:
        return {'status':'unavailable','reason':'compiler_changed_during_query'}
    for row in result['imports']:
        row['next_call']={'tool':'knowledge.symbol','arguments':{
            'repo_key':repo_key,'target_revision':revision,'path':path,'line':row['lookup_line']}}
    return dict(result, path=path, source_revision=revision, blob_sha=blob,
        compiler_sha256=digest, absence_verdict='not-established',runtime_resolution='not-assessed',
        limitations=['Syntax is not declaration resolution or runtime binding.',
                     'Use the supplied SCIP continuation; package aliases and re-exports may remain unresolved.'])
