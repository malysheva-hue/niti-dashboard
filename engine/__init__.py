"""Niti Engine V1 — rule-based analytics с заделом под LLM.

Архитектура трёхслойная:
  Layer 1 (Knowledge Base): operators.py, contexts.py, phrases.py, actions.py
  Layer 2 (Pattern Engine):  rule_loader.py, engine.py
  Layer 3 (Assembler):       assembler.py

Контракт ai_summary.json совместим с index.html.
При замене на LLM переписывается только assembler.py.
"""

__version__ = "1.0.0"
