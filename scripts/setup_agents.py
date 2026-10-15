"""Создать или обновить агентов вручную (обычно не нужно: сервис tsg-bitrix делает это сам при старте).

    ANTHROPIC_API_KEY=... MCP_BASE_URL=https://<домен tsg-bitrix> python scripts/setup_agents.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic  # noqa: E402

from tsg_bitrix.provision import base_url, provision  # noqa: E402

if __name__ == "__main__":
    ids = provision(anthropic.Anthropic(), base_url())
    print("\nГотово:", ids)
