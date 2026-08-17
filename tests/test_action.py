"""Regression coverage for the GitHub Action's artifact contract."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = "behaviordiff-results"


def test_action_uses_a_non_hidden_results_directory() -> None:
    action = (ROOT / "action.yml").read_text()

    assert ".behaviordiff-results" not in action
    assert f'$GITHUB_WORKSPACE/{RESULTS_DIR}/result.json' in action
    assert action.count("${{ github.workspace }}/" + RESULTS_DIR) == 2
    assert "results-dir=${directory}" in action


def test_artifact_upload_examples_match_action_results_directory() -> None:
    for path in (ROOT / "README.md", ROOT / "examples/github-actions/behaviordiff.yml"):
        text = path.read_text()
        assert ".behaviordiff-results" not in text
        assert f"path: {RESULTS_DIR}/" in text
