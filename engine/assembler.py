"""Assembler — собирает финальный ai_summary.json совместимый с index.html.

Контракт результата:
  company_summary (str), manager_blocks (dict), tasks (list),
  generated_at, engine, engine_meta.

При замене на LLM переписывается только этот файл.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from . import contexts as ctx
from . import phrases
from . import actions
from .engine import Finding


# ============================================================
# ОДНА ЗАДАЧА (TASK)
# ============================================================
def _problem_text(finding: Finding) -> str:
    """Короткая строка проблемы для карточки задачи."""
    sku = finding.sku_data
    m = sku.get("month") or {}
    role = ctx.role_of(sku.get("group", "") or "")
    norms = ctx.ROLE_NORMS.get(role, {})

    rid = finding.rule_id
    if rid == "ORDER_DROP_DETAILED":
        td = finding.trigger_details.get("orders") or {}
        return phrases.render_phrase("problem_one_liner_order_drop",
                                     pct=td.get("pct", "—"), window=td.get("window", 7))
    if rid == "MARGIN_ANOMALY":
        return phrases.render_phrase("problem_one_liner_margin",
                                     value=round(m.get("margin") or 0, 1),
                                     min=norms.get("margin_min") or 20,
                                     role=role or "—")
    if rid == "DRR_RUNAWAY":
        return phrases.render_phrase("problem_one_liner_drr",
                                     value=round(m.get("drr") or 0, 1),
                                     max=norms.get("drr_max") or 10)
    if rid in ("OOS_IMMINENT", "LOCO_RISK_OOS"):
        days = finding.diagnostics_details.get("oos_risk", {}).get("days") \
            or finding.diagnostics_details.get("critical_oos", {}).get("days") \
            or finding.trigger_details.get("stock", {}).get("days")
        return phrases.render_phrase("problem_one_liner_oos", days=round(days or 0, 1))
    if rid == "GROWTH_OPPORTUNITY":
        return phrases.render_phrase("problem_one_liner_growth",
                                     margin=round(m.get("margin") or 0, 1),
                                     pct=finding.trigger_details.get("orders", {}).get("pct", 30))
    if rid == "FROZEN_CAPITAL":
        return phrases.render_phrase("problem_one_liner_frozen",
                                     days=round(m.get("turnover") or 0),
                                     frozen=_fmt_rub(m.get("frozen") or 0))
    if rid == "STAR_UNDERPERFORM":
        note = next((s["note"] for s in ctx.STARS if s["code"] == finding.sku_code), "")
        return phrases.render_phrase("star_underperform",
                                     sku=finding.sku_code, manager=finding.manager, note=note)
    if rid == "PRICE_COMPETITOR":
        td = finding.trigger_details.get("price") or {}
        return phrases.render_phrase("price_jump",
                                     pct=td.get("jump_pct", "—"),
                                     window=7,
                                     min=td.get("min", "—"),
                                     max=td.get("max", "—"))
    if rid == "SEASONAL_SIGNAL":
        return phrases.render_phrase("seasonal_peak_strong" if "peak_strong" in finding.diagnostics else "seasonal_peak_weak",
                                     category=sku.get("category", "—"),
                                     pct=finding.trigger_details.get("orders", {}).get("pct", "—"))
    return f"{finding.rule_id}"


def _fmt_rub(v) -> str:
    if v is None:
        return "—"
    v = float(v)
    if abs(v) >= 1e6:
        return f"{v/1e6:.1f} млн"
    if abs(v) >= 1e3:
        return f"{v/1e3:.0f} тыс"
    return f"{int(v)}"


def render_task(finding: Finding) -> dict:
    """Один объект task для ai_summary.json."""
    sku = finding.sku_data
    problem = _problem_text(finding)
    action_short = actions.render_action(finding.action_key, sku, finding)
    # action_extended — для модалки. Можем сделать чуть подробнее, но
    # пока берём тот же action_short с добавлением контекста.
    action_ext = _build_action_extended(finding, action_short)

    return {
        "sku": finding.sku_code,
        "manager": finding.manager,
        "priority": finding.priority,
        "task_type": finding.task_type,
        "problem": problem,
        "action": action_short,
        "action_extended": action_ext,
        "diagnostics": finding.diagnostics,
        "severity_score": finding.severity_score,
        "rule_id": finding.rule_id,
    }


def _build_action_extended(finding: Finding, action_short: str) -> str:
    """Развёрнутое объяснение для модалки SKU."""
    sku = finding.sku_data
    m = sku.get("month") or {}
    h = sku.get("history") or {}
    orders = h.get("orders") or []
    last7 = [o for o in orders[-7:] if o is not None]
    prev7 = [o for o in orders[-14:-7] if o is not None]
    avg_last = sum(last7) / len(last7) if last7 else 0
    avg_prev = sum(prev7) / len(prev7) if prev7 else 0

    parts = [action_short, ""]
    parts.append("Контекст:")
    parts.append(f"  • Заказы за 30 дней: {sum(o or 0 for o in orders):.0f} шт.")
    if last7 and prev7:
        delta_pct = (avg_last - avg_prev) / max(1, avg_prev) * 100
        sign = "+" if delta_pct >= 0 else ""
        parts.append(f"  • Последние 7 дней против предыдущих: {avg_last:.1f}/день vs {avg_prev:.1f}/день ({sign}{delta_pct:.0f}%).")
    if m.get("margin") is not None:
        parts.append(f"  • Маржа за месяц: {m['margin']:.1f}%.")
    if m.get("drr") is not None:
        parts.append(f"  • ДРР за месяц: {m['drr']:.2f}%.")
    if m.get("stock") is not None:
        parts.append(f"  • Остаток: {int(m['stock'])} шт.")
    if finding.diagnostics:
        parts.append(f"  • Сигналы: {', '.join(finding.diagnostics)}.")
    return "\n".join(parts)


# ============================================================
# БЛОК COMPANY_SUMMARY (5-7 строк)
# ============================================================
def render_company_summary(findings: list[Finding], data: dict) -> str:
    """Формальный тон. 5-7 строк. Без приветствий, без эмодзи."""
    monthly = data.get("monthly_totals") or []
    current = (data.get("meta") or {}).get("current_month")
    cur = next((r for r in monthly if r.get("month") == current), {}) or {}

    # темп месяца — считаем локально
    pace = None
    plan_company = _get_plan_company(current)
    if plan_company and cur.get("revenue"):
        from datetime import date
        y, m_ = (int(x) for x in current.split("-"))
        dim = (date(y if m_ < 12 else y + 1, (m_ % 12) + 1, 1) - date(y, m_, 1)).days
        today = date.today()
        if today.year == y and today.month == m_:
            dop = today.day
        elif today > date(y, m_, dim):
            dop = dim
        else:
            dop = 0
        if dop > 0:
            expected = plan_company * dop / dim
            pace = cur["revenue"] / expected * 100 if expected else None

    margin_target = 28.5
    drr_target = 5.5

    red_n = sum(1 for f in findings if f.priority == "red")
    yellow_n = sum(1 for f in findings if f.priority == "yellow" and f.task_type == "risk")
    opp_n = sum(1 for f in findings if f.task_type == "opportunity")

    lines = []

    # 1. Opening
    if red_n > 0:
        lines.append(phrases.render_phrase("company_opening_alert"))
    elif not findings:
        lines.append(phrases.render_phrase("company_opening_calm"))
    else:
        lines.append(phrases.render_phrase("company_opening_neutral"))

    # 2. Темп месяца
    from datetime import date as _date
    y, m_ = (int(x) for x in current.split("-")) if current else (None, None)
    dim = 31
    dop = 0
    if y and m_:
        dim = (_date(y if m_ < 12 else y + 1, (m_ % 12) + 1, 1) - _date(y, m_, 1)).days
        today = _date.today()
        if today.year == y and today.month == m_:
            dop = today.day
        elif today > _date(y, m_, dim):
            dop = dim
    if pace is not None:
        if pace >= 100:
            key = "company_pace_strong"
        elif pace >= 85:
            key = "company_pace_normal"
        else:
            key = "company_pace_weak"
        lines.append(phrases.render_phrase(key, pace=round(pace), days_passed=dop, days_in_month=dim))

    # 3. Маржа
    if cur.get("margin") is not None:
        gap = margin_target - cur["margin"]
        if cur["margin"] >= margin_target - 1:
            lines.append(phrases.render_phrase("company_margin_ok", value=round(cur["margin"], 1), target=margin_target))
        else:
            lines.append(phrases.render_phrase("company_margin_below",
                                               value=round(cur["margin"], 1),
                                               target=margin_target,
                                               gap=round(gap, 1)))

    # 4. ДРР
    if cur.get("drr") is not None:
        if cur["drr"] <= drr_target + 0.3:
            lines.append(phrases.render_phrase("company_drr_ok", value=round(cur["drr"], 2), target=drr_target))
        else:
            lines.append(phrases.render_phrase("company_drr_high", value=round(cur["drr"], 2), target=drr_target))

    # 5. Состав задач
    if not findings:
        lines.append(phrases.render_phrase("tasks_summary_clean"))
    else:
        lines.append(phrases.render_phrase("tasks_summary",
                                           total=len(findings),
                                           red=red_n, yellow=yellow_n, opp=opp_n))

    return "\n".join(lines)


def _get_plan_company(month: str) -> float | None:
    PLANS = {
        "2026-05": 32760000, "2026-06": 37000000, "2026-07": 47780000, "2026-08": 42000000,
    }
    return PLANS.get(month)


# ============================================================
# БЛОК MANAGER (3-5 строк на менеджера)
# ============================================================
PLANS_BY_MGR = {
    "2026-05": {"Виктория": 7830000, "Настя": 15700000, "Владимир": 9230000},
    "2026-06": {"Виктория": 8850000, "Настя": 17740000, "Владимир": 10410000},
    "2026-07": {"Виктория": 11420000, "Настя": 22910000, "Владимир": 13450000},
    "2026-08": {"Виктория": 10040000, "Настя": 20140000, "Владимир": 11820000},
}


def render_manager_block(manager: str, findings: list[Finding], data: dict) -> str:
    current = (data.get("meta") or {}).get("current_month")
    mgr_month = ((data.get("manager_monthly") or {}).get(current) or {}).get(manager) or {}
    plan = PLANS_BY_MGR.get(current, {}).get(manager)

    pace = None
    if plan and mgr_month.get("revenue"):
        from datetime import date as _date
        y, m_ = (int(x) for x in current.split("-"))
        dim = (_date(y if m_ < 12 else y + 1, (m_ % 12) + 1, 1) - _date(y, m_, 1)).days
        today = _date.today()
        if today.year == y and today.month == m_:
            dop = today.day
        elif today > _date(y, m_, dim):
            dop = dim
        else:
            dop = 0
        if dop > 0:
            expected = plan * dop / dim
            pace = mgr_month["revenue"] / expected * 100 if expected else None

    mgr_findings = [f for f in findings if f.manager == manager]
    lines = []

    if pace is not None:
        if pace >= 100:
            lines.append(phrases.render_phrase("manager_summary_strong", manager=manager, pace=round(pace)))
        elif pace >= 85:
            lines.append(phrases.render_phrase("manager_summary_normal", manager=manager, pace=round(pace)))
        else:
            lines.append(phrases.render_phrase("manager_summary_weak", manager=manager, pace=round(pace)))

    if not mgr_findings:
        lines.append(phrases.render_phrase("manager_no_issues", manager=manager))
    else:
        lines.append(phrases.render_phrase("manager_focus",
                                           manager=manager, focus_count=len(mgr_findings)))
        # перечисление SKU в фокусе
        codes = ", ".join(f.sku_code for f in mgr_findings[:5])
        lines.append(f"В фокусе: {codes}.")

    return "\n".join(lines)


# ============================================================
# СБОРКА ИТОГА
# ============================================================
def build_summary(findings: list[Finding], data: dict, engine_meta_extra: dict = None) -> dict:
    """Главная функция — возвращает dict, готовый к сериализации в ai_summary.json."""
    tasks = [render_task(f) for f in findings]

    manager_blocks = {
        mgr: render_manager_block(mgr, findings, data) for mgr in ctx.ACTIVE_MANAGERS
    }

    company_summary = render_company_summary(findings, data)

    out = {
        "company_summary": company_summary,
        "manager_blocks": manager_blocks,
        "tasks": tasks,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "engine": "niti_v1_rules",
        "engine_meta": {
            "version": "1.0.0",
            "rules_active": [r for r in (engine_meta_extra or {}).get("rules_active", [])],
            **(engine_meta_extra or {}),
        },
    }
    return out
