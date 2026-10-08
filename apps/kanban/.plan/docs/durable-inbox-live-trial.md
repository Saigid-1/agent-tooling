# Durable inbox transport acceptance — 2026-09-24

Reviewed source baseline: the fork's head at the time, plus the durable inbox change delivered with this record.

The parent reviewer exercised an isolated source CLI runtime on localhost port 3487 using a new repository and empty physical state directory on the external volume. Requests went through the real tRPC endpoints and WebSocket server. Two labelled synthetic recipients shared one desk. No model was dispatched, no production notes were used, and existing boards on 3484, 3485 and 3486 were not restarted.

All nine live checks passed:

1. Publication while disconnected replays the exact persisted envelope.
2. Acknowledgement for recipient A leaves recipient B pending.
3. Repeated acknowledgement returns acknowledged=true and replay=true.
4. Live WebSocket delivery matches the published envelope.
5. Repeated publication leaves one logical record.
6. A changed payload under the same event ID is refused.
7. Disconnect after receipt but before acknowledgement leaves delivery pending.
8. Different tenant/recipient selectors do not receive or acknowledge the intended events.
9. Process restart preserves both pending events and completed acknowledgements.

The independent runner and HTTP/WebSocket receipts are retained at:
`<operator workspace>/durable-inbox-live-02/`

`trial.mjs` contains the assertions, `receipts/receipt.json` their outcomes, numbered receipt files the HTTP requests/responses, and `websocket.jsonl` the observed frames. An earlier passing trial lives alongside it in `durable-inbox-live-01`. The final run starts with fresh state rather than reusing the first run.

Additional validation: 22 tests across inbox store/transport and existing observation/workspace persistence suites passed; the existing per-project snapshot/isolation WebSocket integration test also passed. Server and browser TypeScript checks passed. Review corrections addressed bounded reads and symlinks, strict subscription parsing, stale replay after a subscription switch, and acknowledgement response semantics.

## Evidence boundary

These are transport consumer receipts. They do not prove an LLM read a note, acted on it, or authenticated the sender. Tenant/session parameters are selectors within the existing trusted local runtime boundary, not independent identity credentials. Cross-harness delivery adapters, automatic publishing from the external legacy memory store, and a dedicated inbox UI were not exercised or introduced. Power-loss durability was not tested. Recipients are an explicit snapshot; a later desk occupant is not silently substituted. The first store is bounded to 512 events / 4 MiB per tenant/workspace and refuses new writes at capacity; archival remains future work.
