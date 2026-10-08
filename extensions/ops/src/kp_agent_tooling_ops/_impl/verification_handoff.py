"""Check exact JSON handoffs against validator receipts retained by this adapter."""
import json
import re
from kp_agent_tooling_ops._impl.verification_finding import finding_digest

MAX_TEXT = 200000


def artifacts_from_text(text):
    """Accept complete fenced JSON or one raw JSON document, never prose fragments."""
    blocks = re.findall(r'```(?:json)?\s*\n(.*?)```', text, re.S)
    if not blocks:
        blocks = [text.strip()]
    artifacts = []
    for block in blocks:
        try:
            value = json.loads(block)
        except ValueError:
            continue
        values = value if isinstance(value, list) else [value]
        artifacts.extend(v for v in values if isinstance(v, dict) and
                         str(v.get('schema_version', '')).startswith('ops.verification-finding.'))
    return artifacts


def check_handoff(final_text, expected_digests, receipts):
    result = {'schema_version':'ops.verification-handoff.v1',
              'semantic_verdict':'not-assessed', 'memory_promotion':'not-performed',
              'scope':'Exact artifacts in supplied final text; not proof of subsequent host publication'}
    errors = []
    if not isinstance(final_text, str) or len(final_text.encode()) > MAX_TEXT:
        return dict(result,status='invalid',errors=[{'code':'final_text_invalid_or_oversized'}])
    if not expected_digests or len(expected_digests)>20 or len(set(expected_digests))!=len(expected_digests):
        return dict(result,status='invalid',errors=[{'code':'bounded_unique_expected_digests_required'}])
    # Do not hide a second malformed/truncated artifact behind one valid artifact.
    if final_text.count('```') % 2:
        errors.append({'code':'unterminated_code_fence'})
    for block in re.findall(r'```(?:json)?\s*\n(.*?)```', final_text, re.S):
        try: json.loads(block)
        except ValueError: errors.append({'code':'malformed_json_block'})
    artifacts=artifacts_from_text(final_text)
    found={}
    for artifact in artifacts:
        try: digest=finding_digest(artifact)
        except (ValueError, TypeError):
            errors.append({'code':'invalid_json_artifact'});continue
        if digest in found:errors.append({'code':'duplicate_artifact','finding_sha256':digest})
        found[digest]=artifact
    for digest in expected_digests:
        receipt=receipts.get(digest)
        if receipt is None:errors.append({'code':'validation_receipt_unavailable','finding_sha256':digest})
        if digest not in found:errors.append({'code':'exact_artifact_missing','finding_sha256':digest})
        if receipt and receipt.get('status')!='valid':
            errors.append({'code':'validation_context_changed' if receipt.get('context_gap') else 'finding_validation_failed','finding_sha256':digest})
    for digest in found:
        if digest not in expected_digests:errors.append({'code':'unexpected_or_changed_artifact','finding_sha256':digest})
    return dict(result,status='invalid' if errors else 'valid',errors=errors,
                artifact_count=len(artifacts),checked_digests=expected_digests)
