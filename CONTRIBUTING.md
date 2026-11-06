# Contributing

Start with `./lab setup` and `./lab start`. The project has a Python core; Promptfoo is the matrix scheduler, not a second implementation of the agent.

## Before a change

Read [architecture](docs/architecture.md) and [security boundaries](docs/security.md). Do not connect real organization data or silently add a paid provider. State the expected behavior and how you will independently observe it. Keep recorded examples immutable; new results belong in a new run.

## Verify

From the checkout root, after setup:

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src/llm_test_lab
.venv/bin/pytest
.venv/bin/python -m llm_test_lab.contracts.schemas --check
.venv/bin/python scripts/check_docs.py
./lab run --source fixture --suite core --variants all --gate fixture-contract
```

Process tests execute real Node/Python workers and a bounded local HTTP stub. They do not require an API key and are not live-model tests. The full fixture matrix should match its expected outcomes, including intentional vulnerable failures.

If you change contracts, regenerate schemas with `.venv/bin/python -m llm_test_lab.contracts.schemas`, review the diff, then run the drift check. If you change an oracle, add positive and negative controls and update its version: never manufacture a pass by weakening an expectation.

A model refusal is not proof of tool enforcement. Test the actual forbidden call. For mutations, check durable state and audit, not just the final answer. Keep unknown/partial distinct from safe. Any live profile still needs separately recorded conformance and explicit budget.

## Dependencies and releases

Runtime pins live in `.python-version`, `.node-version`, `.uv-version`, `.npm-version`; dependency pins are `uv.lock` and `tooling/promptfoo/package-lock.json`. Do not use `npx latest`, global installs or hidden cache/retry fallback. Test dependency updates from a fresh checkout/venv, not just the existing development environment.

[Release readiness](docs/release-readiness.md) distinguishes tested engineering gates from unavailable live/human acceptance. Publication, commit, push and model spending are separate owner decisions.
