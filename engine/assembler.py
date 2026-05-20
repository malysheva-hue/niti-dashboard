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
    if rid in ("MARGIN_ANOMALY", "MARGIN_BELOW_FLOOR"):
        return phrases.render_phrase("problem_one_liner_margin",
                                     value=round(m.get("margin") or 0, 1),
                                     min=norms.get("margin_min") or 20,
                                     role=role or "—")
    if rid == "MARGIN_ABOVE_CEILING":
        return phrases.render_phrase("growth_high_margin_low_ad",
                                     sku=finding.sku_code,
                                     manager=finding.manager,
                                     margin=round(m.get("margin") or 0, 1))
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
# STARS_STATUS — информационные карточки звёзд (всегда 4)
# ============================================================
def _star_tempo(cur_revenue, prev_revenue, target_growth_pct, month_str):
    """Темп звезды: cur / (prev × (1 + target_growth) × dop/dim) × 100."""
    if not prev_revenue or cur_revenue is None or month_str is None:
        return None
    target = prev_revenue * (1 + (target_growth_pct or 0) / 100)
    if target <= 0:
        return None
    from datetime import date as _date
    try:
        y, m = (int(x) for x in month_str.split("-"))
    except (ValueError, TypeError):
        return None
    dim = (_date(y if m < 12 else y + 1, (m % 12) + 1, 1) - _date(y, m, 1)).days
    today = _date.today()
    if today.year == y and today.month == m:
        dop = today.day
    elif today > _date(y, m, dim):
        dop = dim
    else:
        dop = 0
    if dop == 0:
        return None
    expected = target * dop / dim
    if expected == 0:
        return None
    return cur_revenue / expected * 100


def _month_days(month_str):
    """Возвращает (days_in_month, days_passed) для месяца, иначе (None, None)."""
    if not month_str:
        return (None, None)
    from datetime import date as _date
    try:
        y, m = (int(x) for x in month_str.split("-"))
    except (ValueError, TypeError):
        return (None, None)
    dim = (_date(y if m < 12 else y + 1, (m % 12) + 1, 1) - _date(y, m, 1)).days
    today = _date.today()
    if today.year == y and today.month == m:
        dop = today.day
    elif today > _date(y, m, dim):
        dop = dim
    else:
        dop = 0
    return (dim, dop)


def _fmt_pct_delta(curr, prev, current_month=None):
    """MoM-дельта с pro-rata коррекцией для неполного месяца.

    Если current_month неполный (dop<dim), сравниваем не «весь curr vs весь prev»,
    а projected_full_month = curr × dim/dop, чтобы не показывать ложное падение
    в начале месяца. К строке добавляется пометка «(прогноз на 31/31)».
    """
    if curr is None or prev is None or prev == 0:
        return None
    dim, dop = _month_days(current_month) if current_month else (None, None)
    if dim and dop and dop < dim:
        projected = curr * dim / dop
        pct = (projected - prev) / abs(prev) * 100
        sign = "+" if pct >= 0 else ""
        return f"{sign}{pct:.0f}% (прогноз на {dim}/{dim} дн)"
    pct = (curr - prev) / abs(prev) * 100
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.0f}%"


def _fmt_pp_delta(curr, prev):
    """Pp-дельта для маржи (не зависит от длины месяца — это уже %)."""
    if curr is None or prev is None:
        return None
    delta = curr - prev
    sign = "+" if delta >= 0 else ""
    return f"{sign}{delta:.1f}пп"


def _classify_star(cur_month: dict, tempo: float | None, role: str):
    """Возвращает (status, reason)."""
    margin = cur_month.get("margin") if cur_month else None
    drr = cur_month.get("drr") if cur_month else None
    stock = cur_month.get("stock") if cur_month else None
    orders = (cur_month or {}).get("orders") or 0

    # red checks
    if stock == 0 and orders > 0:
        return "red", "OOS при активных заказах"
    if margin is not None and margin < 0:
        return "red", f"маржа {margin:.1f}% — убыточный"
    if tempo is not None and tempo < 70:
        return "red", f"темп {tempo:.0f}% — отстаёт"

    # role norms
    norms = ctx.ROLE_NORMS.get(role, {})
    m_min = norms.get("margin_min")
    drr_max = norms.get("drr_max")

    margin_ok = m_min is None or (margin is not None and margin >= m_min)
    drr_ok = drr_max is None or (drr is not None and drr <= drr_max)
    tempo_ok = tempo is None or tempo >= 85

    # yellow: проседает одна метрика
    if not margin_ok and margin is not None:
        return "yellow", f"маржа {margin:.1f}% (норма ≥{m_min}%)"
    if not drr_ok and drr is not None:
        return "yellow", f"ДРР {drr:.1f}% (норма ≤{drr_max}%)"
    if tempo is not None and tempo < 85:
        return "yellow", f"темп {tempo:.0f}% — на грани"

    if tempo is None:
        return "green", "в норме (темп не вычислим)"
    return "green", f"темп {tempo:.0f}% — норма"


def render_stars_status(data: dict) -> list[dict]:
    current = (data.get("meta") or {}).get("current_month")
    months_all = (data.get("meta") or {}).get("available_months") or []
    prev = None
    if current in months_all:
        idx = months_all.index(current)
        if idx > 0:
            prev = months_all[idx - 1]

    cur_stars = (data.get("stars_monthly") or {}).get(current, {}) or {}
    prv_stars = (data.get("stars_monthly") or {}).get(prev, {}) or {}
    months_data = data.get("_months_data") or {}
    cur_idx = {r["code"]: r for r in months_data.get(current, [])}
    prv_idx = {r["code"]: r for r in months_data.get(prev, [])}
    sku_history = data.get("sku_history") or {}

    out = []
    for s in ctx.STARS:
        code = s["code"]
        cur = cur_stars.get(code, {}) or {}
        prv = prv_stars.get(code, {}) or {}
        cur_full = cur_idx.get(code, {})
        prv_full = prv_idx.get(code, {})

        # days_to_oos: stock / velocity_7d (из истории)
        velocity_7d = None
        history = sku_history.get(code) or {}
        orders_arr = history.get("orders") or []
        last7 = [o for o in orders_arr[-7:] if o is not None]
        if last7:
            velocity_7d = sum(last7) / len(last7)
        days_to_oos = None
        stock_now = cur.get("stock")
        if stock_now is not None and velocity_7d and velocity_7d > 0:
            days_to_oos = stock_now / velocity_7d

        # темп
        tempo = _star_tempo(cur.get("revenue"), prv.get("revenue"),
                            s.get("target_growth_pct"), current)
        # status
        status, reason = _classify_star(cur, tempo, s.get("role") or "Локомотив")

        out.append({
            "sku": code,
            "name": s["name"],
            "manager": s["manager"],
            "status": status,
            "status_reason": reason,
            "metrics": {
                "revenue_month": int(cur.get("revenue") or 0) if cur.get("revenue") is not None else None,
                "margin_pct": round(cur.get("margin"), 1) if cur.get("margin") is not None else None,
                "drr_pct": round(cur.get("drr"), 2) if cur.get("drr") is not None else None,
                "tacos_pct": round(cur.get("tacos"), 2) if cur.get("tacos") is not None else None,
                "stock": int(cur.get("stock")) if cur.get("stock") is not None else None,
                "days_to_oos": round(days_to_oos, 1) if days_to_oos is not None else None,
                "turnover_days": round(cur.get("turnover")) if cur.get("turnover") is not None else None,
                "tempo_pct": round(tempo) if tempo is not None else None,
            },
            "delta_vs_prev_month": {
                "revenue": _fmt_pct_delta(cur.get("revenue"), prv.get("revenue"), current),
                "margin": _fmt_pp_delta(cur.get("margin"), prv.get("margin")),
                "orders": _fmt_pct_delta(cur_full.get("orders"), prv_full.get("orders"), current),
            },
        })
    return out


# ============================================================
# LOCO_RISK_STATUS — карточки топ-локомотивов (всегда 4)
# ============================================================
def _classify_loco(days_to_oos, stock):
    if stock == 0:
        return "red", "OOS — продаём с прерываниями"
    if days_to_oos is None:
        return "yellow", "нет данных по скорости продаж"
    if days_to_oos < 7:
        return "red", f"OOS через {days_to_oos:.1f} дн — критично"
    if days_to_oos <= 14:
        return "yellow", f"OOS через {days_to_oos:.1f} дн — следить"
    return "green", f"OOS через {days_to_oos:.1f} дн — норма"


def render_loco_risk_status(data: dict) -> list[dict]:
    oos_days = data.get("oos_days") or {}
    out = []
    for l in ctx.LOCO_RISK_LIST:
        code = l["code"]
        o = oos_days.get(code, {}) or {}
        stock = o.get("stock")
        velocity = o.get("avg7")
        days_left = o.get("days_left")
        status, reason = _classify_loco(days_left, stock)
        out.append({
            "sku": code,
            "name": l["name"],
            "manager": l["manager"],
            "stock": int(stock) if stock is not None else None,
            "daily_velocity_7d": velocity,
            "days_to_oos": days_left,
            "status": status,
            "status_reason": reason,
        })
    return out


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
        "stars_status": render_stars_status(data),
        "loco_risk_status": render_loco_risk_status(data),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "engine": "niti_v1_rules",
        "engine_meta": {
            "version": "1.2.0",
            "rules_active": [r for r in (engine_meta_extra or {}).get("rules_active", [])],
            **(engine_meta_extra or {}),
        },
    }
    return out
