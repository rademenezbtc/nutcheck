import pytest

from nutcheck.client import MintClient


@pytest.fixture(autouse=True)
def fast_pacing(monkeypatch):
    """The local test mint is Nutshell, so NutCheck would pace it like a real one. Keep tests quick."""
    monkeypatch.setattr(MintClient, "slow_interval", 0.05)
    monkeypatch.setattr(MintClient, "min_interval", 0.0)
