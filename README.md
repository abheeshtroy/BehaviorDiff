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

It runs from the CLI or from a web dashboard, which triggers runs, streams their progress live, and keeps every run around to look at afterwards.

## Architecture

```
CLI (cli.py)                      Web API (web/api.py)
    │                                  │
    ├── --init: scan repo →            ├── POST /api/runs/trigger
    │   AI generates starter manifest  └── WS /api/runs/{id}/stream
    │                                  │
    └──────────────────┬───────────────┘
                       │  Both drive the same pipeline (engine/pipeline.py),
                       │  a generator that yields progress events as it goes
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
    │
    ▼
Store (web/store.py)
    │  Every run — CLI or web-triggered — persisted to SQLite,
    │  result and event stream together
    ▼
Dashboard (web/frontend/)
       React SPA: findings, sequence diagram, blast radius, timeline
```

## Quick start

### 1. Generate a manifest

Point BehaviorDiff at any repo with a Dockerfile and HTTP routes:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
python cli.py --init ./your-app --base-ref main --target-ref your-branch
```

It scans the repo, reads the route handlers, and writes a `behaviordiff.yaml` with workflows that match the actual request schemas.

### 2. Build the demo repository

```bash
python demo/build_demo_repo.py
```

The engine compares two git refs, so the demo needs a repository with real
history. This generates one in `demo/.demo-repo` (gitignored): `main` is
`demo/shop-api`, and each scenario branch applies one overlay from
`demo/variants/` on top of it, so `git diff main..fix/checkout-validation` is
exactly the change a reviewer would see. Re-run it after editing either.

That's all the setup a full run needs — the engine builds an image per ref,
starts a Postgres per version, and tears it all down afterwards:

```bash
python cli.py demo/manifests/scenario1-checkout-validation.yaml
```

Or trigger it from the dashboard at `/runs/new` and watch the events stream in.

Either way the run is saved, so anything you run from the CLI also shows up in
the dashboard — open `/runs` and click it to get all four views of the run. See
[Web dashboard](#web-dashboard) for how to start it.

### 3. Or run against already-started versions

To skip orchestration and compare two containers you started yourself:

```bash
cd demo && docker compose up -d
```

`docker-compose.yaml` runs base (port 8001), target (port 8002), separate
Postgres instances (ports 55432, 55433), and a payment mock. `base` builds from
`demo/shop-api`; `target` builds from `demo/.demo-build/fix-checkout-validation`,
which step 2 also materializes — point it at another `.demo-build/fix-*`
directory to compare a different scenario.

After editing the demo app source or `demo/shop-api/seed.sql`, restart with:

```bash
cd demo && docker compose down -v && docker compose up --build
```

`down -v` drops the Postgres volumes — the seed only runs on an empty data
directory, so without it the old database survives and your `seed.sql` edits are
ignored. `--build` rebuilds the images so source changes are picked up. Skip
either one and the containers come back up with the previous behavior, silently.

Then:

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

The repo ships with three seeded scenarios against a FastAPI shop API, each with a planted bug. Every scenario is one branch of the generated demo repository, changing exactly one module:

| Scenario | Branch changes | Intended | Seeded bugs |
|---|---|---|---|
| checkout-validation | `app/checkout.py` | 400 instead of 500 on a bad address | Payment authorized before validation; discount cleared on rejection |
| retry-logic | `app/fulfillment.py` | Retry queueing the fulfill job | Duplicate background jobs created |
| response-cleanup | `app/orders.py` | Rename `total` → `amount`, change type | `GET /api/orders/{id}/receipt`, an untouched consumer, 500s on the renamed fields |

The app records every payment authorization in `payment_calls`, which
scenario 1 observes — that's how "it charged the customer before rejecting the
address" becomes visible to a database observer. Outbound HTTP interception is
not wired into the run path yet (see Roadmap), so the payment provider is
stubbed in-process when `PAYMENT_URL` is unset, which is how the engine runs it.

## Web dashboard

Build the frontend once, then serve it from the API:

```bash
cd web/frontend && npm install && npm run build
python -m uvicorn web.api:app --port 8100
```

Open http://localhost:8100. FastAPI serves the built SPA and the JSON API from
the same port, so there is nothing else to run.

For frontend work, `cd web/frontend && npm run dev` puts Vite on 5173 with
`/api` proxied to 8100 — including the WebSocket upgrade, so the live run
stream works in dev too. Keep uvicorn running alongside it.

### What's in it

**Landing page with interactive demo scenarios.** Scripted walkthroughs of the
three seeded scenarios that replay a recorded run — no Docker, no database, no
API key. They're there so the tool can be understood in thirty seconds without
anyone building an image first.

**Run triggering** (`/runs/new`). Pick one of the manifests the server
discovered, start it, and watch the pipeline report itself over a WebSocket:
building images, starting containers, each workflow step, normalization,
comparison. When it finishes you land on the result. Only manifests found in
the manifest directory can be started — the requested path is matched by
resolved path against that set, so a traversal or a symlink doesn't get through.
Point it elsewhere with `BEHAVIORDIFF_MANIFEST_DIR` (default `demo/manifests`).

**Run detail** (`/runs/{id}`), four views of the same run:

| View | What it shows |
|---|---|
| Findings | Classified differences, click one to expand base/target evidence side by side |
| Sequence diagram | Vertical timeline of the run's events, with findings anchored to the step that produced them |
| Blast radius | Which workflows were affected, across which observation surfaces |
| Timeline scrubber | Horizontal DAW-style bar with a draggable cursor for scrubbing through the run |

**Run history** (`/runs`). Every persisted run with its stats — workflows,
steps, findings, suppressed differences, duration.

### How runs are persisted

Every run, whether it came from the CLI or from the dashboard, is written to
`~/.behaviordiff/runs.db` (SQLite). The result JSON, the AI intent and
classification if they were produced, and the event stream all go in together.

Persisting the events is what makes the sequence diagram and timeline work after
the fact: the live WebSocket is gone once the run ends, but the run's shape in
time was already recorded, so the visualizations replay from storage instead of
needing the socket. Runs stored before that column existed read back with no
events; the detail view falls back to the scripted scenario's stream when the
manifest is one of the demo ones, and otherwise just doesn't draw those views.

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

web/
  api.py               # FastAPI: manifest discovery, run trigger, WS stream, run reads
  run_registry.py      # In-flight runs: pipeline on a thread, events over a queue
  store.py             # SQLite persistence for results and event streams
  frontend/
    src/pages/         # Landing, DemoRun, RunNew, RunList, RunDetail
    src/components/    # SequenceDiagram, BlastRadiusGrid, TimelineScrubber, ...
    src/lib/           # Event → view-model logic, unit-tested apart from React

demo/
  shop-api/            # FastAPI demo app with 3 seeded bug scenarios
  manifests/           # Hand-written manifests for each scenario
  docker-compose.yaml  # Two app versions + two Postgres + payment mock

tests/                 # 340+ Python tests, all passing
```

## Tech stack

- Python 3.12+, type hints everywhere
- Pydantic for all data models (`extra="forbid"` to catch typos)
- Docker SDK for Python (not subprocess)
- httpx for HTTP, psycopg for Postgres
- structlog for structured logging
- Anthropic API (Claude Sonnet 4.6) for AI features
- FastAPI + SQLite for the web API and run storage
- React + Vite for the dashboard
- pytest for the engine (340+ tests), vitest for the frontend (130+ tests)

## Roadmap

Phase 3 is done — the web dashboard, the live run stream, run persistence, and
the four run views are all in. Next:

- [ ] Benchmark / evaluation harness with seeded regressions — measure detection rate, not just "it found something"
- [ ] Public deployment so the demo is reachable without cloning
- [ ] Demo video
- [ ] CLI observer — compare stdout/stderr/exit codes for non-HTTP apps
- [ ] File system observer — diff output directories for data pipelines
- [ ] MySQL / MongoDB observers
- [ ] GitHub Action for automated PR comparison
- [ ] Positional row matching for UUID-keyed inserts

## License

MIT
