"""Operator-owned quality policy for an isolated assistant desk.

This changes the trusted summarizer instruction, never the source/citation authority.
"""
from pathlib import Path

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.service.desk_binding import call_memo

POLICY_VERSION = 'ops.assistant-summary-policy.v1'
QUALITY_INSTRUCTIONS = (
    'Assess the quality and structural soundness of a concrete decision in its context: '
    'the goal, constraints, considered alternatives, evidence, observed outcome and limits. '
    'User acceptance, a follow-up question, or friction alone does not establish quality or error. '
    'Separate explicit user instructions, observed patterns and tentative inferences. '
    'Cite concrete decisions and observed outcomes; identify counterevidence, plausible alternative '
    'explanations, corrections and superseded preferences. State uncertainty and missing runtime proof. '
    'Do not diagnose the user, infer competence from questions, or turn a situational assessment '
    'into a permanent trait. A valid source citation does not establish the inferred lesson.'
)


def load_policy(path):
    value = leaf.read_private_json(Path(path), memo=call_memo())
    if (set(value) != {'schema_version', 'focus_questions'} or
            value['schema_version'] != POLICY_VERSION or
            not isinstance(value['focus_questions'], list) or
            len(value['focus_questions']) > 4 or
            any(not isinstance(q, str) or not q.strip() or len(q.encode()) > 250
                for q in value['focus_questions'])):
        raise ValueError('invalid assistant summary policy')
    return QUALITY_INSTRUCTIONS + ' Focus questions: ' + ' '.join(value['focus_questions'])
