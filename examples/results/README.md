# Verified synthetic evidence

Current demo folders start with **2026-11-06-**. They preserve fixture-origin bundles from the accepted macOS offline runs after the onboarding/report/oracle changes. No model or API key was used.

- Smoke: `run-20261106T140810-8684912e` — 9 completed, fixture-contract passed.
- Core: `run-20261106T140813-cc253b80` — 36 completed, fixture-contract passed, including two expected violations.
- Timeout control: `run-20261106T140826-374332ce` — 3 expected timeouts; violation remains visible, lab exit 3.

Source plans preserve input hashes. Every demo retains original IDs, SQLite, state snapshots, event logs, checks and hashes. `scripts/check_docs.py` verifies the bundles. Source is still fixture, even when viewing a saved report is called recorded mode.

Earlier unprefixed folders below are historical November 3 evidence. They have not been silently regenerated or rewritten.

---

# Verified synthetic evidence

These are byte-for-byte copies of **fixture-origin** attempt bundles executed through managed Promptfoo on 2026-11-03. They are not live LLM observations. Viewing them is recorded mode and performs no inference. No real organization data or model credentials are included.

- Core run: `run-20261103T061253-ed27496d` —36 completed, fixture-contract exit 0.
- Smoke run: `run-20261103T061249-9e440eb7` —9 completed, fixture-contract exit 0.
- Interrupted control: `run-20261103T061304-818d7221` —3 timeouts, lab exit 3 as expected; violations preserved.

All three ran with macOS `sandbox-exec` denying network access to the entire managed process tree. The deny profile was independently checked by a loopback connect returning EPERM. All attempts report0 model dispatches. No live model was contacted.

Each bundle preserves original IDs, sourceMode=fixture, manifest hashes, SQLite, snapshots, events, checks and reports. `source-plans/` preserves the exact plan hash and input-file hashes, including author Python source. The repository had no commit during implementation, so gitSHA is null rather than fabricated. The final source can be checked against those hashes before reproduction.

Exports were made using `scripts/export_demo.py`; it rejects non-fixture evidence, invalid hashes, ambiguous attempts and overwrites. `scripts/check_docs.py` verifies these bundles and documentation links without network access. A rescore creates a separate result, never an altered original.
