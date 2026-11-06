"""Rendering contracts: novices see the result; untrusted content stays inert."""

from html.parser import HTMLParser
from pathlib import Path

import pytest

from llm_test_lab.agent import run_attempt
from llm_test_lab.contracts import Case, Limits, load_json
from llm_test_lab.reporting import (
    load_report_context,
    render_attempt_report,
    render_counts_svg,
    render_run_report,
    summarize,
)
from llm_test_lab.reporting.presentation import evidence_href, run_presentation
from llm_test_lab.tickets import read_events, read_snapshot

ROOT = Path(__file__).resolve().parents[2]


class Page(HTMLParser):
    def __init__(self, document: str):
        super().__init__()
        self.tags = []
        self.links = []
        self.images = []
        self.visible = []
        self.pre_outside_details = 0
        self.depth = 0
        self.details_open = []
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        self.tags.append((tag, values))
        assert not any(name.startswith("on") for name in values)
        if tag == "details":
            self.depth += 1
            self.details_open.append("open" in values)
        if tag == "pre" and not self.depth:
            self.pre_outside_details += 1
        if tag == "a":
            self.links.append(values.get("href", ""))
        if tag == "img":
            self.images.append(values.get("src", ""))

    def handle_endtag(self, tag):
        if tag == "details":
            self.depth -= 1

    def handle_data(self, text):
        if not self.depth:
            self.visible.append(text)


async def make_attempt(tmp_path: Path, case_id="C01", variant="enforced"):
    return await run_attempt(
        root=ROOT,
        run_dir=tmp_path,
        cell={
            "attemptId": "a1",
            "caseId": case_id,
            "variant": variant,
            "sourceMode": "fixture",
            "profileId": None,
            "resolvedModel": None,
            "repeat": 1,
            "seed": 7,
            "limits": Limits().to_dict(),
        },
    )


async def test_attempt_human_summary_diff_and_raw_only_on_demand(tmp_path: Path) -> None:
    await make_attempt(tmp_path)
    directory = tmp_path / "attempts/a1"
    page = Page((directory / "report.html").read_text())
    visible = " ".join(page.visible)
    assert "Учебный сценарий — без нейросети" in visible
    assert "Что произошло на самом деле" in visible
    assert "Что изменилось в базе" in visible
    assert "Сохранённых изменений агентом: 1" in visible
    assert all(text in visible for text in ["Было", "Стало", "open", "closed", "Версия записи"])
    assert "Что сказал агент — и что проверил стенд" in visible
    assert "Запрошен update_ticket: A-101" in visible
    assert "Изменение A-101 сохранено" in visible
    assert '"checksSummary"' not in visible
    assert page.pre_outside_details == 0
    assert not any(page.details_open)
    assert '<html lang="ru">' in (directory / "report.html").read_text()
    assert not any(tag in {"script", "link", "iframe", "object", "embed"} for tag, _ in page.tags)
    assert not page.images
    assert all(not href.startswith(("https:", "http:", "javascript:", "//")) for href in page.links)
    assert "<details>" in (directory / "report.md").read_text()


async def test_run_table_precedes_technical_data_and_expected_violation_is_separate(
    tmp_path: Path,
) -> None:
    envelope = await make_attempt(tmp_path, "F01", "vulnerable")
    cases, expectations = load_report_context(tmp_path, [envelope])
    summary = summarize([envelope], cases=cases, expectations=expectations)
    summary["gateResult"] = {
        "gate": "fixture-contract",
        "exitCode": 0,
        "promptfooExitCode": 100,
        "reasons": [],
    }
    render_run_report(tmp_path, [envelope], planned=1, summary_override=summary)
    document = (tmp_path / "report.html").read_text()
    page = Page(document)
    visible = " ".join(page.visible)
    assert "Стенд отработал по учебному эталону" in visible
    assert "Нарушение обнаружено" in visible
    assert "Контракт не выполнен" in visible
    assert document.index('id="attempts"') < document.index("Машинная сводка")
    assert "attempts/a1/report.html" in page.links
    assert page.pre_outside_details == 0
    assert not any(page.details_open)
    assert page.images == ["outcomes.svg"]
    assert "default-src 'none'" in document
    assert "url(" not in document and "@import" not in document


async def test_violation_and_timeout_both_visible(tmp_path: Path) -> None:
    await make_attempt(tmp_path, "F02", "vulnerable")
    page = Page((tmp_path / "attempts/a1/report.html").read_text())
    visible = " ".join(page.visible)
    assert "Нарушение обнаружено" in visible
    assert "Истекло время ожидания" in visible
    assert "Материалы неполные" in visible
    assert "уже обнаруженное нарушение" in visible.lower()


async def test_invalid_evidence_is_not_a_positive_card_or_green_chart(tmp_path: Path) -> None:
    envelope = await make_attempt(tmp_path)
    envelope.evidence_integrity = "invalid"
    cases, expectations = load_report_context(tmp_path, [envelope])
    summary = summarize([envelope], cases=cases, expectations=expectations)
    body, _ = run_presentation([envelope], cases, summary)
    visible = " ".join(Page(body).visible)
    assert "Оценка не подтверждена" in visible
    assert "Безопасный исход не подтверждён" in visible
    assert "Целостность нарушена" in visible
    chart = render_counts_svg(tmp_path, [envelope]).read_text()
    assert "выполнено 0 · не выполнено 0 · неизвестно 1" in chart
    assert 'fill="#41b6a6"' not in chart
    assert "н/д (нет оценок)" in chart


async def test_untrusted_text_and_links_cannot_execute_or_load_remote_content(
    tmp_path: Path,
) -> None:
    envelope = await make_attempt(tmp_path)
    source = tmp_path / "attempts/a1"
    case = Case.model_validate(load_json(source / "case.json"))
    hostile = '</h1><script>alert(1)</script>\n\n![leak](https://exfil.invalid/data)\n<img src="https://exfil.invalid/data" onerror="alert(1)">'
    case.goal = hostile
    envelope.final_answer.message = hostile
    before = load_json(source / "state-before.json")
    after = read_snapshot(source / "state.sqlite")
    events = read_events(source / "state.sqlite")
    for event in events:
        if event.type == "user_turn":
            event.payload["user"] = hostile
    output = tmp_path / "rendered-copy"
    output.mkdir()
    render_attempt_report(output, envelope, case, before, after, events)
    page = Page((output / "report.html").read_text())
    assert not any(tag in {"script", "iframe", "img"} for tag, _ in page.tags)
    assert page.pre_outside_details == 0
    markdown = (output / "report.md").read_text()
    assert "![leak](https://exfil.invalid/data)" not in markdown.split("<details>", 1)[0]
    assert "\\!\\[leak\\]" in markdown
    assert "&lt;script&gt;" in (output / "report.html").read_text()
    for ref in [
        "javascript:alert(1)",
        "https://exfil.invalid/report.md",
        "//exfil.invalid/x",
        "attempts/../report.md",
        "attempts/a1/%2e%2e/report.md",
        "attempts/a1/../../report.md",
        "attempts/a1/report.md?url=https://exfil.invalid",
    ]:
        envelope.evidence_ref = ref
        assert evidence_href(envelope) is None
    envelope.evidence_ref = "attempts/a1/recovered/report.md"
    assert evidence_href(envelope) == "attempts/a1/recovered/report.html"


async def test_context_cannot_traverse_outside_run(tmp_path: Path) -> None:
    envelope = await make_attempt(tmp_path)
    envelope.attempt_id = "../../secret"
    with pytest.raises(ValueError, match="invalid attempt identifier"):
        load_report_context(tmp_path, [envelope])


async def test_state_diff_distinguishes_environment_intervention(tmp_path: Path) -> None:
    await make_attempt(tmp_path, "C04")
    visible = " ".join(Page((tmp_path / "attempts/a1/report.html").read_text()).visible)
    assert "учебная среда" in visible
    assert "Среда изменила A-101" in visible
    assert "не считается действием агента" in visible
    assert "normal" in visible and "high" in visible


async def test_partial_scores_do_not_enter_chart_denominator(tmp_path: Path) -> None:
    envelope = await make_attempt(tmp_path)
    envelope.evidence_integrity = "partial"
    envelope.terminal_state = "timeout"
    envelope.checks_summary.task = "fail"
    chart = render_counts_svg(tmp_path, [envelope]).read_text()
    assert "выполнено 0 · не выполнено 0 · неизвестно 1" in chart
    assert "н/д (нет оценок)" in chart
