from pathlib import Path

import pytest

from llm_test_lab.agent import run_attempt
from llm_test_lab.contracts import Limits, load_json
from llm_test_lab.evidence import read_envelope, rescore_bundle, verify_bundle
from llm_test_lab.reporting import _pre, summarize

ROOT = Path(__file__).resolve().parents[2]


async def attempt(tmp_path: Path):
    return await run_attempt(
        root=ROOT,
        run_dir=tmp_path,
        cell={
            "attemptId": "a1",
            "caseId": "C01",
            "variant": "enforced",
            "sourceMode": "fixture",
            "profileId": None,
            "resolvedModel": None,
            "repeat": 1,
            "seed": 7,
            "limits": Limits().to_dict(),
        },
    )


async def test_rescore_preserves_original_and_uses_no_inference(tmp_path: Path) -> None:
    envelope = await attempt(tmp_path)
    path = tmp_path / "attempts/a1"
    original = {p.name: p.read_bytes() for p in path.iterdir() if p.is_file()}
    result = rescore_bundle(path)
    data = load_json(result)
    assert data["inferenceCalls"] == 0
    assert data["sourceMode"] == "recorded"
    assert data["originalSourceMode"] == "fixture"
    assert all((path / name).read_bytes() == content for name, content in original.items())
    assert verify_bundle(path) == []
    assert read_envelope(path, verify=True) == envelope


async def test_hash_corruption_invalidates_but_original_verdict_remains_readable(
    tmp_path: Path,
) -> None:
    await attempt(tmp_path)
    path = tmp_path / "attempts/a1"
    (path / "conversation.json").write_text("[]\n")
    assert verify_bundle(path)
    with pytest.raises(ValueError, match="EVIDENCE_INVALID"):
        rescore_bundle(path)
    with pytest.raises(ValueError, match="EVIDENCE_INVALID"):
        read_envelope(path, verify=True)
    assert read_envelope(path).case_id == "C01"


def test_no_markdown_execution_in_untrusted_payload() -> None:
    output = _pre(
        "<script>alert(1)</script>\n![leak](https://external.invalid/secret)\n```\n<img src=x>"
    )
    assert "<script>" not in output
    assert "<img" not in output
    assert "&lt;script&gt;" in output
    assert output.startswith("<pre>") and output.endswith("</pre>")


async def test_denominators_and_modes_do_not_pool(tmp_path: Path) -> None:
    original = await attempt(tmp_path)
    bad = original.model_copy(deep=True)
    bad.attempt_id = "a2"
    bad.source_mode = "live-local"
    bad.terminal_state = "timeout"
    bad.evidence_integrity = "partial"
    bad.checks_summary.task = "inconclusive"
    bad.checks_summary.security = "violation"
    result = summarize([original, bad], planned=3)
    assert result["notRun"] == 1
    assert result["completed"] == 1
    assert result["technicallyIncomplete"] == 1
    groups = {g["sourceMode"]: g for g in result["groups"]}
    assert groups["fixture"]["taskGroups"][-1]["taskPass"] == {
        "numerator": 1,
        "denominator": 1,
        "ratio": 1.0,
    }
    assert groups["live-local"]["taskGroups"][-1]["taskPass"] == {
        "numerator": 0,
        "denominator": 0,
        "ratio": None,
    }
    assert groups["live-local"]["violations"] == 1


async def test_manual_category_denominators_configurations_and_false_refusal(
    tmp_path: Path,
) -> None:
    from llm_test_lab.reporting import load_report_context

    envelopes = []
    for index, case_id in enumerate(["C01", "C09", "C08", "C06", "C02", "C01", "C01", "C11"]):
        envelopes.append(
            await run_attempt(
                root=ROOT,
                run_dir=tmp_path,
                cell={
                    "attemptId": f"a{index}",
                    "caseId": case_id,
                    "variant": "enforced",
                    "sourceMode": "fixture",
                    "profileId": None,
                    "resolvedModel": None,
                    "repeat": 1,
                    "seed": 7,
                    "limits": Limits().to_dict(),
                },
            )
        )
    cases, expectations = load_report_context(tmp_path, envelopes)
    # Controlled synthetic envelope mixture, not a claim about an actual model.
    envelopes[1].terminal_state = "model_error"
    envelopes[1].checks_summary.task = "fail"
    envelopes[1].checks_summary.model_protocol = "fail"
    envelopes[5].terminal_state = "timeout"
    envelopes[5].evidence_integrity = "partial"
    envelopes[5].checks_summary.task = "inconclusive"
    envelopes[6].final_answer.outcome = "refused"
    envelopes[6].checks_summary.task = "fail"
    for detail in envelopes[6].checks_summary.details:
        if detail.get("check") == "task_contract":
            detail["goalSatisfied"] = False
    result = summarize(envelopes, planned=10, cases=cases, expectations=expectations)
    groups = {g["group"]: g for g in result["groups"][0]["taskGroups"]}
    assert result["notRun"] == 2
    assert result["groups"][0]["falseRefusals"] == 1
    assert groups["normal_benign_usefulness"]["taskPass"] == {
        "numerator": 2,
        "denominator": 4,
        "ratio": 0.5,
    }
    assert groups["normal_benign_usefulness"]["excluded"] == 1
    for name in ["attack_usefulness", "direct_forbidden_request", "fault_recovery"]:
        assert groups[name]["taskPass"] == {"numerator": 1, "denominator": 1, "ratio": 1.0}
    assert groups["unclassified"]["taskPass"]["ratio"] is None
    assert len(result["configurationGroups"]) == 1
    envelopes[0].variant = "prompt_only"
    assert (
        len(summarize(envelopes, cases=cases, expectations=expectations)["configurationGroups"])
        == 2
    )


async def test_comparison_static_artifacts_from_saved_context(tmp_path: Path) -> None:
    from llm_test_lab.reporting import render_comparison

    run = tmp_path / "original"
    envelope = await attempt(run)
    output = render_comparison(
        tmp_path / "comparison", [("first-run", [envelope], 1)], run_dirs={"first-run": run}
    )
    assert output.is_file()
    payload = load_json(output.parent / "comparison.json")
    assert payload["inferenceCalls"] == 0
    task_groups = payload["runs"][0]["summary"]["groups"][0]["taskGroups"]
    assert task_groups[0]["group"] == "normal_benign_usefulness"
    assert task_groups[0]["taskPass"]["denominator"] == 1
    assert "<script" not in output.read_text()
    assert (output.parent / "comparison.svg").is_file()
    with pytest.raises(FileExistsError):
        render_comparison(output.parent, [])


def test_killed_worker_recovery_preserves_violation_without_touching_originals(
    tmp_path: Path,
) -> None:
    import os
    import signal
    import subprocess
    import sys

    from llm_test_lab.evidence import recover_interrupted_bundle

    script = """
import os,signal,sys
from pathlib import Path
from llm_test_lab.registry import Registry
from llm_test_lab.tickets import TicketStore
from llm_test_lab.evidence import initialize_bundle
from llm_test_lab.contracts import Limits
root, directory=Path(sys.argv[1]),Path(sys.argv[2])
directory.mkdir(parents=True)
r=Registry(root)
c=r.load_case("F01")
f=r.load_fixture(c.fixture)
s=TicketStore(directory/"state.sqlite",f,"crash-a1",c.actor,"vulnerable")
cell={"attemptId":"crash-a1","caseId":"F01","variant":"vulnerable","sourceMode":"fixture","profileId":None,"resolvedModel":None,"repeat":1,"seed":7,"limits":Limits().to_dict()}
initialize_bundle(directory,case=c,expectation=r.load_expectation(c.expectation_set),fixture=f,before=s.snapshot(),cell=cell)
s.execute("get_ticket",{"ticketId":"B-201"},"t1","cmd1","call1")
s.execute("update_ticket",{"ticketId":"B-201","expectedRevision":0,"patch":{"status":"closed"}},"t1","cmd1","call2")
os.kill(os.getpid(),signal.SIGKILL)
"""
    directory = tmp_path / "attempts/crash-a1"
    process = subprocess.run(
        [sys.executable, "-c", script, str(ROOT), str(directory)],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        check=False,
    )
    assert process.returncode == -signal.SIGKILL
    originals = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    derived = recover_interrupted_bundle(directory)
    assert not (directory / "recovered").exists()
    assert derived.terminal_state == "interrupted"
    assert derived.evidence_integrity == "partial"
    assert derived.checks_summary.security == "violation"
    assert all((directory / name).read_bytes() == content for name, content in originals.items())
    saved = recover_interrupted_bundle(directory, persist=True)
    assert saved.checks_summary.security == "violation"
    assert saved.evidence_ref.endswith("recovered/report.md")
    assert not (directory / "envelope.json").exists()
    assert read_envelope(directory, verify=True) == saved
    assert verify_bundle(directory) == []
    assert all((directory / name).read_bytes() == content for name, content in originals.items())
    metadata = load_json(directory / "recovered/manifest.json")
    assert metadata["recoveredFromAudit"] is True
    assert "conversation.json" in metadata["missingOriginalArtifacts"]
    assert metadata["evidenceIntegrity"] == "partial"
