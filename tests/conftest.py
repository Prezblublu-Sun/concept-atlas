"""Suite-wide guard: no test may reach a model provider. ATLAS_LLM_NO_NETWORK=1 makes AnthropicClient and
DeepSeekClient raise NetworkForbidden before any HTTP call (atlas/llm/base.py); tests that exercise request
construction inject a fake SDK object and lift the variable with monkeypatch for that test only."""
from __future__ import annotations

import os

from atlas.llm.base import NO_NETWORK_ENV

os.environ[NO_NETWORK_ENV] = "1"
