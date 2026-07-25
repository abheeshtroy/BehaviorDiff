# BehaviorDiff

A change-aware differential testing engine. Run two versions of an application under identical conditions, execute behavioral workflows against both, and compare what actually changed — HTTP responses, database state, and outbound calls.

**AI proposes, deterministic code verifies.** The engine produces correct findings without AI. AI improves coverage and classification.

```
$ behaviordiff manifest.yaml \
    --base-url http://localhost:8001 \
    --target-url http://localhost:8002 \
    --base-pg-dsn "postgresql://...@localhost:55432/shop" \
    --target-pg-dsn "postgresql://...@localhost:55433/shop"

  4 finding(s):

  [~] http | POST /api/carts: headers changed: date
      workflow: checkout-with-discount-and-invalid-address, step 0

  [~] http | POST /api/checkout: status 500 -> 400; body changed
      workflow: checkout-with-discount-and-invalid-address, step 2

  [+] postgres | row inserted into carts
  [-] postgres | row deleted from carts

  Ran 1 workflow(s), 3 step(s) in 0.18s
```

## What it does

You point BehaviorDiff at two running versions of the same app (base and target), give it a manifest describing what to test and what to observe, and it tells you exactly what's different — with raw evidence, not assertions.

The observation surface is configured, not assumed. You choose which HTTP routes to exercise, which database tables to snapshot, and which outbound services to mock. Noise (timestamps, UUIDs, row ordering) is suppressed by measurement, not by rules alone.

## Architecture

```
CLI (cli.py)
    │
    ├── --init: scan repo → AI generates starter manifest
    │
    ▼
Manifest parser (engine/manifest.py)
    │  Pydantic validation, extra="forbid" to catch typos
    ▼
Orchestrator (engine/orchestrator.py)
    │  Docker lifecycle: build images, start containers,
    │  separate Postgres per version, wait for healthchecks
    ▼
Runner (engine/runner.py)
    │  Execute workflows against both versions in lockstep
    │  Dual-track variable capture (each version gets its own)
    ▼
Observers (engine/observers/)
    │  http.py:     request/response comparison
    │  postgres.py: before/after snapshots, delta-of-deltas diffing
    │  proxy.py:    outbound call recording and mock responses
    ▼
Normalizer (engine/normalizer.py)
    │  UUID remapping, field ignoring, numeric tolerance,
    │  instability detection via repeated runs
    ▼
Comparator (engine/comparator.py)
    │  Unifies all observer diffs into structured findings
    ▼
AI Layer (ai/)
    ├── intent.py:       git diff → structured intent (what the change claims to do)
    ├── classifier.py:   findings + intent → intended / suspicious / noise labels
    ├── workflow_gen.py:  diff + routes → proposed test workflows
    ├── manifest_gen.py: repo scan → starter manifest with accurate request bodies
    └── scaffold.py:     extract routes and tables from code (heuristic, not a parser)
```

## Quick start

### 1. Generate a manifest

Point BehaviorDiff at any repo with a Dockerfile and HTTP routes:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
python cli.py --init ./your-app --base-ref main --target-ref your-branch
```

It scans the repo, reads the route handlers, and writes a `behaviordiff.yaml` with workflows that match the actual request schemas.

### 2. Start both versions

```bash
cd demo && docker compose up -d
```

The demo ships with a `docker-compose.yaml` that runs base (port 8001), target (port 8002), separate Postgres instances (ports 55432, 55433), and a payment mock.

### 3. Run the comparison

```bash
python cli.py demo/manifests/scenario1-checkout-validation.yaml \
  --base-url http://localhost:8001 \
  --target-url http://localhost:8002 \
  --base-pg-dsn "postgresql://postgres:postgres@localhost:55432/shop" \
  --target-pg-dsn "postgresql://postgres:postgres@localhost:55433/shop" \
  --verbose
```

### 4. Add AI classification (optional)

```bash
git diff main..fix/checkout-validation > /tmp/change.diff

python cli.py manifest.yaml \
  --base-url http://localhost:8001 \
  --target-url http://localhost:8002 \
  --diff /tmp/change.diff \
  --pr-description "Return 400 instead of 500 on incomplete address"
```

Each finding gets labeled `[intended]`, `[suspicious]`, or `[noise]` with reasoning.

## Demo scenarios

The repo ships with three seeded scenarios against a FastAPI shop API, each with a planted bug:

| Scenario | Change | Intended | Seeded bugs |
|---|---|---|---|
| checkout-validation | Return 400 instead of 500 on bad address | Status code change | Payment called before validation; discount cleared on rollback |
| retry-logic | Refactor fulfillment retry | Job scheduling | Duplicate background jobs created |
| response-cleanup | Rename `total` → `amount`, change type | Field rename | Breaks downstream consumers expecting `total` as string |

## Key design decisions

**Manifest-driven.** The engine knows nothing about any specific app. Everything comes from the manifest — start command, healthcheck, database seed, tables to observe, outbound services, workflows. Anything in Docker that speaks HTTP and uses Postgres works.

**Separate Postgres per version.** Each version gets its own database instance, seeded identically. The engine compares mutation deltas (what each version changed) rather than raw state, so writes from one version never pollute the other's observations.

**Dual-track variable capture.** When a workflow step captures a value (like `cart_id`), each version captures its own. The base version's cart ID is used for base's subsequent requests, the target's for target's. The normalizer's UUID remapping handles comparison.

**Noise suppression by measurement.** Run the same workflow 3x against one version. Anything that varies between runs is nondeterministic — suppress it. Don't rely on rules alone.

**Evidence over assertion.** Every finding includes the raw data. Never report a difference without proof.

**AI proposes, deterministic code verifies.** The engine produces correct findings without AI. AI reads the git diff to extract intent, classifies findings against that intent, proposes test workflows, and generates starter manifests. But it never makes the comparison decision.

## CLI reference

```
behaviordiff manifest.yaml           # Run comparison
behaviordiff --init ./repo           # Generate manifest from repo
behaviordiff manifest.yaml --json    # JSON output
behaviordiff manifest.yaml --verbose # Show raw evidence
behaviordiff manifest.yaml --diff change.diff  # AI classification
behaviordiff manifest.yaml --diff change.diff --generate-workflows  # AI workflow proposals
```

| Flag | Purpose |
|---|---|
| `--init [path]` | Scan a repo and generate `behaviordiff.yaml` |
| `--base-url` / `--target-url` | URLs of already-running versions (skips orchestration) |
| `--base-pg-dsn` / `--target-pg-dsn` | Postgres connection strings for each version |
| `--diff` | Path to git diff file for AI intent extraction |
| `--pr-description` | PR description text for AI context |
| `--generate-workflows` | Propose test workflows from the diff |
| `--json` | Machine-readable JSON output |
| `--verbose` / `-v` | Show raw evidence for each finding |

## Project structure

```
engine/
  manifest.py          # YAML manifest parser with Pydantic validation
  orchestrator.py      # Docker lifecycle management
  runner.py            # Workflow execution with dual-track capture
  normalizer.py        # Noise suppression
  comparator.py        # Unified diff → structured findings
  observers/
    http.py            # HTTP response observation and diffing
    postgres.py        # DB snapshot, row-level diff, delta-of-deltas
    proxy.py           # Outbound call recording

ai/
  intent.py            # Git diff → structured change intent
  classifier.py        # Findings + intent → classification labels
  workflow_gen.py      # Diff + routes → proposed workflows
  manifest_gen.py      # Repo scan → starter manifest
  scaffold.py          # Route/table extraction helpers

demo/
  shop-api/            # FastAPI demo app with 3 seeded bug scenarios
  manifests/           # Hand-written manifests for each scenario
  docker-compose.yaml  # Two app versions + two Postgres + payment mock

tests/                 # 241 tests, all passing
```

## Tech stack

- Python 3.12+, type hints everywhere
- Pydantic for all data models (`extra="forbid"` to catch typos)
- Docker SDK for Python (not subprocess)
- httpx for HTTP, psycopg for Postgres
- structlog for structured logging
- Anthropic API (Claude Sonnet 4.6) for AI features
- pytest for testing (241 tests, 0.5s)

## Roadmap

- [ ] Web UI — React dashboard for findings with classification labels
- [ ] CLI observer — compare stdout/stderr/exit codes for non-HTTP apps
- [ ] File system observer — diff output directories for data pipelines
- [ ] MySQL / MongoDB observers
- [ ] GitHub Action for automated PR comparison
- [ ] Positional row matching for UUID-keyed inserts

## License

MIT
