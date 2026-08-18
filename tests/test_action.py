"""Regression coverage for the GitHub Action's artifact contract."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = "behaviordiff-results"


def test_action_uses_a_non_hidden_results_directory() -> None:
    action = (ROOT / "action.yml").read_text()

    assert ".behaviordiff-results" not in action
    assert f'$GITHUB_WORKSPACE/{RESULTS_DIR}/result.json' in action
    assert '--json > "$raw_result"' in action
    assert "mktemp \"${RUNNER_TEMP:-/tmp}/behaviordiff-result.XXXXXX\"" in action
    assert "from engine.report import write_sanitized_json" in action
    assert "write_sanitized_json(artifact_path, json.loads(raw_path.read_text(encoding=\"utf-8\")))" in action
    assert f'--json > "$GITHUB_WORKSPACE/{RESULTS_DIR}/result.json"' not in action
    assert "trap 'rm -f \"$raw_result\"' EXIT" in action
    assert action.count("${{ github.workspace }}/" + RESULTS_DIR) == 2
    assert "results-dir={directory}" in action
    assert f'$GITHUB_WORKSPACE/{RESULTS_DIR}/report.html' in action
    assert "Interactive review" in action


def test_artifact_upload_examples_match_action_results_directory() -> None:
    for path in (ROOT / "README.md", ROOT / "examples/github-actions/behaviordiff.yml"):
        text = path.read_text()
        assert ".behaviordiff-results" not in text
        assert f"path: {RESULTS_DIR}/" in text


def test_action_examples_use_the_stable_release_tag() -> None:
    for path in (ROOT / "README.md", ROOT / "examples/github-actions/behaviordiff.yml"):
        assert "abheeshtroy/BehaviorDiff@v0.1.0" in path.read_text()
