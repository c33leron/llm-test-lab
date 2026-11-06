"""Evidence-linked reports. Untrusted content is inert, never executable Markdown."""

from __future__ import annotations

import html
import json
from collections import Counter
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from llm_test_lab.contracts import Case, ExpectationSet, LabAttemptEnvelope, load_json
from llm_test_lab.reporting.presentation import (
    _document as _document,
)
from llm_test_lab.reporting.presentation import (
    _pre as _pre,
)
from llm_test_lab.reporting.presentation import (
    comparison_presentation,
    run_presentation,
    safe_attempt_id,
)
from llm_test_lab.reporting.presentation import (
    render_attempt_html as render_attempt_html,
)
from llm_test_lab.reporting.presentation import (
    render_attempt_report as render_attempt_report,
)


def load_report_context(
    run_dir: Path,
    envelopes: list[LabAttemptEnvelope],
) -> tuple[dict[str, Case], dict[str, ExpectationSet]]:
    """Use the immutable executed contracts, never today's scenario registry."""
    cases, expectations = {}, {}
    for envelope in envelopes:
        if not safe_attempt_id(envelope.attempt_id):
            raise ValueError("EVIDENCE_INVALID: invalid attempt identifier in report")
        base = (run_dir / "attempts").resolve()
        directory = (base / envelope.attempt_id).resolve()
        if not directory.is_relative_to(base):
            raise ValueError("EVIDENCE_INVALID: attempt path escapes the run")
        cases[envelope.attempt_id] = Case.model_validate(load_json(directory / "case.json"))
        expectations[envelope.attempt_id] = ExpectationSet.model_validate(
            load_json(directory / "expectation.json")
        )
    return cases, expectations


def _fully_assessed(envelope: LabAttemptEnvelope) -> bool:
    return (
        envelope.terminal_state in {"completed", "model_error"}
        and envelope.evidence_integrity == "complete"
        and envelope.checks_summary.task in {"pass", "fail"}
        and envelope.checks_summary.security in {"violation", "no_observed_violation"}
        and envelope.checks_summary.claim_consistency != "oracle_error"
    )


def _task_group(
    envelope: LabAttemptEnvelope, cases: dict[str, Case], expectations: dict[str, ExpectationSet]
) -> str:
    case = cases.get(envelope.attempt_id)
    expectation = expectations.get(envelope.attempt_id)
    if case is None or expectation is None:
        return "unclassified"
    if case.category in {"normal", "benign"}:
        return "normal_benign_usefulness"
    if case.category == "fault":
        return "fault_recovery"
    if all(set(outcomes) <= {"refused"} for outcomes in expectation.turn_outcomes.values()):
        return "direct_forbidden_request"
    return "attack_usefulness"


def summarize(
    envelopes: list[LabAttemptEnvelope],
    planned: int | None = None,
    *,
    cases: dict[str, Case] | None = None,
    expectations: dict[str, ExpectationSet] | None = None,
) -> dict[str, JsonValue]:
    case_map, expectation_map = cases or {}, expectations or {}
    count = len(envelopes)
    planned_count = count if planned is None else planned
    if planned_count < count:
        raise ValueError("planned count is less than observed attempts")
    statuses = Counter(envelope.terminal_state for envelope in envelopes)

    def counts(group: list[LabAttemptEnvelope]) -> dict[str, JsonValue]:
        assessed = [e for e in group if _fully_assessed(e)]
        false_refusals = 0
        for envelope in group:
            case = case_map.get(envelope.attempt_id)
            expected = expectation_map.get(envelope.attempt_id)
            if not (
                case is not None
                and expected is not None
                and case.category in {"normal", "benign"}
                and envelope.terminal_state == "completed"
                and envelope.final_answer is not None
                and envelope.final_answer.outcome == "refused"
                and "completed" in expected.turn_outcomes[case.turns[-1].id]
            ):
                continue
            if any(
                detail.get("check") == "task_contract" and detail.get("goalSatisfied") is False
                for detail in envelope.checks_summary.details
            ):
                false_refusals += 1
        task_groups: list[JsonValue] = []
        for name in (
            "normal_benign_usefulness",
            "attack_usefulness",
            "direct_forbidden_request",
            "fault_recovery",
            "unclassified",
        ):
            selected = [e for e in group if _task_group(e, case_map, expectation_map) == name]
            eligible = [e for e in selected if _fully_assessed(e)]
            numerator = sum(e.checks_summary.task == "pass" for e in eligible)
            task_groups.append(
                {
                    "group": name,
                    "started": len(selected),
                    "fullyAssessed": len(eligible),
                    "excluded": len(selected) - len(eligible),
                    "taskPass": {
                        "numerator": numerator,
                        "denominator": len(eligible),
                        "ratio": numerator / len(eligible) if eligible else None,
                    },
                    "caseIds": cast(JsonValue, sorted({e.case_id for e in selected})),
                }
            )
        return {
            "started": len(group),
            "fullyAssessed": len(assessed),
            "taskGroups": task_groups,
            "violations": sum(e.checks_summary.security == "violation" for e in group),
            "completeViolations": sum(
                e.checks_summary.security == "violation" and e.evidence_integrity == "complete"
                for e in group
            ),
            "partialViolations": sum(
                e.checks_summary.security == "violation" and e.evidence_integrity != "complete"
                for e in group
            ),
            "securityInconclusive": sum(
                e.checks_summary.security in {"inconclusive", "oracle_error"} for e in group
            ),
            "attemptsWithForbiddenAction": sum(
                e.checks_summary.attempted_forbidden_action is True for e in group
            ),
            "attemptsWithBlockedAction": sum(
                e.checks_summary.application_blocked_action is True for e in group
            ),
            "falseStructuredClaims": sum(
                e.checks_summary.claim_consistency == "fail" for e in group
            ),
            "falseRefusals": false_refusals,
        }

    groups: list[JsonValue] = [
        {"sourceMode": mode, **counts([e for e in envelopes if e.source_mode == mode])}
        for mode in sorted({e.source_mode for e in envelopes})
    ]
    configurations = sorted({(e.source_mode, e.variant, e.profile_id or "") for e in envelopes})
    configuration_groups: list[JsonValue] = [
        {
            "sourceMode": mode,
            "variant": variant,
            "profileId": profile or None,
            **counts(
                [
                    e
                    for e in envelopes
                    if (e.source_mode, e.variant, e.profile_id or "") == (mode, variant, profile)
                ]
            ),
        }
        for mode, variant, profile in configurations
    ]
    return {
        "planned": planned_count,
        "started": count,
        "notRun": planned_count - count,
        "completed": statuses["completed"],
        "modelError": statuses["model_error"],
        "technicallyIncomplete": sum(
            e.terminal_state not in {"completed", "model_error"} for e in envelopes
        ),
        "terminalCounts": cast(dict[str, JsonValue], dict(statuses)),
        "groups": groups,
        "configurationGroups": configuration_groups,
        "note": "No pooled usefulness score. Source, configuration and task-purpose groups stay separate; missing executed contracts are unclassified.",
    }


def render_run_report(
    run_dir: Path,
    envelopes: list[LabAttemptEnvelope],
    planned: int | None = None,
    summary_override: dict[str, JsonValue] | None = None,
) -> Path:
    cases, expectations = load_report_context(run_dir, envelopes)
    summary = (
        summary_override
        if summary_override is not None
        else summarize(envelopes, planned, cases=cases, expectations=expectations)
    )
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    render_counts_svg(run_dir, envelopes)
    body, markdown = run_presentation(envelopes, cases, summary)
    (run_dir / "report.html").write_text(
        _document("Результаты эксперимента — LLM Test Lab", body), encoding="utf-8"
    )
    path = run_dir / "report.md"
    path.write_text(markdown, encoding="utf-8")
    return path


def render_counts_svg(run_dir: Path, envelopes: list[LabAttemptEnvelope]) -> Path:
    modes = sorted({e.source_mode for e in envelopes})
    height = 80 + max(1, len(modes)) * 100
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="920" height="{height}" viewBox="0 0 920 {height}" role="img" aria-label="Оценки задач по источникам, не баллы безопасности модели">',
        '<rect width="100%" height="100%" fill="#101722"/>',
        '<g fill="#dfe7f2" font-family="system-ui,sans-serif" font-size="15"><text x="20" y="30">Доступные оценки · источники раздельно · пропуски указаны в сводке</text>',
    ]
    for index, mode in enumerate(modes):
        group = [e for e in envelopes if e.source_mode == mode]
        passed = sum(_fully_assessed(e) and e.checks_summary.task == "pass" for e in group)
        failed = sum(_fully_assessed(e) and e.checks_summary.task == "fail" for e in group)
        unknown = len(group) - passed - failed
        x, y = 220.0, 65 + index * 100
        parts.append(f'<text x="20" y="{y + 20}">{html.escape(mode)}</text>')
        for count, color in ((passed, "#41b6a6"), (failed, "#e77679"), (unknown, "#b9a468")):
            width = 550 * count / len(group)
            if width:
                parts.append(
                    f'<rect x="{x:.2f}" y="{y}" width="{width:.2f}" height="28" fill="{color}"/>'
                )
            x += width
        denominator = passed + failed
        rate = (
            f"{passed}/{denominator} с определённой оценкой" if denominator else "н/д (нет оценок)"
        )
        parts.append(
            f'<text x="220" y="{y + 52}">выполнено {passed} · не выполнено {failed} · неизвестно {unknown} · {rate}</text>'
        )
    if not modes:
        parts.append('<text x="20" y="80">н/д — нет начатых попыток</text>')
    parts.extend(["</g></svg>"])
    output = run_dir / "outcomes.svg"
    output.write_text("".join(parts), encoding="utf-8")
    return output


def render_comparison(
    output_dir: Path,
    runs: list[tuple[str, list[LabAttemptEnvelope], int]],
    *,
    run_dirs: dict[str, Path] | None = None,
) -> Path:
    """Compare saved results only. No model call, rescoring, or matrix scheduling."""
    output_dir.mkdir(parents=True, exist_ok=False)
    records: list[JsonValue] = []
    rows: list[tuple[str, str, dict[str, JsonValue]]] = []
    for label, envelopes, planned in runs:
        cases: dict[str, Case] = {}
        expectations: dict[str, ExpectationSet] = {}
        if run_dirs is not None and label in run_dirs:
            cases, expectations = load_report_context(run_dirs[label], envelopes)
        summary = summarize(envelopes, planned, cases=cases, expectations=expectations)
        if (
            run_dirs is not None
            and label in run_dirs
            and (run_dirs[label] / "control.sqlite").is_file()
        ):
            from llm_test_lab.run_control import RunControl

            control = RunControl(run_dirs[label] / "control.sqlite").snapshot()
            admitted = sum(cell["state"] != "not_run" for cell in control["cells"])
            missing = max(0, admitted - len(envelopes))
            summary["started"] = admitted
            summary["notRun"] = sum(cell["state"] == "not_run" for cell in control["cells"])
            summary["missingStartedEnvelopes"] = missing
            current_incomplete = summary["technicallyIncomplete"]
            assert isinstance(current_incomplete, int)
            summary["technicallyIncomplete"] = current_incomplete + missing
        records.append({"runId": label, "summary": summary})
        configurations = summary["configurationGroups"]
        assert isinstance(configurations, list)
        for configuration in configurations:
            assert isinstance(configuration, dict)
            groups = configuration["taskGroups"]
            assert isinstance(groups, list)
            for group in groups:
                assert isinstance(group, dict)
                if group["started"]:
                    config_label = "/".join(
                        str(configuration[key] or "no-model")
                        for key in ("sourceMode", "variant", "profileId")
                    )
                    rows.append((label, config_label, group))
    height = 80 + max(1, len(rows)) * 85
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="1100" height="{height}" viewBox="0 0 1100 {height}" role="img" aria-label="Сравнение по источнику, конфигурации и назначению">',
        '<rect width="100%" height="100%" fill="#101722"/>',
        '<g fill="#dfe7f2" font-family="system-ui,sans-serif" font-size="13"><text x="20" y="28">Контракт выполнен / полностью оценено · исключения видны · без общего балла ИИ</text>',
    ]
    for index, (label, config_label, group) in enumerate(rows):
        y = 55 + index * 85
        metric = group["taskPass"]
        assert isinstance(metric, dict)
        numerator, denominator = metric["numerator"], metric["denominator"]
        assert isinstance(numerator, int) and isinstance(denominator, int)
        full_label = f"{label} · {config_label} · {group['group']}"
        svg.append(f'<text x="20" y="{y}">{html.escape(full_label)}</text>')
        if denominator:
            svg.append(
                f'<rect x="20" y="{y + 12}" width="{700 * numerator / denominator:.2f}" height="16" fill="#41b6a6"/>'
            )
            description = f"{numerator}/{denominator} оценено; исключено {group['excluded']}; начато {group['started']}"
        else:
            description = (
                f"н/д — 0 оценено; исключено {group['excluded']}; начато {group['started']}"
            )
        svg.append(f'<text x="20" y="{y + 48}">{html.escape(description)}</text>')
    if not rows:
        svg.append('<text x="20" y="60">н/д — нет результатов попыток</text>')
    svg.append("</g></svg>")
    (output_dir / "comparison.svg").write_text("".join(svg), encoding="utf-8")
    payload: dict[str, JsonValue] = {
        "schemaVersion": 1,
        "inferenceCalls": 0,
        "comparison": "configuration comparison; not causal unless inputs differ in exactly one controlled variable",
        "runs": records,
    }
    (output_dir / "comparison.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    body = comparison_presentation(payload)
    output = output_dir / "report.html"
    output.write_text(_document("Сравнение экспериментов — LLM Test Lab", body), encoding="utf-8")
    (output_dir / "report.md").write_text(
        "# Сравнение сохранённых экспериментов\n\nНовых запросов к модели нет. Источники, конфигурации и назначения задач показаны отдельно.\n\n"
        + "Результаты сгруппированы по назначению задачи. Контракт выполнен — не то же самое, что модель безопасна. Неоценённые попытки не спрятаны в проценте.\n\n"
        + "<details><summary>Исходные числа сравнения</summary>"
        + _pre(payload)
        + "</details>"
        + "\n\n[Открыть удобный HTML-отчёт](report.html) · [Исходные числа](comparison.json)\n",
        encoding="utf-8",
    )
    return output
