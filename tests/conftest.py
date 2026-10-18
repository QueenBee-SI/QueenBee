"""The suite runs where no agent is installed (CI, a clean machine): when the catalog finds no binary for an agent,
a stand-in path is returned, so that every test that mocks the host still sees a worker start. Where the agent is installed, the
real path is used as before."""
import os
import tempfile

import pytest

from queenbee.agents import catalog

_real_find = catalog._find_binary


def _find_or_stand_in(spec):
    return _real_find(spec) or f"{spec.binaries[0]}.exe"


@pytest.fixture(autouse=True)
def _agents_always_found(monkeypatch):
    monkeypatch.setattr(catalog, "_find_binary", _find_or_stand_in)


@pytest.fixture(autouse=True, scope="session")
def _own_state_folder():
    """The suite never reads or writes this computer's QueenBee state (saved queens, plans, logs): a folder of its own."""
    before = os.environ.get("QUEENBEE_STATE_DIR")           # a developer's own setting is put back afterwards
    with tempfile.TemporaryDirectory(prefix="queenbee-test-state-") as d:
        os.environ["QUEENBEE_STATE_DIR"] = d
        try:
            yield
        finally:
            if before is None:
                os.environ.pop("QUEENBEE_STATE_DIR", None)
            else:
                os.environ["QUEENBEE_STATE_DIR"] = before
