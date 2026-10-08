# Local container session import

The board image includes the portable memory CLI (`kp-agent-session-import`),
built from the same source revision as the board. It includes no embedding models,
legacy service, host virtualenv or Docker socket. The existing bounded CLI
and admission checks are reused unchanged. Without explicit configuration the
Import history dialog remains unavailable with setup instructions.

For a local, single-operator deployment, render the runtime root with
`kp-agent-install` (`plan`, `apply`, then `prepare`) and the `board` component
([DOCKER.md](DOCKER.md)). The former board import overlay is folded into the
manifest's `board` role, which mounts, from the runtime root `$root`:

- `$root/config/` read-only at `/config`. Put `board/session-import.json`,
  `board/catalog.json` (0600) and the referenced doctrine under `$root/config/board/`.
- `$root/state/` at `/state`, and the project's store volume `<project>_memory` at
  `/state/memory` ([The store volume](DOCKER.md#the-store-volume)). Keep the
  initialized portable memory and exact-session admission in the volume, for example
  in `/state/memory/<desk>` (0700). Create that directory inside the runtime, never on
  the host:

  ```sh
  docker compose --project-directory "$root" run --rm -T tooling sh -c 'mkdir -m 700 /state/memory/<desk>'
  ```

- `$root/import-sources/` read-only at `/import-sources`. Put only the native
  transcript files deliberately selected for import here.

The manifest sets `KANBAN_SESSION_IMPORT_CONFIG=/config/board/session-import.json`.
Use container paths inside it
([example](../deploy/examples/board-session-import.example.json)):

- schema `ops.desk-memory.local.v1`;
- `state_root: /state/memory/<desk>`;
- `catalog_path: /config/board/catalog.json`;
- `workspace_root: /config/board`;
- the operator-selected `provider_instance` and exact `provider_session_id`.

Do not copy fixture approvals into a production catalog. Initialize and admit
through the installed portable desk setup tools, run inside the runtime
(`docker exec` or `docker compose ... run`), under explicit operator selection; image
startup does neither. The store files are never opened from the host. Mounting a transcript does not establish
its ownership. Preview and owner assertion remain separate actions.

```sh
docker compose --project-directory "$root" up -d board
```

Only this service is selected. Other tooling consumers can continue independently.
The board and its admitted import actor are one trusted local operator boundary;
this is not a shared multi-user hosting configuration. Changing the container's
session config requires an explicit new admission, not a browser-supplied identity.
For a future remote/multi-user installation retain the narrow authenticated
import-service boundary described in COMPOSABLE-DEPLOYMENT.md.

Use an isolated Compose project, fresh memory state and synthetic transcripts for
the first dry run. Verify unauthenticated refusal, unadmitted refusal, preview,
apply, capture, source retrieval, owner assertion, restart/replay and follow/stop.
Keep receipts private and record the exact image ID and source label. No global
hooks or host MCP registrations need to change for this test.

Follower recovery runs at startup and every 30 seconds, without overlapping
checks. It respects an existing two-minute worker lease. After abrupt container
termination, recovery may therefore take about 150 seconds plus command time.
The board stops its recovery timer on shutdown; replay retains immutable source
identity and does not duplicate captured rows.

To repeat the synthetic acceptance from the repository checkout, use the Python
that has this checkout's package installed:

```sh
python scripts/trial_kanban_import.py --root /absolute/physical/fresh-trial \
  --image IMAGE_DIGEST --port 3488
```

The parent directory must already exist. The script:

- renders `runtime/` with `kp-agent-install` (tooling and board components)
  around a synthetic committed fixture repository;
- writes the fixture import configuration;
- starts only the `board` role, not tooling or telemetry;
- leaves the fixture board running for inspection;
- writes a private `receipt.json` and never prints the passcode.

The default Docker command is `docker`, overridable with `--docker`. The script
grants only a synthetic fixture actor inside the fresh trial store and makes no
real model calls. Since S3 it expects the single product image (`kanban` command,
package on `PATH`), which AT-0004 T6a plans to build from this tree. It has not
been re-run against that image. It predates the store volume (T9b): it still creates
its trial store under `runtime/state/memory` from the host and does not run
`kp-agent-install prepare`, so it needs those two steps updated before it is re-run.

The synthetic image trial passed for a recorded runtime source revision: authenticated
browser import, explicit admission, replay, follower restart after natural lease
expiry, appended-source capture, stop, and native MCP/CLI source-search parity.
The receipt is kept with the operator's records (not published).
Later documentation/receipt commits do not relabel that tested image. No fresh
navigation-provider, remote hosting, real transcript, or model-provider trial is
claimed by this board/import acceptance.
