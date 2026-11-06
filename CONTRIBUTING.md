# Contributing

Run `./lab setup` first. For a new scenario, start with [the extension guide](docs/extending.md); for code changes, see [architecture](docs/architecture.md) and [security boundaries](docs/security.md).

## Check a change

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src/llm_test_lab
.venv/bin/pytest
.venv/bin/python -m llm_test_lab.contracts.schemas --check
.venv/bin/python scripts/check_docs.py
./lab run --source fixture --suite core --variants all --gate fixture-contract
```

These checks need no model or API key. Process tests use real Node/Python workers and local HTTP stubs. Deliberately vulnerable cases should still produce their expected violations.

Keep a few rules in mind:

- Test stored state and events, not just the assistant's reply. A refusal does not prove that a tool blocked the action
- Add passing and failing controls for a new check. Missing evidence stays unknown, not safe
- Keep saved example bundles unchanged; record new results separately
- After changing contracts, regenerate schemas with `.venv/bin/python -m llm_test_lab.contracts.schemas` and review the diff. Bump the oracle version when its logic changes
- Keep dependency updates explicit: runtime version files, `uv.lock` and the Promptfoo lockfile belong in the diff. Test a fresh setup; avoid global installs and `npx latest`

Live experiments are separate: use synthetic data, your own credentials and an explicit request budget. Never add a paid call to the default test suite.
