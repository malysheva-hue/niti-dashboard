#!/usr/bin/env python3
"""update.py — сборка дашборда + AI-сводка.

V2: вместо вызова Gemini используется локальный rule-based движок
(engine/). Контракт ai_summary.json совместим с index.html.

Pipeline:
  1. python3 build.py — свежие data.json / data/processed/months/
  2. PatternEngine.analyze() — поиск аномалий по 10 YAML-правилам
  3. Assembler.build_summary() — рендер ai_summary.json
  4. python3 build.py — встраивание сводки в index.html

FUTURE: replace assembler with LLM call (см. блок ниже).
"""
from __future__ import annotations

import json
import random
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
DATA_DIR = ROOT / "data"
PROCESSED_DIR = DATA_DIR / "processed"
AI_SUMMARY_FILE = DATA_DIR / "ai_summary.json"
DATA_JSON = ROOT / "data.json"


def load_env():
    if not ENV_FILE.exists():
        return {}
    out = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def run_build():
    print("→ python3 build.py")
    r = subprocess.run([sys.executable, "build.py"], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("  ✗ build.py упал:")
        print(r.stdout)
        print(r.stderr)
        sys.exit(1)
    for l in r.stdout.strip().splitlines()[-4:]:
        print(f"    {l}")


def load_months_data() -> dict:
    """Подгружает все месячные json (для PatternEngine это нужно для срезов)."""
    out = {}
    months_dir = PROCESSED_DIR / "months"
    if not months_dir.exists():
        return out
    for p in sorted(months_dir.glob("*.json")):
        month = p.stem
        try:
            out[month] = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  ⚠ не удалось прочитать {p.name}: {e}")
    return out


def main():
    print("=" * 60)
    print("UPDATE.PY — сборка дашборда + Niti Engine V1")
    print("=" * 60)

    env = load_env()  # noqa — пока не используется, оставлено под FUTURE/LLM
    _ = env

    # --------------------------------------------------------
    # [1/3] первый прогон build.py
    # --------------------------------------------------------
    print("\n[1/3] build.py для свежего data.json")
    run_build()

    # --------------------------------------------------------
    # [2/3] PatternEngine + Assembler
    # --------------------------------------------------------
    print("\n[2/3] Niti Engine V1 — анализ")
    data = json.loads(DATA_JSON.read_text(encoding="utf-8"))
    months = load_months_data()
    data["_months_data"] = months
    current_month = data.get("meta", {}).get("current_month")
    print(f"  current_month: {current_month}")

    # импорт здесь — после первого build.py, чтобы dev-окружение было готово
    from engine.engine import PatternEngine, assemble_sku_records
    from engine.assembler import build_summary
    from engine import phrases

    # Идемпотентность фраз — seed по дате
    random.seed(date.today().isoformat())
    phrases.reset_usage()

    sku_records = assemble_sku_records(data, current_month)
    print(f"  SKU records: {len(sku_records)}")

    engine_obj = PatternEngine(ROOT / "engine" / "rules")
    print(f"  rules loaded: {len(engine_obj.rules)}")

    raw_findings = engine_obj.analyze(sku_records)
    print(f"  raw findings: {len(raw_findings)}")

    deduped = engine_obj.dedupe(raw_findings)
    print(f"  after dedup: {len(deduped)}")

    # v2.3 БЛОК 1: классификация хорошо/плохо ДО приоритизации, чтобы limit считался корректно
    from engine.assembler import filter_findings_classification
    deduped = filter_findings_classification(deduped)
    reclassified = sum(1 for f in deduped if any("reclassified" in str(d) for d in (f.diagnostics or [])))
    print(f"  reclassified opp→risk: {reclassified}")

    # v2.3 БЛОК 6: 10-15 задач на менеджера, общий лимит 45 (3 менеджера × 15)
    final = engine_obj.prioritize(deduped, total_limit=45,
                                  per_manager_limit=15, opportunity_reserve=5,
                                  force_star_inject=True)
    print(f"  final: {len(final)}")

    # ---- диагностика правил с 0 findings ----
    print()
    print("  Findings по правилам:")
    for rule_id, cnt in engine_obj.rule_match_counts.items():
        flag = "[WARN] " if cnt == 0 else "        "
        print(f"    {flag}{rule_id:30s} {cnt:>4} matches"
              + (" — порог слишком жёсткий?" if cnt == 0 else ""))

    print()
    print("  Распределение финальных по менеджерам:")
    by_mgr = {}
    for f in final:
        by_mgr[f.manager] = by_mgr.get(f.manager, 0) + 1
    for m, c in sorted(by_mgr.items()):
        print(f"    {m:15s} {c}")

    print()
    print("  Распределение финальных по priority/task_type:")
    by_pp = {}
    for f in final:
        key = f"{f.priority}/{f.task_type}"
        by_pp[key] = by_pp.get(key, 0) + 1
    for k, v in sorted(by_pp.items()):
        print(f"    {k:20s} {v}")

    print()
    print("  Top-10:")
    for f in final:
        print(f"    {f.severity_score:>3} | {f.rule_id:24s} | {f.sku_code:12s} | {f.manager:10s} | {f.priority:6s} | {f.task_type}")

    # ---- assembler ----
    engine_meta = {
        "rules_matched": sum(engine_obj.rule_match_counts.values()),
        "findings_after_dedup": len(deduped),
        "findings_returned": len(final),
        "rules_active": [r.id for r in engine_obj.rules],
        "rule_match_counts": dict(engine_obj.rule_match_counts),
    }

    summary = build_summary(final, data, engine_meta_extra=engine_meta)

    AI_SUMMARY_FILE.parent.mkdir(parents=True, exist_ok=True)
    AI_SUMMARY_FILE.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n✓ Записал {AI_SUMMARY_FILE.relative_to(ROOT)} "
          f"({AI_SUMMARY_FILE.stat().st_size} байт)")

    # ---- диагностика фраз ----
    usage = phrases.get_usage_stats()
    print()
    print("  Использование фраз:")
    print(f"    всего situation_id использовано: {usage['total_situations_used']}")
    print(f"    всего вызовов render_phrase: {usage['total_calls']}")
    print(f"    уникальных вариантов сыграло: {usage['variants_used']}")
    avg_variants = (usage["variants_used"] / max(1, usage["total_situations_used"]))
    print(f"    среднее вариантов на ситуацию: {avg_variants:.2f}")

    # --------------------------------------------------------
    # [3/3] второй прогон build.py — встроит свежий ai_summary в HTML
    # --------------------------------------------------------
    print("\n[3/3] build.py для встраивания AI-сводки в HTML")
    run_build()

    print("\n✓ Готово. Открывай index.html.")


# ============================================================
# FUTURE: replace assembler with LLM call
# ============================================================
# Когда подключим LLM (Anthropic / OpenAI / YandexGPT), переписываем
# engine/assembler.py — он принимает на вход тот же набор данных, но
# делает один HTTP-запрос вместо локального рендера. PatternEngine
# остаётся как этап предобработки (выбор топ-10 кандидатов + контекст),
# чтобы не отправлять в LLM весь портфель.
#
# Пример точки переключения:
#
#   from engine.engine import PatternEngine
#   from engine.assembler_llm import build_summary_llm   # будущий модуль
#
#   findings = PatternEngine(...).analyze(...)
#   summary  = build_summary_llm(findings, data, api_key=env["..."])
#
# Старый код Gemini-call (до v2 движка) находится в истории git:
#   git show 9e42bbd:update.py
# ============================================================


if __name__ == "__main__":
    main()
