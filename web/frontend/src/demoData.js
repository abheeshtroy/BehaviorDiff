// `manifest` ties a scripted scenario to the real manifest that reproduces it,
// so the picker can show one story per comparison and the bridge CTA can
// preselect the right file. It is an explicit field, not derived: the
// api-cleanup scenario lives in scenario3-response-cleanup.yaml.
const SCRIPTS = {
  "checkout-validation": {
    id: "checkout-validation",
    manifest: "scenario1-checkout-validation.yaml",
    pr: "#482",
    title: "Fix checkout validation for empty carts",
    subtitle: "Rejects empty carts with a 400 instead of failing downstream with a 500.",
    file: "demo/shop-api/app/main.py",
    diff: [
      { n: 41, type: "ctx", text: "def checkout(cart):" },
      { n: 42, type: "add", text: "    if not cart.items:" },
      { n: 43, type: "add", text: "        raise ValidationError(\"Cart is empty\")" },
      { n: 44, type: "rem", text: "    charge(cart.total)" },
      { n: 45, type: "add", text: "    charge(cart.total)" },
    ],
    checks: ["47 tests passing", "94% coverage", "lint clean"],
    steps: ["cart", "discount", "submit", "charge", "order"],
    divergeAt: 2,
    workflows: 6,
    duration: "4.2s",
    intent: {
      summary: "Reject empty carts at checkout, return 400 instead of 500",
      expected: ["POST /checkout returns 400 for empty cart"],
    },
    assessment: "Status change is intended. Discount clearing and the premature payment call are not explained by the diff.",
    suppressed: 12,
    // Readable headlines for the findings a *real* run of this manifest
    // produces, keyed by "category:severity:subject" (see findingKey in
    // lib/findings.js). The comparator names the row it changed; these name
    // what that meant. Written against observed output of the run — the raw
    // summary and evidence stay on the card underneath.
    //
    // Only this scenario has them: it is the one wired to run end to end, so
    // it is the only one whose real keys have been seen rather than guessed.
    readable: {
      "http:changed:POST /api/checkout": "Checkout rejects the bad address with a 400 instead of failing with a 500",
      "postgres:added:carts": "Cart stored with its discount cleared",
      "postgres:removed:carts": "The discounted cart no longer exists",
      "postgres:added:payment_calls": "Card charged on a rejected order",
    },
    findings: [
      {
        category: "http", classification: "intended", severity: "changed",
        summary: "POST /checkout: status 200 → 400",
        workflow: "checkout-flow", step: 2,
        reasoning: "Matches the stated intent", confidence: 0.95,
        base: { status_code: 200, body: { order_id: "ord_8f2a", total: 29.99 } },
        target: { status_code: 400, body: { detail: "Cart is empty" } },
      },
      {
        category: "postgres", classification: "suspicious", severity: "changed",
        summary: "Discount silently cleared",
        workflow: "checkout-flow", step: null,
        reasoning: "carts.discount zeroed on the failure path, not mentioned in the PR", confidence: 0.82,
        base: { id: 1, discount_code: "SAVE10", discount: 10.0, status: "active" },
        target: { id: 1, discount_code: null, discount: 0.0, status: "active" },
      },
      {
        category: "outbound", classification: "suspicious", severity: "added",
        summary: "Card charged on a rejected order",
        workflow: "checkout-flow", step: 1,
        reasoning: "POST /charge fired before validation completed", confidence: 0.88,
        base: null,
        target: { method: "POST", path: "/charge", body: { amount: 29.99, currency: "usd" } },
      },
    ],
  },

  "retry-logic": {
    id: "retry-logic",
    manifest: "scenario2-retry-logic.yaml",
    pr: "#495",
    title: "Refactor background job retry logic",
    subtitle: "Moves retry scheduling into a decorator so it can be reused across jobs.",
    file: "demo/shop-api/app/jobs.py",
    diff: [
      { n: 88, type: "ctx", text: "def fulfill_order(order_id):" },
      { n: 89, type: "rem", text: "    schedule_retry(fulfill_order, order_id)" },
      { n: 90, type: "add", text: "    @with_retry(max_attempts=3)" },
      { n: 91, type: "add", text: "    def _run():" },
      { n: 92, type: "add", text: "        return _fulfill(order_id)" },
    ],
    checks: ["31 tests passing", "89% coverage", "lint clean"],
    steps: ["order", "fulfill", "retry", "notify", "close"],
    divergeAt: 2,
    workflows: 4,
    duration: "3.8s",
    intent: {
      summary: "Consolidate retry scheduling behind a reusable decorator",
      expected: ["Retry behaviour is unchanged, only the call site moves"],
    },
    assessment: "The decorator schedules a retry that the original call site also schedules, so every job is now enqueued twice.",
    suppressed: 7,
    findings: [
      {
        category: "postgres", classification: "suspicious", severity: "added",
        summary: "Duplicate job rows inserted into job_queue",
        workflow: "fulfillment", step: null,
        reasoning: "Two rows per order instead of one — decorator and call site both schedule", confidence: 0.91,
        base: [{ id: 41, job: "fulfill_order", order_id: 7 }],
        target: [{ id: 41, job: "fulfill_order", order_id: 7 }, { id: 42, job: "fulfill_order", order_id: 7 }],
      },
      {
        category: "outbound", classification: "suspicious", severity: "added",
        summary: "Customer notified twice for one order",
        workflow: "fulfillment", step: 3,
        reasoning: "Second POST /notify follows the duplicate job", confidence: 0.86,
        base: null,
        target: { method: "POST", path: "/notify", body: { order_id: 7, template: "shipped" } },
      },
    ],
  },

  "api-cleanup": {
    id: "api-cleanup",
    manifest: "scenario3-response-cleanup.yaml",
    pr: "#503",
    title: "Clean up order response payload",
    subtitle: "Renames a field for consistency with the rest of the API.",
    file: "demo/shop-api/app/schemas.py",
    diff: [
      { n: 22, type: "ctx", text: "class OrderResponse(BaseModel):" },
      { n: 23, type: "ctx", text: "    id: str" },
      { n: 24, type: "rem", text: "    total_cents: int" },
      { n: 25, type: "add", text: "    total: float" },
    ],
    checks: ["52 tests passing", "96% coverage", "lint clean"],
    steps: ["cart", "submit", "order", "fetch", "render"],
    divergeAt: 3,
    workflows: 5,
    duration: "3.1s",
    intent: {
      summary: "Rename total_cents to total for naming consistency",
      expected: ["Order responses expose total instead of total_cents"],
    },
    assessment: "The rename also changed the unit and the type. Any consumer reading total_cents now reads a different number in a different scale.",
    suppressed: 4,
    findings: [
      {
        category: "http", classification: "suspicious", severity: "changed",
        summary: "GET /orders/{id}: total_cents removed, type changed",
        workflow: "order-fetch", step: 3,
        reasoning: "Field renamed and unit changed from cents to dollars — breaking for existing consumers", confidence: 0.93,
        base: { status_code: 200, body: { id: "ord_1c4d", total_cents: 2999 } },
        target: { status_code: 200, body: { id: "ord_1c4d", total: 29.99 } },
      },
    ],
  },
};

// ---- Synthetic event streams ------------------------------------------------
//
// A stored run carries the progress stream the pipeline emitted (see
// run_pipeline in engine/pipeline.py), and the views built on top of it read
// stages and timestamps. The scripted scenarios have no pipeline behind them,
// so they script the stream too — same stages, same order, same data keys as a
// real run, so a view never has to know which kind of run it is looking at.
//
// Timestamps are seconds since the epoch like the real ones. The absolute
// values are arbitrary; the gaps are what carries meaning.

const EPOCH = 1_753_000_000;

// Gaps in milliseconds, roughly what each stage costs in a real run: standing
// the two versions up dominates, everything after it is fast.
const GAP = {
  ready: 1800,
  snapshot: 220,
  workflow: 380,
  observe: 90,
  compare: 140,
  classify: 640,
  persist: 40,
  done: 30,
};

/** Every workflow the scenario's findings mention, in the order they appear. */
function workflowNames(scenario) {
  const names = scenario.findings.map((f) => f.workflow).filter(Boolean);
  return [...new Set(names)];
}

function syntheticEvents(scenario) {
  const events = [];
  let elapsedMs = 0;

  const push = (gapMs, stage, message, data = null) => {
    elapsedMs += gapMs;
    events.push({ stage, message, timestamp: EPOCH + elapsedMs / 1000, data });
  };

  push(0, "environments_starting", "Building and starting both versions", { direct: false });
  push(GAP.ready, "environments_ready", "Environments ready: base http://localhost:8001, target http://localhost:8002", {
    base_url: "http://localhost:8001",
    target_url: "http://localhost:8002",
  });
  push(GAP.snapshot, "observing_postgres", "Snapshotting tables before the workflows run", { phase: "before" });

  const names = workflowNames(scenario);
  names.forEach((name, index) => {
    const total = names.length;
    push(GAP.observe, "workflow_started", `Running workflow: ${name}`, { name, index, total });
    push(GAP.workflow, "workflow_completed", `Finished workflow: ${name}`, { name, index, total });
  });

  push(GAP.observe, "workflows_complete", `Ran ${names.length} workflow(s)`, { count: names.length });
  push(GAP.observe, "observing_http", "Comparing HTTP responses");
  push(GAP.snapshot, "observing_postgres", "Snapshotting tables after the workflows ran", { phase: "after" });
  push(GAP.compare, "comparing", "Comparing observations");
  push(GAP.classify, "classifying", `Classifying ${scenario.findings.length} finding(s) against the change's intent`, {
    findings: scenario.findings.length,
  });
  push(GAP.persist, "persisting", "Saving the run");
  push(GAP.done, "done", `Run complete: ${scenario.findings.length} finding(s)`);

  return events;
}

export const SCENARIOS = Object.fromEntries(
  Object.entries(SCRIPTS).map(([id, scenario]) => [id, { ...scenario, events: syntheticEvents(scenario) }]),
);

export const SCENARIO_LIST = Object.values(SCENARIOS);
