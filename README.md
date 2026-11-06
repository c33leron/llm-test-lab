# LLM Test Lab

A local security and quality lab for AI agents. Check what an agent actually did — not just what it said it did.

- **Learn in the browser** — investigate a data leak, build an access rule, then see whether it protects data without breaking normal work. No model, account or API key needed
- **Run experiments from the terminal** — test scenarios, compare defenses and inspect the evidence. Live-model connections are experimental and haven't been validated with a real reference model yet

This is an **alpha**. The browser is currently a learning mission, not the full experiment dashboard. Experiments use the lab's ticket agent and synthetic data, not arbitrary external AI apps.

## Try it

You'll need macOS or Linux, Python 3.10+ and internet access for setup.

```sh
git clone https://github.com/c33leron/llm-test-lab.git
cd llm-test-lab
./lab setup
./lab start
```

Setup installs tools inside the project. It doesn't download models or make paid model requests. `start` opens the Russian-language lesson in your browser. Keep the terminal open; `Ctrl+C` stops the server.

The assistant's actions in the lesson are scripted. The tools, access checks and SQLite database run for real. You make a prediction, find the evidence, change a rule and test it with another user.

[Пошаговый запуск на русском](docs/start-ru.md) · [Read a saved experiment — no install](docs/demos/outcome.md)

## What you can check

- Access to another user's data, including instructions hidden in ticket text
- Whether a task really finished, even after a timeout or lost confirmation
- Whether a defense blocks the attack **and** preserves legitimate work

The CLI includes 12 core scenarios, two explicit controls and three protection variants. The browser lesson has five separate cases. A deliberately vulnerable case should show a violation — that's useful evidence, not a safe result.

```sh
./lab smoke                    # check the lab without a model
./lab start --terminal         # open the CLI menu
```

## Go further

[Connect a model](docs/live.md) · [Read results](docs/results-reference.md) · [Add a scenario](docs/extending.md) · [Troubleshooting](docs/troubleshooting.md)

[What we've tested](docs/release-readiness.md) · [Contribute](CONTRIBUTING.md) · [Security](SECURITY.md) · [Release notes](https://github.com/c33leron/llm-test-lab/releases)

By [c33leron](https://github.com/c33leron). [MIT license](LICENSE) · [Third-party notices](NOTICE)
