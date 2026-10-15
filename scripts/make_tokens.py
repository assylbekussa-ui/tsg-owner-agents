"""Печатает случайные токены для MCP-сервера. Скопируйте строки в переменные Railway.

    python scripts/make_tokens.py
"""
import secrets

for role in ("TEAMLEAD", "PRODUCTION", "ACCOUNT", "ACCOUNTANT"):
    print(f"MCP_TOKEN_{role}={secrets.token_urlsafe(32)}")
