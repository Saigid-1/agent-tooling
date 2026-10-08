# Contributing to agent-tooling

Thank you for your interest. This repository is licensed under the GNU Affero
General Public License, version 3 only (`AGPL-3.0-only`; see [LICENSE](LICENSE)),
except `apps/kanban`, which is Apache-2.0 (see `apps/kanban/NOTICE`). The
third-party components it contains or builds into its images keep their own
licences, listed in [NOTICE](NOTICE).

The copyright notice for the project is:

    Copyright 2026 Andrew Wedding and the agent-tooling contributors

## How to contribute

1. **Open an issue first for anything larger than a small fix.** Describe the
   problem or the proposal, so the design can be agreed before you write code.
2. **Fork, branch from `main`, and keep each pull request to one change.**
   Describe what the change does and how you checked it.
3. **Add or update tests with the change, and run the suites that cover it.**
   The CI workflow (`.github/workflows/tooling.yml`) runs the same commands:
   - Core package (Python 3.11 or 3.12):
     `python -m pip install -e './packages/tooling[test]'`, then
     `python -m pytest tests -q`.
   - OPS extension: `python -m pip install -e './packages/tooling[test]' -e './extensions/ops[test]'`,
     then `python -m pytest extensions/ops/tests -q`.
   - Kanban board (Node 22+), in `apps/kanban`: `npm ci`, `npm ci --prefix web-ui`,
     `npm run typecheck`, `npm test`, `npm --prefix web-ui run test` and
     `npm run build`.
4. **Keep private material out of commits.** That covers credentials, private
   state, transcripts, session identifiers, personal email addresses and
   machine-local absolute paths (home directories, mounted volumes). The test
   `tests/docs/test_release_hygiene.py` refuses these classes in tracked files.
5. **Sign off every commit** under the Developer Certificate of Origin, as
   described below.

`apps/kanban` is a fork of [Cline Kanban](https://github.com/cline/kanban).
Its own `apps/kanban/CONTRIBUTING.md` is the upstream project's guide.
Contributions to this repository, including changes under `apps/kanban`, follow
this file. A change meant for upstream Kanban belongs in `cline/kanban`, under
that project's terms.

## Fixtures that cannot be regenerated from the public history

Two test fixtures were generated from product trees that are in the project's
private history only. Their tests compare against them as committed.

- `tests/boundary/fixtures/pre_move_tools_list.json` is the frozen capture of
  what the navigation server's `tools/list` returned, per configuration, before
  the OPS code moved into `extensions/ops`. It cannot be regenerated from the
  public history, because its capture command
  (`python tests/boundary/t1_harness.py capture`) refuses every product tree but
  the pre-move one, whose content hash `tests/boundary/t1_contract.py` pins as
  `PRE_MOVE_PRODUCT_SHA256`, and that tree is not in the public history. Its
  test, `tests/boundary/test_t1_p3_tool_surface.py`, compares tool names and
  input schemas only.
- `tests/fixtures/t10-read-path-projection/` holds the T10 goldens, the memory
  read path's outputs over a fixed corpus, generated with a frozen clock by its
  `generate.py`. They cannot be regenerated from the public history, because
  the generator refuses every product tree but the base it was written for,
  whose content hash it pins as `BASE_PRODUCT_SHA256`, and that tree is not in
  the public history (its docstring says so). Their test,
  `tests/test_t10_p5_identical_results.py`, compares the read path's results
  against the golden and checks that the golden records the generator's own
  hash and the base product's content hash.

## Sign-off: the Developer Certificate of Origin

This project uses the Developer Certificate of Origin (DCO) 1.1, reproduced in
full below. There is no Contributor License Agreement (CLA). The DCO sign-off is
the only certification asked of contributors.

Every commit must carry a `Signed-off-by:` trailer with your name and email
address. By adding it, you certify the DCO for that commit. Git adds the
trailer when you commit with `-s`:

```sh
git commit -s -m "Describe the change"
```

This produces a trailer such as:

```text
Signed-off-by: Jane Developer <jane.developer@example.com>
```

To sign off commits you have already made, use `git commit --amend -s` for the
last commit, or `git rebase --signoff main` for every commit on your branch.
A pull request is merged only when every one of its commits carries a sign-off.

Your contribution is submitted under the licence indicated for the files it
touches; see LICENSE and NOTICE. As item (d) of the DCO states, the sign-off,
including your name and email address, becomes part of the public record of
the project.

### Developer Certificate of Origin 1.1

The text below is reproduced verbatim from <https://developercertificate.org/>.

```text
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```
