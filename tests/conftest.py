import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cloud_architect.pricing import PriceBook

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "prices.json").read_text(encoding="utf-8"))


class FixtureBook(PriceBook):
    """Real Azure prices captured from the API (NZ North, Australia East); anything else is 'not sold'."""

    def __init__(self):
        self.offline, self.ttl, self._fx = True, 0, None

    def items(self, filt, currency="USD"):
        return FIXTURE.get(f"{currency}|{filt}", [])


@pytest.fixture
def book():
    return FixtureBook()


def tool_call(name, args, call_id="c1"):
    return SimpleNamespace(id=call_id, type="function", function=SimpleNamespace(name=name, arguments=json.dumps(args)))


class FakeLLM:
    """Replays scripted assistant turns: a list of (tool, args) calls, or plain text."""

    def __init__(self, turns):
        self.turns, self.seen = list(turns), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.seen.append({**kw, "messages": list(kw["messages"])})
        turn = self.turns.pop(0)
        if isinstance(turn, list):
            msg = SimpleNamespace(content="", tool_calls=[tool_call(n, a, f"c{i}") for i, (n, a) in enumerate(turn)])
        else:
            msg = SimpleNamespace(content=turn, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


@pytest.fixture
def fake_llm():
    return FakeLLM
