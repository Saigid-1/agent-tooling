# Workspace Assistant memory: first local slice

This slice uses the portable desk registry, exact-session admission, episode store,
literal search index, cited capsules and consolidation queue. The operator creates
one private `ops.imported-desk-catalog.v1` binding with `role: "assistant"` and a
stable `(tenant_id, role, repo_key)` tuple. That tuple determines the binding key;
provider and model are session provenance, not the assistant's identity. Each new
provider session needs an explicit admission receipt for the same binding key.

Use a dedicated private directory for the assistant's `state_root` (mode `0700`),
distinct from every project desk state root. Its `sessions.sqlite3`,
`episodes.sqlite3`, `episode-search.sqlite3`, queue database and any capture or hook
receipts belong there. The assistant config uses schema
`ops.assistant-memory.local.v1` and the usual local desk fields plus
`assistant_policy_path`, which names an existing private JSON file (mode `0600`)
outside the state root.

Initialization requires an empty state root and writes a private
`assistant-owner.json` binding marker. An assistant config refuses populated
unmarked state, and a legacy desk config refuses a marked assistant root. This
prevents accidental adoption of an existing global desk store through a path typo.

The policy format is:

```json
{
  "schema_version": "ops.assistant-summary-policy.v1",
  "focus_questions": ["What observed outcome supports this decision?"]
}
```

The fixed assistant policy asks the summarizer to assess contextual decision
quality and structural soundness. It keeps user instructions, observed patterns
and tentative inferences separate; cites concrete decisions and observed outcomes;
and carries counterevidence, corrections, supersession, plausible alternatives
and uncertainty. Acceptance or friction alone is not quality evidence. Questions
do not establish competence, and cited source bytes do not prove an inferred
lesson. Operator focus questions can narrow the assessment without replacing
these rules. The policy is included in the trusted system prompt for a
`queue_cli work-once` proposal and its digest appears in the existing call
receipt. Reserve enough system budget for the full prompt.

Initialize the assistant with `kp-agent-desk --config ... initialize`, then admit
each exact host session with `kp-agent-desk --config ... admit`,
then use `memory_cli` for source recovery and cited proposals. Use `queue_cli`
with `--queue` set inside this assistant's state root. That CLI already rejects a
queue path outside the configured state root. No search across a global store is
configured by this assistant schema; the only imported binding is its own.

This first slice proves private state composition, explicit provider changes,
source and correction recovery, public CLI/MCP scope, and policy transport to a
stubbed summarizer request. It does not prove model assessment quality, host hook
emission, automatic capture, UI admission, or a live external summarizer call.
Capture and hook receipt adapters require their own explicit paths; no assistant
hook is installed by this slice. Do not point those adapters at another desk's
state or infer full capture from these tests.
