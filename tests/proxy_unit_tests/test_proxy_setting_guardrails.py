import json
import os
from unittest import mock

from dotenv import load_dotenv

load_dotenv()
import asyncio
import io

import openai
import pytest
from fastapi import Response
from fastapi.testclient import TestClient

import litellm
from litellm.proxy.proxy_server import (  # Replace with the actual module where your FastAPI router is defined
    initialize,
    router,
    save_worker_config,
)


@pytest.fixture
def client(monkeypatch):
    from tests.test_litellm.proxy.auth.license_test_helpers import install_entitlements, licensed_entitlements

    install_entitlements(monkeypatch, licensed_entitlements(features=("guardrails",)))
    filepath = os.path.dirname(os.path.abspath(__file__))
    config_fp = f"{filepath}/test_configs/test_guardrails_config.yaml"
    asyncio.run(initialize(config=config_fp))
    from litellm.proxy.proxy_server import app

    return TestClient(app)


# raise openai.AuthenticationError
def test_active_callbacks(client):
    response = client.get("/active/callbacks")

    print("response", response)
    print("response.text", response.text)
    print("response.status_code", response.status_code)

    json_response = response.json()
    print(f"json_response={json_response}")
    _active_callbacks = json_response["litellm.callbacks"]

    expected_callback_names = [
        "lakeraAI_Moderation",
        "_OPTIONAL_PromptInjectionDetectio",
        "HideSecretsGuardrail",
    ]

    for callback_name in expected_callback_names:
        # check if any of the callbacks have callback_name as a substring
        found_match = False
        for callback in _active_callbacks:
            if callback_name in callback:
                found_match = True
                break
        assert (
            found_match is True
        ), f"{callback_name} not found in _active_callbacks={_active_callbacks}"

    assert not any(
        "OpenAIModerationGuardrail" in callback for callback in _active_callbacks
    ), f"OpenAIModerationGuardrail should not be in _active_callbacks={_active_callbacks}"
