import os

import pytest


@pytest.fixture(autouse=True)
def isolate_demo_settings(monkeypatch):
    """The live integration opt-in must not reconfigure local app tests."""
    for key in tuple(os.environ):
        if key.startswith("SWITCHBOARD_") and key != "SWITCHBOARD_TEST_FHIR_URL":
            monkeypatch.delenv(key)
