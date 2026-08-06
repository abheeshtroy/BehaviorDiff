"""Build the git repository the demo scenario manifests compare against.

BehaviorDiff compares two git refs, so the demo needs a real repository with a
real history. This generates one: ``main`` is demo/shop-api as it stands, and
each scenario branch applies one overlay from demo/variants/ on top of it —
so ``git diff main..fix/checkout-validation`` is exactly the change a reviewer
would see on that pull request.

The repository is generated rather than committed because it is derived.
demo/shop-api plus demo/variants/ is the source of truth; this script is how
those become refs. It is safe to re-run — the output directory is rebuilt from
scratch every time.

    python demo/build_demo_repo.py

Writes demo/.demo-repo (cloned by the engine, one image built per ref) and
demo/.demo-build/<branch> (plain trees, for docker-compose's build contexts).
Both are gitignored.
"""

from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Paths are resolved from this file, never the cwd, so the script behaves the
# same however it is invoked.
DEMO_DIR = Path(__file__).resolve().parent
APP_DIR = DEMO_DIR / "shop-api"
VARIANTS_DIR = DEMO_DIR / "variants"
DEFAULT_REPO_DIR = DEMO_DIR / ".demo-repo"
DEFAULT_BUILD_DIR = DEMO_DIR / ".demo-build"

BASE_BRANCH = "main"

# Files that exist only to make the working copy work, and have no business in
# a repository that stands in for someone's application.
COPY_EXCLUDES = shutil.ignore_patterns("__pycache__", "*.pyc", ".git")


@dataclass(frozen=True)
class Scenario:
    """One scenario branch: an overlay directory and the commit that applies it."""

    branch: str
    overlay: str
    subject: str
    body: str

    @property
    def slug(self) -> str:
        """Filesystem-safe form of the branch name."""
        return self.branch.replace("/", "-")


SCENARIOS = (
    Scenario(
        branch="fix/checkout-validation",
        overlay="fix-checkout-validation",
        subject="checkout: return 400 on an incomplete address",
        body=(
            "A missing street or zip answered 500, which told the caller nothing and "
            "paged us for a bad request. Answer 400 and name the missing fields.\n\n"
            "Also authorize up front so the provider's latency overlaps validation, "
            "and release the cart on rejection so the customer can edit and retry it."
        ),
    ),
    Scenario(
        branch="fix/retry-logic",
        overlay="fix-retry-logic",
        subject="fulfillment: retry queueing the fulfill job",
        body=(
            "A transient error on the insert dropped the job on the floor and the "
            "order never shipped. Retry the insert so queueing survives one failure."
        ),
    ),
    Scenario(
        branch="fix/response-cleanup",
        overlay="fix-response-cleanup",
        subject="orders: consistent field names and a numeric amount",
        body=(
            "GET /api/orders returned `order_id` where the rest of the API returns "
            "`id`, and a stringly-typed `total`. Rename to `id`/`amount` and return "
            "the amount as a number."
        ),
    ),
    # The bug/ branches are the benchmark set: each seeds exactly one regression
    # on the HTTP surface, and the matching demo/manifests/benchNN-*.yaml is the
    # run that should find it. Their commit messages read like the change was
    # meant, which is the point — a message that announced the regression would
    # tell the classifier the answer.
    Scenario(
        branch="bug/wrong-error-code",
        overlay="bug-wrong-error-code",
        subject="checkout: answer a missing cart with an error document",
        body=(
            "Callers had to handle a 404 body and a success body differently. "
            "Return the error in the response document instead so there is one "
            "shape to parse."
        ),
    ),
    Scenario(
        branch="bug/drop-total-from-response",
        overlay="bug-drop-total-from-response",
        subject="checkout: return the order's identity, not its amount",
        body=(
            "The amount is already on GET /api/orders/{id}, which is the authority "
            "on it. Drop it from the checkout response so the two cannot disagree."
        ),
    ),
    Scenario(
        branch="bug/wrong-content-type",
        overlay="bug-wrong-content-type",
        subject="orders: render an order as key=value text",
        body=(
            "The order-label printer reads GET /api/orders/{id} and has no JSON "
            "parser. Render the order as a flat line of key=value pairs."
        ),
    ),
    Scenario(
        branch="bug/swallow-not-found",
        overlay="bug-swallow-not-found",
        subject="fulfillment: report zero jobs for an order that is gone",
        body=(
            "A batch of fulfill calls died on the first order that had been "
            "cancelled. Report that nothing was scheduled instead of failing."
        ),
    ),
    Scenario(
        branch="bug/hardcode-cart-total",
        overlay="bug-hardcode-cart-total",
        subject="carts: price the cart on discount and checkout, not on create",
        body=(
            "Creating a cart priced the items, then discount and checkout priced "
            "them again. Start a new cart at zero and price it once, later."
        ),
    ),
    Scenario(
        branch="bug/break-discount-math",
        overlay="bug-break-discount-math",
        subject="carts: retune the discount rate",
        body="Move DISCOUNT_MULTIPLIER to the rate the current promotion runs at.",
    ),
    Scenario(
        branch="bug/skip-payment-record",
        overlay="bug-skip-payment-record",
        subject="payment: stop copying authorizations into payment_calls",
        body=(
            "The provider is the ledger of record for an authorization, and the "
            "local copy is a second place for the same fact to live. Drop the "
            "insert and read authorizations from the provider."
        ),
    ),
    Scenario(
        branch="bug/wrong-order-status",
        overlay="bug-wrong-order-status",
        subject="checkout: write a new order as pending",
        body=(
            "An order was confirmed before anything had picked it up. Write it "
            "as pending; fulfillment moves it to confirmed when it takes it."
        ),
    ),
    Scenario(
        branch="bug/double-fulfill-job",
        overlay="bug-double-fulfill-job",
        subject="fulfillment: queue the pick and the ship legs together",
        body=(
            "Shipping waited on picking to be enqueued before it could start. "
            "Queue both legs up front so a free worker can take either."
        ),
    ),
    Scenario(
        branch="bug/orphan-order",
        overlay="bug-orphan-order",
        subject="checkout: let the order stand on its own",
        body=(
            "An order carries the amount it was placed for, so holding the cart "
            "behind it only keeps a finished cart from being swept up. Write the "
            "order without the back-reference."
        ),
    ),
    Scenario(
        branch="bug/corrupt-discount-total",
        overlay="bug-corrupt-discount-total",
        subject="carts: record the discount code, price it at checkout",
        body=(
            "Applying a code rewrote the cart's total, so a second code compounded "
            "on the first. Record the code and let checkout price it once."
        ),
    ),
)


class DemoRepoError(Exception):
    """Raised when the demo repository cannot be built."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo-dir", type=Path, default=DEFAULT_REPO_DIR, help="where to write the git repository")
    parser.add_argument("--build-dir", type=Path, default=DEFAULT_BUILD_DIR, help="where to write the plain per-branch trees")
    args = parser.parse_args(argv)

    try:
        build(args.repo_dir, args.build_dir)
    except DemoRepoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"\nBuilt {_display(args.repo_dir)} with {len(SCENARIOS) + 1} refs:")
    print(f"  {BASE_BRANCH}")
    for scenario in SCENARIOS:
        print(f"  {scenario.branch}")
    print("\nThe scenario manifests in demo/manifests/ point at it. Run one with:")
    print("  python cli.py demo/manifests/scenario1-checkout-validation.yaml")
    print("or trigger it from the dashboard at /runs/new.")
    return 0


def build(repo_dir: Path, build_dir: Path) -> None:
    """Create the demo repository and the per-branch trees, replacing any existing ones."""
    _check_sources()

    shutil.rmtree(repo_dir, ignore_errors=True)
    shutil.rmtree(build_dir, ignore_errors=True)

    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(APP_DIR, repo_dir, ignore=COPY_EXCLUDES)

    _git(repo_dir, "init", "--quiet", f"--initial-branch={BASE_BRANCH}")
    # Committer identity is set on the repo so the script works on a machine
    # with no global git config.
    _git(repo_dir, "config", "user.name", "BehaviorDiff Demo")
    _git(repo_dir, "config", "user.email", "demo@behaviordiff.local")
    _commit_all(repo_dir, "shop-api: initial import", "The application as it stands on main.")
    print(f"{BASE_BRANCH}: {_describe(repo_dir)}")

    for scenario in SCENARIOS:
        _build_branch(repo_dir, scenario)

    _git(repo_dir, "checkout", "--quiet", BASE_BRANCH)

    # Plain trees for docker-compose, which builds images from directories
    # rather than refs.
    build_dir.mkdir(parents=True)
    for scenario in SCENARIOS:
        _git(repo_dir, "checkout", "--quiet", scenario.branch)
        shutil.copytree(repo_dir, build_dir / scenario.slug, ignore=COPY_EXCLUDES)
    _git(repo_dir, "checkout", "--quiet", BASE_BRANCH)


def _build_branch(repo_dir: Path, scenario: Scenario) -> None:
    overlay = VARIANTS_DIR / scenario.overlay
    _git(repo_dir, "checkout", "--quiet", "-b", scenario.branch, BASE_BRANCH)

    changed = _apply_overlay(overlay, repo_dir)
    _commit_all(repo_dir, scenario.subject, scenario.body)
    print(f"{scenario.branch}: {_describe(repo_dir)} ({', '.join(changed)})")


def _apply_overlay(overlay: Path, repo_dir: Path) -> list[str]:
    """Copy an overlay tree over the checkout. Returns the paths it replaced.

    An overlay may only replace files that already exist: it stands for an edit
    to the application, and a stray path silently adding a new module is much
    more likely a typo than an intention.
    """
    replaced: list[str] = []
    for source in sorted(p for p in overlay.rglob("*") if p.is_file()):
        relative = source.relative_to(overlay)
        destination = repo_dir / relative
        if not destination.is_file():
            raise DemoRepoError(
                f"overlay {overlay.name} has {relative}, which does not exist in {APP_DIR.name}"
            )
        shutil.copy2(source, destination)
        replaced.append(str(relative))
    if not replaced:
        raise DemoRepoError(f"overlay {overlay.name} is empty — nothing to change")
    return replaced


def _check_sources() -> None:
    if not (APP_DIR / "Dockerfile").is_file():
        raise DemoRepoError(f"no Dockerfile in {APP_DIR}")
    for scenario in SCENARIOS:
        overlay = VARIANTS_DIR / scenario.overlay
        if not overlay.is_dir():
            raise DemoRepoError(f"missing overlay directory for {scenario.branch}: {overlay}")


def _commit_all(repo_dir: Path, subject: str, body: str) -> None:
    _git(repo_dir, "add", "--all")
    _git(repo_dir, "commit", "--quiet", "-m", subject, "-m", body)


def _describe(repo_dir: Path) -> str:
    return _git(repo_dir, "rev-parse", "--short", "HEAD").strip()


def _git(repo_dir: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise DemoRepoError("git executable not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise DemoRepoError(f"git {shlex.join(args)} failed in {repo_dir}:\n{exc.stderr.strip()}") from exc
    return result.stdout


def _display(path: Path) -> str:
    """Path relative to the project root when it is under it, for readable output."""
    try:
        return str(path.resolve().relative_to(DEMO_DIR.parent))
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main())
