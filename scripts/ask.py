"""Задать вопрос Тимлиду из терминала.

    python scripts/ask.py "Что горит по Атакенту?"
    python scripts/ask.py --summary          # вечерняя сводка за сегодня
    python scripts/ask.py --new "…"          # начать новую сессию
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from owner_bot.teamlead import SUMMARY_PROMPT, Teamlead  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("text", nargs="?", default="")
    p.add_argument("--summary", action="store_true")
    p.add_argument("--new", action="store_true")
    a = p.parse_args()
    text = SUMMARY_PROMPT if a.summary else a.text
    if not text:
        p.error("нужен вопрос или --summary")
    lead = Teamlead()
    if a.new:
        lead.reset("terminal")
    print(lead.ask("terminal", text, on_progress=lambda m: print(f"  → {m}", file=sys.stderr)))


if __name__ == "__main__":
    main()
