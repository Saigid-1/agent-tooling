# T8 P4 golden: base-capsule.json

`base-capsule.json` is a capsule that the T8 base code stored, and what that code read back
for it. `tests/test_claim_note_budget.py` (P4) checks that the current code reads it unchanged.

- **Provenance.** `generate_base_capsule.py` produced it by running the T8 base code, exported
  from T8's frozen base in the private history, at this tree's example catalog
  (`config/desk-context`). The public history does not contain that base, so the file cannot
  be regenerated from the public history as it stands.
- **Bytes.** sha256 `ce9b04f33d8a35ccbf470b48f65c0459291c538562607ccdb35efe81774fb795`. Two runs of
  the generator at that base wrote identical bytes (D0a-3).
- **Next change.** The fixture's next legitimate change regenerates it from a public base, with
  `generate_base_capsule.py --base <public ref> --base-label <text>`, and this README will say so
  then.
