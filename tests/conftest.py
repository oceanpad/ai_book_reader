import os

import pytest


@pytest.fixture(autouse=True)
def isolate_reader_environment(monkeypatch):
    for name in os.environ:
        if name.startswith("READER_") or name in {"OPENAI_API_KEY", "OPENROUTER_API_KEY"}:
            monkeypatch.delenv(name)
