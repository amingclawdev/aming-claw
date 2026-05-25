from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "aming-claw" / "SKILL.md"


def test_observer_operating_modes_are_documented():
    text = SKILL.read_text(encoding="utf-8")

    assert "## Observer Operating Modes" in text
    assert "### Design Alignment Mode" in text
    assert "### Execution Supervisor Mode" in text


def test_observer_design_mode_dispatch_and_stop_boundary():
    text = SKILL.read_text(encoding="utf-8")

    assert "Design Alignment Mode is dispatch-and-stop" in text
    assert "do not\nwait for completion" in text
    assert "unless the user explicitly switches to execution supervision" in text


def test_observer_execution_mode_wait_audit_merge_boundary():
    text = SKILL.read_text(encoding="utf-8")

    assert "Execution Supervisor Mode is used only after explicit user intent" in text
    assert "wait for subagent completion" in text
    assert "timeline precheck" in text
    assert "merge gates" in text
    assert "cannot bypass contract, precheck, timeline, or\nmerge gates" in text


def test_observer_mode_trigger_phrases_and_v1_chain_boundary():
    text = SKILL.read_text(encoding="utf-8")

    assert "启动 subagent 后停止" in text
    assert "进入执行模式" in text
    assert "Chain is not the default path for routine implementation" in text
    assert "observer-led Manual Fix" in text
