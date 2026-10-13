# ActionWeaver

**Meeting notes → evidence-linked plan → review → reliable task board.**

ActionWeaver is a local AI-automation workbench that separates **what a model proposes** from **what software is allowed to do**. It extracts action items through Ollama, validates exact source quotes and dependency graphs, requires approval of the precise proposal, and creates local tasks in one SQLite transaction. Repeated or concurrent apply attempts do not create duplicate tasks for that proposal.

**Status:** the automation engine, offline demo, and mocked provider contract are tested. **Live Ollama extraction has not been tested in the reference environment.** The bundled plan is hand-authored, not presented as model output.

## Try the no-download demo

Python 3.12+; no pip dependencies or API keys for the demo:

```sh
python -m unittest -v
python demo.py
```

The demo uses a temporary database, an explicitly labeled fixture, and a simulated reviewer. It creates three tasks, retries without duplicates, and completes the first prerequisite:

| Task | Owner | Result |
|---|---|---|
| Document worker retries | Ava | Done |
| Test restart recovery | Noah | Ready |
| Prepare release notes | Mina | Blocked by restart testing |

The dashboard discussed without a commitment is intentionally absent from the **fixture**. This is an example expectation, not a measured model-extraction result.

## Run the actual review workflow

```sh
python actionweaver.py propose --notes example-notes.txt --plan example-plan.json
```

Copy the returned full `digest`, then replace `DIGEST` below. The same `--db` must be used throughout; the default is `actionweaver.sqlite`.

```sh
python actionweaver.py review DIGEST
python actionweaver.py approve DIGEST --reviewer your-name
python actionweaver.py apply DIGEST
python actionweaver.py board DIGEST
python actionweaver.py complete DIGEST t1 --reviewer your-name
python actionweaver.py history DIGEST
```

Review the **source, titles, owners, quotes, and dependencies** before approving. To decline a pending proposal, use `reject DIGEST --reviewer your-name`. To revise it, edit an exported/imported plan and propose again; changes to the notes or task plan produce a different digest and require a new approval. `apply` means **create local task cards**, not perform the work described by their titles.

## Connect a real local LLM

Install [Ollama](https://ollama.com/download) separately and run a downloaded local model that supports structured outputs. Model downloads require storage and may have hardware/license requirements; this repository does not install or download models for you.

```sh
python actionweaver.py propose --notes example-notes.txt --model YOUR_INSTALLED_MODEL
```

The adapter calls `http://127.0.0.1:11434/api/chat` using Ollama's [JSON-schema structured output interface](https://docs.ollama.com/capabilities/structured-outputs), then independently validates the response. It uses a 120-second network timeout, limits responses to 1 MB, requests bounded output, disables proxy inheritance and rejects redirects. No automatic provider retries or fallback fake answers.

The local server receives the notes. Use a **downloaded local model** and check the server's configuration: a local URL alone cannot guarantee that a separately configured server will never forward data elsewhere. There is no paid hosted-provider integration in this project.

## Architecture

```text
Untrusted notes → Ollama JSON proposal (no tools)
                         ↓
          Strict schema + exact quote validation
          Owner evidence + dependency DAG validation
                         ↓
          Immutable source/plan digest: PENDING
                         ↓ explicit reviewer decision
                  APPROVED or REJECTED
                         ↓ transactional apply
          SQLite tasks + APPLIED state + audit event
                         ↓
          Ready/blocked board → prerequisite-aware completion
```

- **Evidence:** each task cites 1–5 exact source-line quotes. An owner must appear in a cited quote or be null. Unknown action fields, duplicate JSON keys and cyclic/unknown dependencies fail closed.
- **DSA:** deterministic topological sorting validates up to 25 proposed tasks; a dependency graph determines ready versus blocked work.
- **Approval binding:** SHA-256 covers the schema version, source hash and canonical plan. Saved content is revalidated before review/apply. Approval is scoped to that digest, not whatever a model generates later.
- **Atomic effects:** an immediate SQLite transaction inserts all tasks, marks the proposal applied and records its audit entry. A mid-transaction failure rolls back the whole set. Retrying after commit creates zero additional tasks.
- **Concurrent workers:** SQLite serializes competing apply operations; the proposal/task primary key also enforces uniqueness. Board reads use one snapshot.
- **Least authority:** the model never receives shell, email, browser, or task-system tools. The only apply effect is insertion into the local board. No task title is executed as code.

## Verification and measured results

**32 tests** cover citation forgery, invalid owners, extra action fields, schema bounds, malformed provider replies, cycles, 50 randomized DAGs, changed-plan approvals, conflicting workflow states, rollback, concurrent apply, reopen/retry and board snapshot consistency. GitHub Actions runs on Python 3.12 and 3.14.

The concurrency regression sends **24 attempts through 8 workers** and checks that only one three-task set exists. This is a correctness test, not a distributed-throughput benchmark.

```sh
python benchmark.py --output local-benchmark.json
```

Reference local run, three repetitions on Apple Silicon / Python 3.14.2 / SQLite 3.50.4:

| Synthetic workload | Measured result |
|---|---:|
| Pre-approved proposals per run | 500 |
| Created task cards per run | 1,500 |
| Median apply phase | 77.17 ms |
| Median retry phase for the same 500 proposals | 33.01 ms |
| Additional task cards after retries | 0 |

[Raw timings and environment](benchmark-results.json) are included. File-backed temporary SQLite WAL, default synchronization; inference, staging, review and export are excluded. Approvals are simulated. **These are not LLM accuracy, human time-saved, production traffic, or power-loss durability results.**

## Boundaries worth understanding

1. Exact quotes prove text exists, **not that a generated task is entailed by it**. A model can quote a negated or irrelevant sentence. Human semantic review remains essential. No prompt-injection immunity claim is made.
2. Reviewer names are audit labels, **not authentication**. Anyone with CLI/database access can approve. This is a single-user local workbench, not a multi-tenant approval service.
3. The audit log and hashes detect some content changes but are **not tamper-proof against a database administrator**. Source notes and task text are stored unencrypted locally.
4. Retry safety is per proposal digest and local database. It does not deduplicate semantically equivalent meetings or guarantee exactly-once delivery to external APIs.
5. The board is JSON/CLI, not a hosted UI. It creates task records; it does not send emails, change Jira tickets, execute code or perform the tasks themselves.
6. A 120-second socket timeout is not a total wall-clock deadline against a slow-drip server. This is a trusted-local-provider client, not a hardened public gateway.

## What would make this production-ready?

A real meeting-note evaluation set with labeled expected actions and unsupported-action checks; live-model latency/quality measurements; authenticated reviewers; encryption/retention policies; and independently idempotent adapters for a real task tracker. Those are future work, not hidden claims of current functionality.

Developed with AI assistance. All sample meetings and benchmark inputs are synthetic. MIT licensed.
