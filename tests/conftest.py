import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture
def summary():
    return load("summary")


@pytest.fixture
def history():
    return {i: load(f"history_{i}") for i in ("1m", "1h", "1d")}
