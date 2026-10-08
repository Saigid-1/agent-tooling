# Standalone message publisher

The inbox is the durable message source. No legacy graph, embeddings, desk registry, model provider, or external desknote writer is required. The existing trusted-local Kanban runtime supplies transport and workspace routing. Its storage can later be extracted behind the same message contract.

## Publish

Use `kanban inbox-send --endpoint http://127.0.0.1:3487 --file /absolute/path/message.json`.
For this source checkout, use `node --import tsx src/cli.ts inbox-send` with the same options.
The runtime must be started from the updated source/build, with a physical `KANBAN_STORAGE_ROOT` on the mounted external volume. An already-running older server does not acquire this endpoint automatically.

```json
{
  "message_id": "handoff-001",
  "tenant_id": "local-team",
  "workspace_id": "repo",
  "destination_desk_id": "coordinator",
  "recipient_session_ids": ["session-a", "session-b"],
  "sender": {"session_id": "sender-session", "desk_id": "implementation"},
  "created_at": "2026-09-24T12:00:00Z",
  "text": "Please review the committed slice and its test receipt."
}
```

Save the exact input before calling. The CLI reads a bounded regular file, does not follow a leaf symlink, allows only an explicit loopback HTTP origin, rejects redirects, and times out after ten seconds. Remote access can use a local tunnel; this command does not collect subscription credentials. Identifiers follow the existing inbox contract; `message_id` has a 120-character limit, text 4,000 characters, and the final event 8 KiB. Use opaque binding/session IDs for labels containing spaces.

`inbox.send` computes SHA-256 over the exact UTF-8 text and creates the existing inbox envelope. Its `desknote` field is a compatibility name for the inline message, with a `sha256:` reference; it is not a legacy graph reference. Recipient order is normalized, duplicates are refused, and event ID is `message:<message_id>`. Repeating identical input returns `replay: true`; changing text, routing, sender, or time under that ID is refused. Deliberate new publications need a new ID. The hash establishes byte identity, not authorship or authority.

Persistence reuses the bounded namespace store, with a lock, mode-0600 temporary file, file sync, atomic rename and directory sync before notification. Delivery is at least once. If the process stops after persistence but before notification, pending reads/reconnect recover the message. No automatic background retry queue is added: if the publisher cannot confirm success, it exits 1 and directs the caller to retain and retry the same file. Never delete retry input on timeout. Acknowledgement is transport receipt, not model reading or action.

The existing `inbox.pending`, `inbox.ack` and WebSocket `inbox_subscribe` contract are unchanged; see [durable-inbox.md](durable-inbox.md). There is no automatic desk-wide recipient discovery, model injection, or new visual inbox in this slice. Sender and recipient IDs remain declarations under the trusted-local runtime boundary, not authenticated cross-tenant identities.

## Verification, 2026-09-24

Ten focused regression tests passed; server TypeScript checking passed. An isolated real runtime trial retained twelve passing checks at `<operator workspace>/standalone-inbox-live-01/receipts/receipt.json`:

- CLI failure while runtime stopped, successful retry, generated content digest, duplicate retry.
- Offline replay, exact live WebSocket payload, changed-ID refusal, reconnect recovery.
- Independent acknowledgements for concurrent recipients, acknowledgement replay, scope isolation, restart persistence.

Synthetic recipients used the real CLI, HTTP, WebSocket and file-store paths. No production note was read or published, no LLM session was invoked, and existing board processes were not restarted. Abrupt machine power loss was not tested. The disposable runtime was stopped after verification.
