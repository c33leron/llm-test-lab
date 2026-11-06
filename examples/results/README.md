# Saved experiments

These are synthetic, scripted trials — not live-model results. Read the [walkthroughs](../../docs/demos/README.md) for the short version.

Folders starting with `2026-11-06-` hold the newer examples. Unprefixed folders preserve the earlier November 3 snapshot. Both were run on macOS with network access denied and no model calls.

| Snapshot | Smoke plan | Core plan | Timeout-control plan |
|---|---|---|---|
| November 6 | [9 attempts](source-plans/run-20261106T140810-8684912e.json) | [36 attempts](source-plans/run-20261106T140813-cc253b80.json) | [3 timeouts](source-plans/run-20261106T140826-374332ce.json) |
| November 3 | [9 attempts](source-plans/run-20261103T061249-9e440eb7.json) | [36 attempts](source-plans/run-20261103T061253-ed27496d.json) | [3 timeouts](source-plans/run-20261103T061304-818d7221.json) |

Each bundle keeps its original IDs, SQLite database, state snapshots, events, checks and hashes. The timeout controls preserve observed violations; incomplete evidence is not a safe result. Old bundles are not rewritten when the code changes.

From the project root, check their integrity with:

```sh
.venv/bin/python scripts/check_docs.py
```
