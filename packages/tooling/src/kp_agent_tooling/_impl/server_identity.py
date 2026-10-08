"""Identify the serving source checkout without claiming executable attestation."""
from pathlib import Path
import json
import re
import subprocess


_REVISION = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}\Z')
# Written by the final stage of every deploy/Dockerfile target from the SOURCE_REVISION
# build arg, the value of its org.opencontainers.image.revision label. A build without
# the arg declares "unknown", which is not a revision.
IMAGE_DECLARATION = Path('/usr/local/share/agent-tooling-build-identity.json')


def _declared(path):
    """The source revision a build declaration names, or None."""
    try:
        declared = json.loads(Path(path).read_text())['source_revision']
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return declared if isinstance(declared, str) and _REVISION.fullmatch(declared) else None


def _git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root, stderr=subprocess.DEVNULL,
                                   timeout=3).decode().strip()


def identity(config, config_sha256, *, source_root=None):
    root = Path(source_root) if source_root is not None else Path(__file__).resolve().parents[1]
    revision, reason = None, 'unversioned_source'
    try:
        if Path(_git(root, 'rev-parse', '--show-toplevel')).resolve() == root.resolve():
            candidate = _git(root, 'rev-parse', 'HEAD')
            if _REVISION.fullmatch(candidate):
                if _git(root, 'status', '--porcelain', '--untracked-files=no'):
                    reason = 'modified_tracked_source'
                else:
                    revision, reason = candidate, None
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError):
        pass
    origin = 'git-checkout'
    # The installed wheel has a build-time source declaration, not a live Git tree;
    # an image built from deploy/Dockerfile declares the revision it is labelled with.
    if revision is None and reason == 'unversioned_source' and source_root is None:
        for path, name in ((Path(__file__).with_name('build_identity.json'), 'package-build-declaration'),
                           (IMAGE_DECLARATION, 'image-build-declaration')):
            declared = _declared(path)
            if declared is not None:
                revision, reason, origin = declared, None, name
                break
    repos = config.get('repos', {})
    candidates = sorted((key, row) for key, row in repos.items() if isinstance(key, str))[:32] if isinstance(repos, dict) else []
    sources = {key: row['revision'] for key, row in candidates
               if isinstance(key, str) and len(key) <= 80 and isinstance(row, dict)
               and isinstance(row.get('revision'), str)
               and _REVISION.fullmatch(row['revision'])}
    evidence_revision = config.get('evidence_revision')
    if not isinstance(evidence_revision, str) or not _REVISION.fullmatch(evidence_revision):
        evidence_revision = None
    return {
        'schema_version': 'ops.agent-tooling-identity.v1',
        'status': 'known' if revision is not None else 'unknown',
        'identity_observed_at': 'server_startup',
        'server_build_revision': revision,
        'server_build_reason': reason,
        'server_build_origin': origin,
        'config_sha256': config_sha256,
        'config_digest_contract': 'SHA-256 of the exact raw JSON bytes parsed at server startup',
        'configured_navigation_source_revisions': sources,
        'configured_evidence_revision': evidence_revision,
        'executable_artifact_integrity': 'not-attested',
        'limitations': [
            'The build revision identifies source via a Git checkout, a package build declaration or an image build declaration; it does not attest executable artifact integrity.',
            'Untracked files that could shadow modules are not assessed.',
            'Configured navigation, catalog and evidence revisions are separate from the serving build; inspect their own source reports.',
        ],
    }
