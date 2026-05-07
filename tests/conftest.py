"""Shared pytest fixtures."""
from __future__ import annotations

import pytest


@pytest.fixture
def mock_chat_response() -> dict:
    """Minimal OpenAI-compatible chat completion payload."""
    return {
        "id": "stub",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "stubbed response"},
                "finish_reason": "stop",
            }
        ],
    }
