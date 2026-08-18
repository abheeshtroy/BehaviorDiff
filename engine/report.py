"""Offline, self-contained review documents for BehaviorDiff results."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any


_SENSITIVE_KEY = re.compile(
    r"(?:authorization|cookie|password|passwd|secret|token|api[_-]?key|"
    r"session(?:[_-]?id)?|client[_-]?secret|access[_-]?key|private[_-]?key)",
    re.IGNORECASE,
)
_SECRET_QUERY = re.compile(
    r"([?&](?:access[_-]?token|api[_-]?key|auth(?:orization)?|password|"
    r"secret|token|session(?:[_-]?id)?)=)[^&#\s]*",
    re.IGNORECASE,
)
_BEARER = re.compile(r"\bbearer\s+[^\s,;]+", re.IGNORECASE)
_REDACTED = "[REDACTED]"
_GROUPS = (
    ("http", "HTTP", "http"),
    ("database", "Database", "postgres"),
    ("outbound", "Outbound", "outbound"),
    ("timing", "Timing", "latency"),
    ("other", "Other / uncategorized", None),
)


def sanitize_evidence(value: Any, *, key: str | None = None) -> Any:
    """Return a serialization-safe copy with common secrets redacted."""
    if key is not None and _SENSITIVE_KEY.search(str(key)):
        return _REDACTED
    if isinstance(value, dict):
        return {str(name): sanitize_evidence(item, key=str(name)) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_evidence(item) for item in value]
    if isinstance(value, str):
        return _BEARER.sub("Bearer " + _REDACTED, _SECRET_QUERY.sub(r"\1" + _REDACTED, value))
    return value


def write_sanitized_json(path: str | Path, payload: Any) -> Path:
    """Write JSON for a review artifact using the report's redaction policy."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(sanitize_evidence(payload), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return destination


def _payload_parts(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None, str | None]:
    """Accept CLI JSON and, for local use, the saved-run API envelope."""
    if isinstance(payload.get("result"), dict):
        return payload["result"], payload.get("intent"), payload.get("classification"), payload.get("app_name")
    return payload, payload.get("intent"), payload.get("classification"), payload.get("app_name")


def _text(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _json(value: Any) -> str:
    return html.escape(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True), quote=False)


def _path(path: tuple[str, ...]) -> str:
    return "evidence" if not path else ".".join(path).replace(".[", "[")


def _diff_rows(base: Any, target: Any, path: tuple[str, ...] = ()) -> list[tuple[str, str, Any, Any]]:
    """Return leaf-level evidence changes without assuming an evidence schema."""
    if isinstance(base, dict) and isinstance(target, dict):
        rows = []
        for key in sorted(set(base) | set(target), key=str):
            name = str(key)
            if key not in base:
                rows.append((_path(path + (name,)), "added", None, target[key]))
            elif key not in target:
                rows.append((_path(path + (name,)), "removed", base[key], None))
            else:
                rows.extend(_diff_rows(base[key], target[key], path + (name,)))
        return rows
    if isinstance(base, list) and isinstance(target, list):
        rows = []
        for index in range(max(len(base), len(target))):
            item_path = path + (f"[{index}]",)
            if index >= len(base):
                rows.append((_path(item_path), "added", None, target[index]))
            elif index >= len(target):
                rows.append((_path(item_path), "removed", base[index], None))
            else:
                rows.extend(_diff_rows(base[index], target[index], item_path))
        return rows
    return [(_path(path), "changed", base, target)] if base != target else []


def _value(value: Any) -> str:
    return _json(value) if isinstance(value, (dict, list)) else _text(value)


def _field_diff(base: Any, target: Any) -> str:
    """Render changed evidence in fixed Base/Target columns before raw JSON."""
    if base is None and target is None:
        return ""
    if base is None:
        rows = [("recorded evidence", "added", None, target)]
    elif target is None:
        rows = [("recorded evidence", "removed", base, None)]
    else:
        rows = _diff_rows(base, target)
    if not rows:
        rows = [("recorded evidence", "unchanged", base, target)]
    rendered = []
    for path, change, before, after in rows:
        base_value = "<em>only on target</em>" if change == "added" else _value(before)
        target_value = "<em>only on base</em>" if change == "removed" else _value(after)
        rendered.append(
            f'<div class="field-row {change}"><code>{_text(path)}</code>'
            f'<div class="base-value">{base_value}</div><div class="target-value">{target_value}</div></div>'
        )
    return (
        '<section class="field-diff" aria-label="Structured evidence difference">'
        '<div class="field-head"><span>Field</span><span>Base</span><span>Target</span></div>'
        + "".join(rendered) + "</section>"
    )


def _classification_for(index: int, classification: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(classification, dict):
        return None
    for item in classification.get("classifications", []):
        if isinstance(item, dict) and item.get("finding_index") == index:
            return item
    return None


def _group_key(finding: dict[str, Any]) -> str:
    category = str(finding.get("category", "")).lower()
    for key, _, source in _GROUPS[:-1]:
        if category == source:
            return key
    return "other"


def _context_bar(result: dict[str, Any], app_name: str | None) -> str:
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    items = ['<strong>BehaviorDiff Review</strong>']
    real_app = app_name or metadata.get("app_name") or result.get("app_name")
    if real_app is not None:
        items.append(f'<span>{_text(real_app)}</span>')
    repository = metadata.get("repository", metadata.get("repo", result.get("repository", result.get("repo"))))
    if repository is not None:
        items.append(f'<span>{_text(repository)}</span>')
    base = metadata.get("base_ref", result.get("base_ref"))
    target = metadata.get("target_ref", result.get("target_ref"))
    if base is not None or target is not None:
        refs = " → ".join(_text(value) for value in (base, target) if value is not None)
        items.append(f'<span><small>Compare</small>{refs}</span>')
    if "total_workflows" in metadata:
        items.append(f'<span><small>Workflows</small>{_text(metadata["total_workflows"])}</span>')
    if "duration_seconds" in metadata:
        items.append(f'<span><small>Duration</small>{_text(metadata["duration_seconds"])}s</span>')
    generated = metadata.get("generated_at", metadata.get("generated_time", result.get("generated_at", result.get("generated_time"))))
    if generated is not None:
        items.append(f'<span><small>Generated</small>{_text(generated)}</span>')
    return '<div class="context-line">' + "".join(items) + "</div>"


def _verdict(findings: list[dict[str, Any]], intent: dict[str, Any] | None) -> str:
    headline = "No behavioral changes detected" if not findings else f'{len(findings)} behavioral change{"s" if len(findings) != 1 else ""} detected'
    subtitle = ""
    if isinstance(intent, dict) and intent.get("summary"):
        subtitle = f'<p class="verdict-subtitle">{_text(intent["summary"])}</p>'
    return f'<section class="verdict"><p class="verdict-eyebrow">Review result</p><h1>{headline}</h1>{subtitle}</section>'


def _failure_review(payload: dict[str, Any], message: Any = None) -> str:
    """Render a failed review without inventing a failure reason."""
    supplied = message
    if supplied is None:
        supplied = payload.get("message")
    if supplied is None or supplied == "":
        supplied = "The comparison did not produce a valid result."
    body = (
        '<main class="review-document failure"><div class="context-line">'
        '<strong>BehaviorDiff Review</strong></div><section class="verdict">'
        f'<h1>Run could not complete</h1><p class="verdict-subtitle">{_text(supplied)}</p>'
        "</section></main>"
    )
    return _document(body, payload)


def _surface_counts(findings: list[dict[str, Any]]) -> str:
    counts = {key: 0 for key, _, _ in _GROUPS}
    for finding in findings:
        counts[_group_key(finding)] += 1
    links = []
    for key, label, _ in _GROUPS:
        if counts[key]:
            links.append(f'<a href="#{key}">{counts[key]} {label}</a>')
    return '<nav class="surface-counts" aria-label="Observed surfaces">' + "<span> · </span>".join(links) + "</nav>" if links else ""


def _finding_card(index: int, finding: dict[str, Any], classification: dict[str, Any] | None) -> str:
    category = _group_key(finding)
    badges = [f'<span class="badge category">{_text(category)}</span>']
    if finding.get("severity") is not None:
        badges.append(f'<span class="badge severity { _text(finding["severity"]) }">{_text(finding["severity"])}</span>')
    reasoning = ""
    if classification:
        label = classification.get("classification")
        if label in {"intended", "suspicious", "unknown"}:
            badges.append(f'<span class="badge classification { _text(label) }">{_text(label)}</span>')
        if classification.get("reasoning"):
            reasoning = f'<p class="finding-explanation">{_text(classification["reasoning"])}</p>'
        confidence = classification.get("confidence")
        if isinstance(confidence, (int, float)):
            reasoning += f'<p class="confidence">Confidence: {_text(f"{confidence * 100:.0f}%")}</p>'
    context = []
    if finding.get("workflow_name") is not None:
        context.append(_text(finding["workflow_name"]))
    if finding.get("step_index") is not None:
        context.append(f'step {_text(finding["step_index"])}')
    context_html = f'<p class="finding-context">{" · ".join(context)}</p>' if context else ""
    base, target = finding.get("evidence_base"), finding.get("evidence_target")
    raw = ""
    if base is not None or target is not None:
        raw = (
            '<details class="raw-evidence"><summary>Full captured evidence</summary><div class="evidence-columns">'
            f'<section><h3>Base</h3><pre>{_json(base)}</pre></section><section><h3>Target</h3><pre>{_json(target)}</pre></section>'
            "</div></details>"
        )
    return (
        f'<article class="finding-card" id="finding-{index}"><header><div class="badges">{"".join(badges)}</div>'
        f'<h3>{_text(finding.get("summary", "Observed difference"))}</h3>{context_html}{reasoning}</header>'
        f'<div class="evidence-columns structured"><section class="base-column"><h4>Base</h4></section><section class="target-column"><h4>Target</h4></section></div>'
        f'{_field_diff(base, target)}{raw}</article>'
    )


def _finding_groups(findings: list[dict[str, Any]], classification: dict[str, Any] | None) -> str:
    grouped = {key: [] for key, _, _ in _GROUPS}
    for index, finding in enumerate(findings):
        grouped[_group_key(finding)].append((index, finding))
    sections = []
    for key, label, _ in _GROUPS:
        items = grouped[key]
        if items:
            cards = "".join(_finding_card(index, finding, _classification_for(index, classification)) for index, finding in items)
            sections.append(f'<section class="finding-group" id="{key}"><header class="group-heading"><h2>{label}</h2><span>{len(items)}</span></header>{cards}</section>')
    return "".join(sections)


def render_report(payload: dict[str, Any]) -> str:
    """Render one sanitized, fully standalone BehaviorDiff Review document."""
    safe_payload = sanitize_evidence(payload)
    if not isinstance(safe_payload, dict):
        safe_payload = {"error": "The report input was not a JSON object."}
    result, intent, classification, app_name = _payload_parts(safe_payload)
    if "error" in safe_payload:
        return _failure_review(safe_payload, safe_payload.get("error"))
    if not isinstance(result, dict):
        return _failure_review(safe_payload)
    if "error" in result:
        return _failure_review(safe_payload, result.get("error"))
    if not isinstance(result.get("findings"), list):
        return _failure_review(safe_payload)
    findings = [item for item in result["findings"] if isinstance(item, dict)]
    clean = '<section class="clean"><p>Both versions matched across the observed surfaces.</p></section>' if not findings else ""
    body = (
        f'<main class="review-document">{_context_bar(result, app_name)}{_verdict(findings, intent)}{_surface_counts(findings)}'
        f'<div class="findings-stack">{_finding_groups(findings, classification)}{clean}</div>'
        '<footer>Evidence values are redacted in this review when common secret-like keys, headers, bearer credentials, or URL query parameters are detected. Normalization is not a privacy boundary.</footer></main>'
    )
    return _document(body, safe_payload)


def _document(body: str, payload: dict[str, Any]) -> str:
    """Wrap the rendered body; the data blob is inert and safe for offline inspection."""
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>BehaviorDiff Review</title><style>
:root{{color-scheme:dark;--bg-0:#040410;--bg-1:#0f0f13;--bg-2:#16161b;--text-0:#f4f4f6;--text-1:#c9c9d6;--text-2:#8888a0;--text-3:#55556a;--border:rgba(255,255,255,.06);--border-strong:rgba(255,255,255,.10);--teal:#76d4c6;--coral:#ef9a91;--teal-fill:rgba(52,211,190,.095);--coral-fill:rgba(248,113,113,.095);--glass-fill:linear-gradient(180deg,rgba(14,14,22,.74),rgba(10,10,18,.86));--glass-rim:rgba(255,255,255,.11);--sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;--serif:Georgia,"Times New Roman",serif}}
*{{box-sizing:border-box}} html{{scroll-behavior:smooth}} body{{min-height:100vh;margin:0;background:var(--bg-0);color:var(--text-1);font:14px/1.55 var(--sans);-webkit-font-smoothing:antialiased}} body::before,body::after{{content:"";position:fixed;z-index:-2;pointer-events:none}} body::before{{inset:0;background:radial-gradient(ellipse 36% 56% at -8% 13%,rgba(45,255,225,.105),transparent 68%),radial-gradient(ellipse 35% 56% at 108% 17%,rgba(255,120,112,.09),transparent 68%),radial-gradient(ellipse 80% 62% at 50% 20%,rgba(47,68,125,.07),transparent 74%),linear-gradient(180deg,#070716 0%,var(--bg-0) 42%,#03030c 100%)}} body::after{{inset:0;opacity:.035;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='96' height='96' viewBox='0 0 96 96'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.75' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='.9'/%3E%3C/svg%3E");mix-blend-mode:soft-light}}
.review-document{{position:relative;max-width:1080px;margin:22px auto 36px;padding:18px 30px 34px;background:rgba(4,4,16,.94);border:1px solid var(--border);border-radius:14px;box-shadow:0 24px 60px rgba(0,0,0,.32)}} .context-line{{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;padding:11px 13px;border:1px solid var(--glass-rim);border-radius:12px;background:var(--glass-fill);box-shadow:inset 0 1px 0 rgba(255,255,255,.045),0 10px 28px rgba(0,0,0,.16);color:var(--text-2);font-size:.82rem}} .context-line strong{{color:var(--text-0);font:600 .95rem var(--sans);letter-spacing:-.01em}} .context-line span{{display:flex;gap:5px;align-items:baseline}} small{{color:var(--text-3);font:.64rem var(--mono);letter-spacing:.09em;text-transform:uppercase}}
.verdict{{padding:24px 2px 20px;border-bottom:1px solid var(--border)}} .verdict-eyebrow{{margin:0 0 7px;color:var(--text-3);font:.65rem var(--mono);letter-spacing:.11em;text-transform:uppercase}} h1,h2{{font-family:var(--serif);font-weight:400;color:var(--text-0)}} h1{{font-size:clamp(1.9rem,3.35vw,2.75rem);line-height:1.05;letter-spacing:-.025em;margin:0}} .verdict-subtitle{{margin:9px 0 0;color:var(--text-2);max-width:76ch}}
.surface-counts{{position:sticky;z-index:2;top:0;display:flex;flex-wrap:wrap;gap:8px;padding:11px 2px;border-bottom:1px solid var(--border);background:rgba(4,4,16,.98);font:.75rem var(--mono)}} .surface-counts a{{color:var(--text-2);text-decoration:none;white-space:nowrap}} .surface-counts a:hover,.surface-counts a:focus-visible{{color:var(--teal);text-decoration:underline}} .surface-counts span{{color:var(--text-3)}} .findings-stack{{padding-top:23px}}
.finding-group{{margin:0 0 34px;scroll-margin-top:45px}} .group-heading{{display:flex;align-items:baseline;justify-content:space-between;padding:0 2px 9px;border-bottom:1px solid var(--border);margin-bottom:11px}} .group-heading h2{{font-size:1.28rem;letter-spacing:-.015em;margin:0}} .group-heading span{{color:var(--text-3);font:11px var(--mono)}}
.finding-card{{position:relative;overflow:hidden;padding:17px 18px;margin-top:12px;border:1px solid var(--glass-rim);border-radius:12px;background:var(--glass-fill);box-shadow:inset 0 1px 0 rgba(255,255,255,.045),0 13px 30px rgba(0,0,0,.16)}} .finding-card::before{{content:"";position:absolute;inset:0 auto 0 0;width:2px;background:linear-gradient(180deg,var(--coral),rgba(239,154,145,.18))}} .badges{{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:9px}} .badge{{border:1px solid var(--border);border-radius:5px;padding:2px 6px;color:var(--text-2);font:.63rem/1.3 var(--mono);letter-spacing:.075em;text-transform:uppercase}} .badge.category{{color:var(--teal);border-color:rgba(118,212,198,.22);background:rgba(118,212,198,.055)}} .badge.severity{{color:var(--coral);border-color:rgba(239,154,145,.22);background:rgba(239,154,145,.05)}} .badge.classification.intended{{color:var(--teal)}} .badge.classification.suspicious{{color:var(--coral)}} .badge.classification.unknown{{color:var(--text-2)}}
h3{{color:var(--text-0);font-size:1rem;line-height:1.38;margin:0;font-weight:600}} .finding-context,.finding-explanation,.confidence{{margin:6px 0 0;color:var(--text-2)}} .finding-context{{font:.78rem var(--mono);color:var(--text-3)}} .confidence{{font-size:.78rem}} .evidence-columns{{display:grid;grid-template-columns:1fr 1fr;gap:1px;margin-top:14px;background:var(--border-strong);border:1px solid var(--border)}} .evidence-columns section{{min-width:0;padding:10px 12px;background:#090a13}} .evidence-columns h3,.evidence-columns h4{{margin:0;font:.67rem var(--mono);letter-spacing:.09em;text-transform:uppercase}} .structured{{margin-bottom:0;border-radius:7px 7px 0 0}} .structured .base-column{{color:var(--teal);box-shadow:inset 0 2px 0 var(--teal)}} .structured .target-column{{color:var(--coral);box-shadow:inset 0 2px 0 var(--coral)}} .structured h4{{color:currentColor}}
.field-diff{{border:1px solid var(--border);border-top:0;border-radius:0 0 7px 7px;overflow:hidden}} .field-head,.field-row{{display:grid;grid-template-columns:minmax(125px,1fr) minmax(0,1.35fr) minmax(0,1.35fr)}} .field-head{{color:var(--text-3);font:.64rem var(--mono);letter-spacing:.09em;text-transform:uppercase;background:#0d0e18}} .field-head span,.field-row>*,.field-row div{{padding:8px 10px;overflow-wrap:anywhere}} .field-row{{border-top:1px solid var(--border)}} .field-row:first-of-type{{border-top:0}} .field-row code{{color:var(--text-1);font:12px var(--mono);background:#0d0e18}} .base-value{{background:var(--teal-fill);color:#d2f0ea}} .target-value{{background:var(--coral-fill);color:#f8d8d4}} .field-row.unchanged .base-value,.field-row.unchanged .target-value{{background:#10111b;color:var(--text-1)}} .field-row em{{color:var(--text-2);font-size:.82rem;font-style:normal}}
.raw-evidence{{margin-top:12px;border:1px solid var(--border);border-radius:7px;overflow:hidden;background:#090a13}} .raw-evidence summary{{padding:10px 12px;color:var(--text-2);cursor:pointer;font:.78rem var(--sans)}} .raw-evidence[open] summary{{border-bottom:1px solid var(--border)}} pre{{margin:0;white-space:pre-wrap;overflow-wrap:anywhere;color:var(--text-1);font:12px/1.5 var(--mono)}} .clean{{padding:15px 17px;border:1px solid rgba(118,212,198,.22);border-left:2px solid var(--teal);border-radius:12px;background:var(--glass-fill);box-shadow:inset 0 1px 0 rgba(255,255,255,.045);color:var(--text-2)}} .clean p{{margin:0}} footer{{padding-top:15px;border-top:1px solid var(--border);color:var(--text-3);font-size:.76rem}}
@media(max-width:640px){{.review-document{{margin:0;border-width:0;border-radius:0;padding:16px 14px 28px}}.context-line{{padding:10px 11px;border-radius:10px}}.verdict{{padding:22px 1px 18px}}h1{{font-size:clamp(1.75rem,9vw,2.25rem)}}.field-head{{display:none}}.field-row{{grid-template-columns:1fr}}.field-row code{{background:#0d0e18}}.field-row .base-value:before,.field-row .target-value:before{{display:block;margin-bottom:3px;font:.61rem var(--mono);letter-spacing:.09em;text-transform:uppercase}}.field-row .base-value:before{{content:"Base"}}.field-row .target-value:before{{content:"Target"}}.evidence-columns{{grid-template-columns:1fr}}.structured{{display:none}}}}
@media print{{:root{{color-scheme:light}}body{{background:#fff;color:#111}}body::before,body::after{{display:none}}.review-document{{max-width:none;margin:0;padding:14px;background:#fff;border:0;box-shadow:none}}.context-line,.verdict,.surface-counts,.group-heading,footer{{border-color:#999}}.context-line,.finding-card,.clean{{background:#fff;box-shadow:none;border-color:#999}}.surface-counts{{position:static;background:#fff}}.finding-card::before{{background:#777}}.field-diff,.raw-evidence,.evidence-columns{{border-color:#999}}.evidence-columns section{{background:#fff}}.field-head,.field-row code{{background:#f3f3f3}}.base-value{{background:#e5f3f0;color:#111}}.target-value{{background:#f8e7e2;color:#111}}.raw-evidence{{break-inside:avoid}}}}
</style></head><body>{body}<script id="behaviordiff-review-data" type="application/json">{blob}</script></body></html>'''


def write_report(path: str | Path, payload: dict[str, Any]) -> Path:
    """Write a review document, creating a requested output directory."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_report(payload), encoding="utf-8")
    return destination
