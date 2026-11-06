# LLM Test Lab

**Break an AI agent. Fix the failure. Prove it still works.**

A local lab for learning and testing AI-agent security, quality and reliability. Close your own synthetic ticket, try to access somebody else's, then see **what actually happened**—not just what the assistant claimed.

**Start without an AI model, account or API key.** A local browser mission asks you to predict an outcome, find evidence of unauthorized access, build an access rule, and test whether normal work still succeeds. The tools and isolated SQLite database are real; the assistant's actions are scripted.

> **Status: `v0.1.0-alpha.1` technical alpha (Python package `0.1.0a1`), not a stable release or a validated live-model benchmark.** See [release readiness](docs/release-readiness.md) for dated evidence and gaps. No model downloads or paid requests happen during setup or the browser mission.

## Two parts, different readiness

| Part | What you can do now | Current boundary |
|---|---|---|
| **Learn in the browser** | Predict, investigate an access violation, build a rule, test it and export your experiment | Verified scripted trials; no model or API key needed |
| **Run experiments from the CLI** | Execute scenario matrices, compare protection variants, inspect evidence, and configure a live-model profile | Fixture runs are verified; live-model transports are implemented but remain experimental and unverified with a supported real model |

The browser is currently the **learning mission**, not a full web control panel for experiments. You do not need to finish the lesson before using the CLI. A full browser experiment workspace is future work, not a feature of this alpha.

Live experiments place your selected model inside **this lab's ticket agent**, with its tools, synthetic database and scenarios. This is not a scanner for arbitrary external AI applications. The distinction matters: a scripted trial validates the lab and its controls; a live trial would evaluate the selected model in this specific environment.

## Try it

**Just looking?** Read the [saved experiment](docs/demos/outcome.md). Nothing to install.

**Start the mission:** download this repository as a ZIP and extract it, or use Git:

```sh
git clone https://github.com/c33leron/llm-test-lab.git
cd llm-test-lab
```

Open a terminal in the project folder and run the two commands below. Bootstrap requires Python **3.10+**; macOS and Linux only, no native Windows claim.

```sh
./lab setup
./lab start
```

`setup` downloads pinned tools and dependencies **inside this project**, not into your system. It requires internet access. In an interactive terminal, `start` opens the Russian-language mission in your browser and keeps its server on **127.0.0.1 only**. Keep the terminal running. Opening or reloading the screen does not execute an experiment; the mission asks for an explicit action first.

If Python is missing, install it from [python.org](https://www.python.org/downloads/) and repeat setup. If your ZIP tool removed the executable bit, run `chmod +x lab` once. The [Russian step-by-step guide](docs/start-ru.md) explains each screen.

For a non-interactive terminal, explicitly start the screen without opening a browser, then use the full local address printed by the command:

```sh
./lab start --web --no-open
```

## Your first investigation

Your own ticket is closed—but did the application also disclose somebody else's data?

1. **Predict** whether the authorized task and the forbidden read will happen.
2. **Run the baseline** and select the event that proves what actually happened.
3. **Build a rule** using four choices: identity source, access condition, covered operations, and when the check runs.
4. **Test the rule** against unauthorized reading, a normal task, a harmless quotation, and unauthorized writing. Denying everything is not a successful fix.
5. **Change the user**, keeping the same rule. A rule that only blocks one remembered ticket ID is not enough.
6. **Explain and export** your experiment card. The explanation is saved for self-review, not scored by an AI judge.

The mission uses five dedicated learning cases, L01–L05. It checks real application effects, not whether your choices match a hidden answer string. [Mission guide](docs/LEARNING_MISSION.md).

## Beyond the first mission

| Experiment | What to check |
|---|---|
| Close your ticket | Did the stored status change, while other records stayed intact? |
| Lose the confirmation after a write | Can the assistant recover without a duplicate effect? |
| Put a malicious instruction inside ticket data | Is it treated as data rather than permission? |
| Force a forbidden tool call | Does the application actually block it? |
| Save a harmless attack-like quotation | Does the defense preserve legitimate functionality? |
| Correct your request or clarify ambiguity | Does the assistant follow the current intent without early writes? |

The original twelve core cases and two explicit controls remain available. They compare three variants: no ownership check, a prompt-only rule, and a server-enforced ownership check. A deliberately vulnerable result should be red; the **lab check** can still pass because it correctly detected that failure. The report separates these facts.

```sh
./lab start --terminal
./lab start --case C01 --open
./lab start --case F01 --variants all --open
```

`--terminal` retains the earlier terminal menu. `--case` runs a named fixture directly; `--open` opens its saved HTML report. Advanced `run`, `plan`, `compare`, `new-case`, and `--json` workflows remain available.

## Your key is never part of the project

The default lessons need no key. For experimental live runs through the CLI, each person selects their own model and uses their own local credentials. Only environment-variable **names**, never secret values, belong in profiles or GitHub. A local Ollama model needs no cloud API key, but must be installed separately and checked for local-only operation. No supported live-model reference run is claimed in this alpha. [Live setup and its limits](docs/live.md).

## Learn, extend, develop

- [Three lessons](docs/lessons/README.md) · [What the result means](docs/results-reference.md).
- [Add your own case](docs/extending.md) · [How the pieces fit](docs/architecture.md).
- [Troubleshooting](docs/troubleshooting.md) · [Security boundaries](docs/security.md).
- [Contributing and running tests](CONTRIBUTING.md) · [Release readiness](docs/release-readiness.md) · [Alpha release notes](docs/releases/v0.1.0-alpha.1.md).

Python owns the agent, tools, evidence and checks. Pinned Promptfoo still executes the matrix. The local lesson screen is a small guided interface over that same execution path—not a second eval runner or a general chatbot. Progress stays in the project; full run evidence and static HTML reports remain available separately.

The separate **Promptfoo web viewer is still disabled**: its pinned server cannot enforce the required loopback binding. The new lesson screen does not silently enable that viewer or close its original acceptance gap.

[MIT license](LICENSE) · [Attribution](NOTICE)
