import datetime
import json
import os
import unittest
from typing import List, Optional, Tuple
from unittest.mock import ANY, MagicMock, Mock, patch

import httpx
import pytest

import litellm
from tests.test_litellm.proxy.auth.license_test_helpers import install_entitlements, licensed_entitlements


@pytest.mark.asyncio
async def test_construct_request_headers_project_id_from_env(monkeypatch):
    """Test that construct_request_headers uses GCS_PUBSUB_PROJECT_ID environment variable."""
    from litellm.integrations.gcs_pubsub.pub_sub import GcsPubSubLogger

    # Set up test environment variable
    test_project_id = "test-project-123"
    monkeypatch.setenv("GCS_PUBSUB_PROJECT_ID", test_project_id)
    install_entitlements(monkeypatch, licensed_entitlements(features=("logging_integrations",)))

    try:
        # Create handler with no project_id
        handler = GcsPubSubLogger(
            topic_id="test-topic", credentials_path="test-path.json"
        )

        # Mock the Vertex AI auth calls
        mock_auth_header = "mock-auth-header"
        mock_token = "mock-token"

        with patch(
            "litellm.vertex_chat_completion._ensure_access_token_async"
        ) as mock_ensure_token:
            mock_ensure_token.return_value = (mock_auth_header, test_project_id)

            with patch(
                "litellm.vertex_chat_completion._get_token_and_url"
            ) as mock_get_token:
                mock_get_token.return_value = (mock_token, "mock-url")

                # Call construct_request_headers
                headers = await handler.construct_request_headers()

                # Verify headers
                assert headers == {
                    "Authorization": f"Bearer {mock_token}",
                    "Content-Type": "application/json",
                }

                # Verify _ensure_access_token_async was called with correct project_id
                mock_ensure_token.assert_called_once_with(
                    credentials="test-path.json",
                    project_id=test_project_id,
                    custom_llm_provider="vertex_ai",
                )
    finally:
        # Clean up environment variable
        del os.environ["GCS_PUBSUB_PROJECT_ID"]


@pytest.mark.parametrize("features", [(), ("sso",)])
def test_pubsub_logger_requires_the_logging_integrations_feature(monkeypatch, features):
    from fastapi import HTTPException

    from litellm.integrations.gcs_pubsub.pub_sub import GcsPubSubLogger
    from tests.test_litellm.proxy.auth.license_test_helpers import unlicensed_entitlements

    install_entitlements(monkeypatch, licensed_entitlements(features=features) if features else unlicensed_entitlements())

    with pytest.raises(HTTPException) as exc_info:
        GcsPubSubLogger(project_id="p", topic_id="t", credentials_path="c.json")

    assert exc_info.value.status_code == 403