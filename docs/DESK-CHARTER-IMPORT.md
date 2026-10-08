# Import existing desk charters

With the OPS extension (`extensions/ops`) installed, use
`python -m kp_agent_tooling_ops.charter_import_cli --config PRIVATE_CONFIG
--manifest PRIVATE_MANIFEST preview`. Apply the identical reviewed bytes with
`--expected-sha256 RAW_FILE_SHA256 apply`.

The private manifest uses schema `agent-tooling.charter-import.v1`, a tenant_id,
`sources` containing exact text with sha256 and revision/path provenance, and
`profiles` containing an existing desk save request plus explicit legacy aliases.
Map existing profiles by ID first; never match only a model or current cwd.
Each source's complete UTF-8 bytes are hashed. Full source text is retained in
`desk_charter_imports`; `desk_charter_aliases` maps old identifiers to profiles.
Existing aliases cannot silently move to another desk. Preview refuses changed
versions or duplicate names needing an explicit identity decision. Apply reuses
the profile save interface; interruption is replayable, not a cross-table atomic
transaction. A successful replay adds no profile versions or aliases.

This is a metadata import. It does not rewrite historical session claims, extend
their time intervals, admit sessions, grant writes, or install source capture.
Historical mapping reports may resolve existing claims through the aliases;
unresolved/conflicting claims remain unresolved/conflicting. Existing session
annotations remain untouched. In particular, aliases alone do not manufacture
whole-session annotations for time-bounded desk occupancy.

Standing desks and per-dispatch role templates are distinct. Preserve templates
without inventing desk instances. Human-authored charters are source evidence,
not agent seats. Superseded charters, expired overlays, old model assignments and
legacy dispatch directions stay in source history; importing them does not
reactivate them. Current operator rulings take precedence.

Desk memory views follow verified imported aliases to directly bound legacy
episodes. Unbounded, resolved historical ownership claims may also contribute.
Time-bounded claims without sufficient episode-time evidence are excluded and
counted as historical_claim_intervals_unverified. No time interval is widened.
