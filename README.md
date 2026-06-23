# audit

An 8-stage vulnerability-discovery agent driven by local **OpenAI Codex**
through your ChatGPT subscription login. It uses many narrow agents,
deliberate disagreement, schema-validated outputs, and an explicit
reachability gate.

MIT-licensed. No OpenAI API key is required when the Codex CLI is signed in
with ChatGPT.

Fork of [evilsocket/audit](https://github.com/evilsocket/audit), modified to
use Codex. Keep the original repository as `upstream` when working from this
fork.

## Short local run

Full audits can take a long time. Start with a capped recon run so the harness
proves the target path, auth, and state handling before it fans out.

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -U pip
./.venv/bin/python -m pip install -e .

codex login
./.venv/bin/audit auth-check

./.venv/bin/audit run \
  --repo /Users/alex/Documents/src/coworking_front_next \
  --run-id run-1 \
  --max-recon-tasks 12
```

## Origin

This project is a from-scratch implementation of the pipeline described in
Cloudflare's [Project Glasswing](https://blog.cloudflare.com/cyber-frontier-models/)
post. The post argues that real-world vulnerability discovery does not come
from asking one big model "find bugs here". It comes from:

1. Many narrow agents working in parallel on tightly scoped questions.
2. Deliberate disagreement: a second agent tries to disprove the first agent's
   finding.
3. A reachability trace: most "is this code buggy?" findings are noise unless
   attacker-controlled input can reach the sink from outside the system.
4. A feedback loop so reachable bugs seed hunts for the same pattern elsewhere.

This repo packages that pipeline into a runnable local harness with prompts,
schemas, a state store, and an orchestrator.

## The 8 stages

![Vulnerability discovery harness - 8 stages](https://raw.githubusercontent.com/evilsocket/audit/main/docs/pipeline.png)

| # | Stage | Default model | Purpose |
|---|-------|---------------|---------|
| 1 | Recon | gpt-5.5 | Map the repo and emit narrowly scoped Hunt tasks |
| 2 | Hunt | gpt-5.4 | One attack class per agent; compile or reason through PoCs |
| 3 | Validate | gpt-5.5 | Adversarial re-read that tries to disprove Hunt findings |
| 4 | Gapfill | gpt-5.4 | Re-queue under-covered areas |
| 5 | Dedupe | gpt-5.4 | Cluster findings by root cause |
| 6 | Trace | gpt-5.5 | Prove attacker-controlled input reaches the sink |
| 7 | Feedback | gpt-5.4 | Turn reachable traces into new Hunt tasks |
| 8 | Report | gpt-5.4 | Schema-validated structured report |

Each stage is one markdown prompt in `prompts/` plus one JSON Schema in
`schemas/`. The runner passes the schema to Codex and validates every final
response locally before a stage persists it.

## Quickstart

```bash
# 1. Install
python -m venv .venv
source .venv/bin/activate
pip install -e .

# 2. Sign in to Codex with ChatGPT subscription auth
codex login
codex login status

# 3. Verify the harness sees the same local Codex login
python -m audit auth-check

# 4. Run
python -m audit run --repo /path/to/target --run-id my-run
python -m audit status --run-id my-run
python -m audit report --run-id my-run --format md > report.md
```

On macOS, `/usr/sbin/audit` can shadow this package's console script. When in
doubt, use `python -m audit ...` or `./.venv/bin/audit ...`.

## Codex auth model

This harness uses the local Codex CLI/SDK credential cache. The intended path is
ChatGPT subscription auth:

```bash
codex login
codex login status
```

`audit auth-check` rejects API-key Codex logins because this project is
configured for subscription-based local Codex use, not Platform API billing.

For headless machines, use Codex's device-code login:

```bash
codex login --device-auth
```

The harness never reads or prints credential contents from `~/.codex/auth.json`
or the OS credential store.

## Progress output

Long runs emit progress at the stage, task, retry, repair, heartbeat, and
completion boundaries. Typical output includes:

- run id and stage;
- task or finding id;
- model;
- elapsed seconds;
- counters for completed, failed, skipped, reachable, or confirmed work.

If the terminal has not printed anything for a while, the runner heartbeat
prints a status line every 30 seconds during active Codex turns.

## Containment knobs

Subscription usage is governed by Codex plan limits, so the harness does not
expose a project budget flag. Keep runs small with concurrency and task caps:

```bash
python -m audit run --repo /path/to/target \
  --max-concurrency 1 \
  --max-recon-tasks 15
```

The default config is intentionally conservative for local subscription use.
Increase concurrency only after a small run has completed cleanly.

## Live-target reproduction (optional)

If the target has a running deployment, point the agents at it. Hunt can use the
live service for reproduction, Validate rejects findings that do not reproduce,
and Trace confirms reachability with real HTTP round-trips. The static path
remains available; these flags are opt-in.

```bash
python -m audit run --repo /path/to/target --run-id live \
  --max-concurrency 1 \
  --target-url http://server.local:8888 \
  --target-creds email=admin@system.com \
  --target-creds password=changechangeme
```

Rules the agents follow when `--target-url` is set:

- Network egress is restricted in prompts to that host plus `127.0.0.1`.
- A finding that does not reproduce against the live target is dropped or
  rejected depending on stage.
- Credentials flow into every relevant stage's `user_input` as a dict.

## Scope notes (optional)

Targets often have intentionally loose surfaces that are not bugs. Put those
rules in a text file and pass it in:

```bash
python -m audit run --repo /path/to/target --scope-notes target_scope.md
```

Example `target_scope.md`:

```markdown
- Mailpit (port 1025) is test-only; ignore.
- Plaintext API keys in the database are a required feature.
- Do not flag rate-limit absence on anonymous /ping endpoints.
- Only consider critical/high severity.
```

## Recon mines git history

Recon greps git history for past security patches (`CVE`, `sec:`,
`fix.*auth`, `sanitize`, and similar). Patched files are often hardened, but
sibling files with the same idiom may not be. Findings get seeded against the
unpatched copies.

## Logic chains

The pipeline's default is one attack class per task. Recon can also emit
`logic_chain` tasks for high-impact multi-component paths such as auth bypass
plus IDOR plus path traversal. This is the one allowed exception to
single-attack-class scoping.

## Layout

```text
prompts/        8 stage prompts, loaded as Codex developer instructions
schemas/        JSON schemas; every agent output is validated
config/         stages.yaml; model, concurrency, and sandbox policy hints
audit/          Python package
  auth.py       Codex ChatGPT login preflight
  state.py      SQLite DAO for runs, tasks, findings, traces, usage, artifacts
  runner.py     openai-codex SDK wrapper with schema validation and repair turns
  orchestrator.py pipeline driver
  stages/       one module per stage
work/           per-task scratch dirs
results/        JSONL artifacts per stage plus final report.json
state.db        SQLite runtime state
```

## Safety

Hunt and Trace stages may run with Codex `workspace-write` sandbox because they
can need command execution in scratch directories. Run audits in a disposable
VM or container when the target source is untrusted. A target with malicious
build scripts can otherwise execute during PoC compilation.

Codex can read the directories you add to the run. Do not include secrets in
target paths unless they are in scope for the audit. Outputs land in
`results/<run-id>/`; treat those artifacts as sensitive.

## License

[MIT](LICENSE). Reuse freely. No warranty.

## Acknowledgements

- The pipeline design is from Cloudflare's Project Glasswing post.
- Built on the official local Codex CLI and `openai-codex` SDK.
