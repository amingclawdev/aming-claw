"""Tests for chain_trailer RUNTIME_VERSION and get_runtime_version()."""


def test_runtime_version_set_at_import():
    from agent.governance import chain_trailer
    assert chain_trailer.RUNTIME_VERSION
    assert isinstance(chain_trailer.RUNTIME_VERSION, str)


def test_get_runtime_version_returns_constant():
    from agent.governance.chain_trailer import RUNTIME_VERSION, get_runtime_version
    assert get_runtime_version() == RUNTIME_VERSION


def test_runtime_version_stable_across_calls():
    from agent.governance.chain_trailer import get_runtime_version
    v1 = get_runtime_version()
    v2 = get_runtime_version()
    assert v1 == v2
