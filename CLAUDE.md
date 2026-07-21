# BehaviorDiff

A change-aware differential testing engine. Runs two versions of an application under identical conditions, executes behavioral workflows against both, and compares a configured observation surface — HTTP responses, PostgreSQL state, outbound HTTP calls, logs, and latency.

## Architecture

The engine is framework-agnostic. Any app that runs in Docker, speaks HTTP, and uses Postgres works. The manifest defines everything the engine needs to know about the app.

```
CLI / Web API
    ↓
Manifest parser (manifest.py)
    ↓
Orchestrator (orchestrator.py)
  - Builds Docker images for both versions
  - Starts containers with identical Postgres seeds
  - Waits for health checks
  - Tears down after comparison
    ↓
Runner (runner.py)
  - Executes workflow steps against both versions in lockstep
  - Each step is an HTTP request with optional assertions
    ↓
Observers (observers/)
  - http.py: captures request/response pairs
  - postgres.py: snapshots selected tables before/after each workflow
  - proxy.py: intercepts and records outbound HTTP calls
    ↓
Normalizer (normalizer.py)
  - Suppresses nondeterministic noise (UUIDs, timestamps, row order)
  - Uses repeated runs against the same version to detect instability
  - Applies field-level ignore rules from the manifest
    ↓
Comparator (comparator.py)
  - Diffs normalized observations between base and target
  - Produces structured findings with raw evidence
```

## AI layer (engine/ai/ — added in phase 2)

AI proposes and explains. Deterministic code verifies and decides. The engine must produce correct findings without AI. AI improves coverage and classification.

- intent.py: reads git diff + PR description → structured intent
- classifier.py: takes findings + intent → intended/suspicious/noise labels
- generator.py: reads change + routes + schema → suggested workflows

## Key design decisions

- **Manifest-driven**: the engine knows nothing about any specific app. Everything comes from the manifest — start command, health check, database seed, tables to observe, outbound services, workflows.
- **Noise suppression by measurement**: run the workflow 3x against the same version. Anything that varies is nondeterministic. Suppress it. Don't rely on rules alone.
- **Evidence over assertion**: every finding includes the raw data (HTTP response, DB rows, outbound call). Never report a difference without showing the proof.
- **Observations are a configured surface**: we observe HTTP responses, selected Postgres tables, outbound HTTP calls, logs, and latency. We do NOT claim to observe "everything."

## Code rules

- Python 3.12+, type hints on all function signatures
- Use pydantic for data models (manifest, findings, observations)
- Use docker SDK for Python (not subprocess calls to docker CLI)
- Use httpx for HTTP requests (async where appropriate)
- Use psycopg for Postgres (not SQLAlchemy — we want raw queries for snapshot diffing)
- Tests go in tests/ mirroring the source structure
- No print() for output — use structlog
- Error handling: fail loudly with clear messages, don't silently continue
- Every observer returns a typed dataclass/pydantic model, not raw dicts

## Manifest format

```yaml
app:
  name: my-app
  start: uvicorn app.main:app --host 0.0.0.0 --port 8000
  port: 8000
  healthcheck: /health
  dockerfile: Dockerfile  # optional, defaults to Dockerfile

database:
  type: postgres
  seed: seed.sql
  observe_tables:
    - orders
    - payments
    - carts

outbound:
  services:
    - name: payment-provider
      base_url: https://api.payments.example.com
      mock_responses:
        POST /v1/authorize:
          status: 200
          body: { "auth_id": "mock_001", "status": "approved" }

compare:
  base_ref: main
  target_ref: fix/checkout-validation
  repo: .  # local repo path or git URL

workflows:
  - name: checkout-with-invalid-address
    steps:
      - method: POST
        path: /api/carts
        body: { "items": [{ "sku": "SHOE-42", "qty": 1 }] }
        capture: { "cart_id": "$.cart_id" }
      - method: POST
        path: /api/carts/{cart_id}/discount
        body: { "code": "SAVE10" }
      - method: POST
        path: /api/checkout
        body: { "cart_id": "{cart_id}", "address": { "city": "SF" } }

normalize:
  ignore_fields:
    - "*.created_at"
    - "*.updated_at"
    - "*.id"
  uuid_fields:
    - "*.cart_id"
    - "*.order_id"
  numeric_tolerance: 0.001
  ignore_row_order:
    - carts
    - orders
```

## What NOT to do

- Don't hardcode anything specific to the demo app in the engine
- Don't use LLM calls in the comparison/observation path — AI is classification only
- Don't build a general-purpose test framework — this compares behavior between two versions, period
- Don't add features not in the current phase without discussing first
- Don't use subprocess for Docker operations — use the Docker SDK
- Don't swallow exceptions — surface them with context
