"""Deterministic source attribution and review signals, never semantic proof.

Checks run after exact citation validation in EpisodeStore. English lexical
qualifiers are a safety net, not exhaustive negation or entailment detection.
"""
import re

from kp_agent_tooling._impl.service.episodic_memory import EpisodeBoundExceeded
from kp_agent_tooling._impl.service.episodic_provenance import origin as _origin

VERSION = 'ops.summary-evidence.v1'
QUALIFIER_SENTENCES = 64       # negated source sentences one capsule carries
QUALIFIER_SENTENCE_BYTES = 2000  # UTF-8 bytes of one carried sentence
OMITTED = 'source_qualifiers_omitted'
_NEGATIVE = re.compile(r"\b(?:not|never|neither|unverified|unresolved|unapproved|unimplemented|undeployed|without|no|hasn't|haven't|wasn't|weren't|didn't|isn't|aren't|cannot|can't)\b", re.I)
_RELATION = re.compile(r'\b(?:after|before|until|unless|requires?|prerequisites?|must|because|therefore|thus|proves?|implies?|hence|consequently)\b', re.I)
_EXECUTION = re.compile(r'\b(?:implemented|deployed|executed|passed|verified|succeeded|safe|correct)\b', re.I)


def sentences(text):
    # Preserve exact Unicode coordinates and punctuation; no rewritten quotes.
    for match in re.finditer(r'[^.!?\n]+(?:[.!?]+|(?=\n)|$)', text):
        a, b = match.span()
        while a < b and text[a].isspace(): a += 1
        while b > a and text[b-1].isspace(): b -= 1
        if a < b: yield a, b, text[a:b]


def _negated(text):
    return ((start, end, quote) for start, end, quote in sentences(text) if _NEGATIVE.search(quote))


def _qualifier(episode_id, record, event, start, end, quote):
    return {'role':event['role'], **_origin(record, event['event_id']),
            'citation':{'episode_id':episode_id,'event_id':event['event_id'],
                        'start':start,'end':end,'quote':quote}}


def qualifiers(records):
    """Every event of every source episode; refuse what one capsule cannot carry.

    Used by the consolidation pre-flight, which must refuse before any paid
    proposal call, and by direct EpisodeStore.consolidate callers. The capsules of
    memory.propose and the summarizer paths use CitedQualifiers.
    """
    result = []
    for episode_id, record in records.items():
        for event in record['events']:
            for start, end, quote in _negated(event['text']):
                if len(result) >= QUALIFIER_SENTENCES or len(quote.encode()) > QUALIFIER_SENTENCE_BYTES:
                    raise EpisodeBoundExceeded('source_qualifier_budget',
                        'source qualifier budget exceeded; split source episodes explicitly')
                result.append(_qualifier(episode_id, record, event, start, end, quote))
    return result


class CitedQualifiers:
    """Negated sentences of the cited events only; what does not fit is counted, never refused.

    Only the (episode_id, event_id) pairs named by citations are scanned. When
    they hold more than QUALIFIER_SENTENCES sentences, sentences overlapping a
    cited range are kept first, then the rest in source order. A sentence longer
    than QUALIFIER_SENTENCE_BYTES is omitted. drop() omits the lowest-ranked kept
    sentence so the handoff fits its byte budget. Kept qualifiers are reported in
    source order; every omission is recorded per cited event, and any omission
    marks the capsule review_required.
    """

    def __init__(self, records, items):
        ranges = {}
        for item in items:
            for ref in item['citations']:
                ranges.setdefault((ref['episode_id'], ref['event_id']), []).append((ref['start'], ref['end']))
        self.scanned, self.found, self.omitted = [], 0, {}
        candidates = []
        for episode_id, record in records.items():
            for event in record['events']:
                key = (episode_id, event['event_id'])
                if key not in ranges:
                    continue
                self.scanned.append(key)
                for start, end, quote in _negated(event['text']):
                    self.found += 1
                    if len(quote.encode()) > QUALIFIER_SENTENCE_BYTES:
                        self._omit(key, 'sentence_bytes')
                        continue
                    near = any(start < b and a < end for a, b in ranges[key])
                    candidates.append(((not near, len(candidates)), key,
                                       _qualifier(episode_id, record, event, start, end, quote)))
        ranked = sorted(candidates, key=lambda candidate: candidate[0])
        self.kept = ranked[:QUALIFIER_SENTENCES]
        for _, key, _ in ranked[QUALIFIER_SENTENCES:]:
            self._omit(key, 'sentence_count')

    def _omit(self, key, bound):
        counts = self.omitted.setdefault(key, {})
        counts[bound] = counts.get(bound, 0) + 1

    def drop(self):
        """Omit the lowest-ranked kept sentence to fit the handoff byte budget."""
        if not self.kept:
            return False
        self._omit(self.kept.pop()[1], 'handoff_bytes')
        return True

    def qualifiers(self):
        return [value for _, _, value in sorted(self.kept, key=lambda candidate: candidate[0][1])]

    def record(self):
        scanned = []
        for episode_id, event_id in self.scanned:
            if scanned and scanned[-1]['episode_id'] == episode_id:
                scanned[-1]['event_ids'].append(event_id)
            else:
                scanned.append({'episode_id':episode_id, 'event_ids':[event_id]})
        omissions = [{'episode_id':key[0], 'event_id':key[1],
                      'omitted_sentences':sum(self.omitted[key].values()),
                      'by_bound':dict(sorted(self.omitted[key].items()))}
                     for key in self.scanned if key in self.omitted]
        return {'scope':'cited_events', 'scanned':scanned, 'scanned_events':len(self.scanned),
                'budget':{'sentences':QUALIFIER_SENTENCES, 'sentence_bytes':QUALIFIER_SENTENCE_BYTES},
                'qualifier_sentences':self.found, 'kept_sentences':len(self.kept),
                'omitted_sentences':self.found - len(self.kept), 'omissions':omissions}

    def review(self, evidence_review):
        result = dict(evidence_review, qualifier_scope='cited_events')
        omitted = self.found - len(self.kept)
        if omitted:
            result.update(status='review_required', reasons=[OMITTED],
                          omitted_qualifier_sentences=omitted,
                          omission_record='source_qualifier_scan.omissions')
        return result

    def apply(self, handoff, evidence_review):
        handoff['source_qualifiers'] = self.qualifiers()
        handoff['source_qualifier_scan'] = self.record()
        handoff['evidence_review'] = self.review(evidence_review)


def item_evidence(item, records, events):
    attribution = []
    matched = False
    for index, ref in enumerate(item['citations']):
        event = events[(ref['episode_id'], ref['event_id'])]
        attribution.append({'citation_index':index,'role':event['role'],
                            **_origin(records[ref['episode_id']], ref['event_id'])})
        # Substrings (e.g. "deployed" from "not deployed") cannot qualify.
        matched |= any(item['text'] == text and ref['start'] <= start and end <= ref['end']
                       for start, end, text in sentences(event['text']))
    reasons = []
    if not matched:
        reasons.append('interpretation_requires_review')
        if _RELATION.search(item['text']): reasons.append('relationship_requires_support')
        if _EXECUTION.search(item['text']): reasons.append('execution_or_conclusion_requires_support')
    return {'attribution':attribution,'evidence_check':{
        'status':'source_text_matched' if matched else 'review_required',
        'semantic_entailment':'not-assessed','reasons':reasons,
        'next_step':'read_cited_source_then_verify_claim'}}


def review(items, questions):
    return {'version':VERSION,'semantic_entailment':'not-assessed',
            'review_required_item_indices':[i for i,item in enumerate(items)
                if item['evidence_check']['status']=='review_required'],
            'unlinked_question_indices':list(range(len(questions))),
            'qualifier_coverage':'English lexical candidates only; not exhaustive',
            'boundary':'Attributed source assertions, not independent execution evidence'}
