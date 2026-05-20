"""Загрузчик YAML-правил.

Каждый YAML-файл описывает одно правило. Структура:
  id, description, severity_base, applies_to, triggers,
  diagnostics, action_lookup, escalation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Rule:
    id: str
    description: str
    severity_base: int
    applies_to: dict
    triggers: list[dict]
    diagnostics: list[dict] = field(default_factory=list)
    action_lookup: dict = field(default_factory=dict)
    escalation: dict = field(default_factory=lambda: {"red_threshold": 85, "yellow_threshold": 60})
    raw: dict = field(default_factory=dict)


def load_rules(rules_dir: str | Path) -> list[Rule]:
    """Читает все *.yaml из директории и возвращает список Rule.

    Файлы загружаются в алфавитном порядке (01_*, 02_* …).
    """
    rules = []
    rules_path = Path(rules_dir)
    for yaml_path in sorted(rules_path.glob("*.yaml")):
        with open(yaml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not data or "id" not in data:
            continue
        rules.append(Rule(
            id=data["id"],
            description=data.get("description", ""),
            severity_base=int(data.get("severity_base", 50)),
            applies_to=data.get("applies_to", {}) or {},
            triggers=data.get("triggers", []) or [],
            diagnostics=data.get("diagnostics", []) or [],
            action_lookup=data.get("action_lookup", {}) or {},
            escalation=data.get("escalation", {}) or {"red_threshold": 85, "yellow_threshold": 60},
            raw=data,
        ))
    return rules
