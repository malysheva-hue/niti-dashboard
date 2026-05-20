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
                                     max=norms.get("drr_max") or 10,
                                     role=role or "—")
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
    if rid == "PRICE_DROP_NO_EFFECT":
        return "Снизили цену за 7 дней — заказы не выросли, эффекта пока нет."
    if rid == "SPP_DROP":
        return "СПП от ВБ просела за 7 дней, конверсия упала. Алгоритм остудил карточку."
    if rid == "STAGNATION":
        turn = round(m.get("turnover") or 0)
        return f"Оборачиваемость {turn} дней — товар стоит, замораживает капитал."
    if rid == "SEASONAL_SIGNAL":
        return phrases.render_phrase("seasonal_peak_strong" if "peak_strong" in finding.diagnostics else "seasonal_peak_weak",
                                     category=sku.get("category", "—"),
                                     pct=finding.trigger_details.get("orders", {}).get("pct", "—"))
    return f"{finding.rule_id}"


def fmt_month_ru_genitive(month_str):
    """'2026-05' → 'май'."""
    if not month_str:
        return "месяц"
    try:
        m = int(month_str.split("-")[1])
    except Exception:
        return "месяц"
    names = ["", "январь","февраль","март","апрель","май","июнь","июль","август","сентябрь","октябрь","ноябрь","декабрь"]
    return names[m] if 1 <= m <= 12 else "месяц"


# v2.2: склонения имён менеджеров для правильных русских конструкций
MANAGER_GENITIVE = {"Виктория": "Виктории", "Настя": "Насти", "Владимир": "Владимира"}


def _gen(name):
    return MANAGER_GENITIVE.get(name, name)


def _plural(n, one, few, many):
    """1 приоритет / 2-4 приоритета / 5+ приоритетов."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11: return one
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14): return few
    return many


def _fmt_rub(v) -> str:
    if v is None:
        return "—"
    v = float(v)
    if abs(v) >= 1e6:
        return f"{v/1e6:.1f} млн"
    if abs(v) >= 1e3:
        return f"{v/1e3:.0f} тыс"
    return f"{int(v)}"


def _delta_pct(a, b):
    if a is None or b is None or a == 0:
        return None
    return (b - a) / abs(a) * 100


# ============================================================
# v2.3 БЛОК 1: классификация good_performer
# Один негатив исключает SKU из "хорошо идёт".
# ============================================================
def _mean_window(arr, end_excl, n):
    if not arr or end_excl < n:
        return None
    win = [v for v in arr[end_excl - n:end_excl] if v is not None]
    return sum(win) / len(win) if win else None


def _mean_last(arr, n):
    if not arr:
        return None
    tail = [v for v in arr[-n:] if v is not None]
    return sum(tail) / len(tail) if tail else None


def is_good_performer(sku: dict) -> tuple[bool, str]:
    """v2.3: SKU попадает в «хорошо идёт» только если ВСЕ негативные условия пройдены
    И есть ХОТЯ БЫ ОДИН положительный сигнал.

    Возвращает (good: bool, reason: str). reason — почему НЕ хорошо (если good=False)
    или какой позитив сработал (если good=True).
    """
    h = sku.get("history") or {}
    m = sku.get("month") or {}
    code = sku.get("code") or ""
    role = ctx.role_of(sku.get("group", "") or "")
    norms = ctx.ROLE_NORMS.get(role, {})

    orders = h.get("orders") or []
    drr_arr = h.get("drr") or []
    conv_arr = h.get("conv") or []
    stock_arr = h.get("stock") or []
    price_arr = h.get("price") or []

    # === НЕГАТИВНЫЕ ФИЛЬТРЫ (любой неуспех = не good) ===
    # 1. orders_7d_delta >= -5%
    o_prev = _mean_window(orders, len(orders) - 7, 7) if len(orders) >= 14 else None
    o_last = _mean_last(orders, 7) if len(orders) >= 7 else None
    if o_prev and o_last is not None:
        dp = (o_last - o_prev) / abs(o_prev) * 100
        if dp < -5:
            return False, f"orders {dp:.0f}% за 7д"

    # 2. drr_7d_delta_pp <= +2
    d_prev = _mean_window(drr_arr, len(drr_arr) - 7, 7) if len(drr_arr) >= 14 else None
    d_last = _mean_last(drr_arr, 7) if len(drr_arr) >= 7 else None
    if d_prev is not None and d_last is not None:
        if d_last - d_prev > 2:
            return False, f"drr +{d_last-d_prev:.1f}пп за 7д"

    # 3. cr_7d_delta_pp >= -3
    c_prev = _mean_window(conv_arr, len(conv_arr) - 7, 7) if len(conv_arr) >= 14 else None
    c_last = _mean_last(conv_arr, 7) if len(conv_arr) >= 7 else None
    if c_prev is not None and c_last is not None:
        if c_last - c_prev < -3:
            return False, f"conv {c_last-c_prev:.1f}пп за 7д"

    # 4. margin >= role_floor (только если коридор задан)
    margin = m.get("margin")
    m_min = norms.get("margin_min")
    if margin is not None and m_min is not None and margin < m_min:
        return False, f"margin {margin:.1f}% < {m_min}%"

    # 5. stock_days_left >= 7
    stock_now = m.get("stock")
    velocity = _mean_last(orders, 7)
    if stock_now is not None and velocity and velocity > 0:
        dz = stock_now / velocity
        if dz < 7:
            return False, f"остаток на {dz:.1f}д"

    # === ХОТЯ БЫ ОДИН ПОЗИТИВ ===
    positives = []
    # звезда плана
    if code in ctx.STARS_CODES:
        positives.append("star")
    # заказы +20%
    if o_prev and o_last and (o_last - o_prev) / abs(o_prev) * 100 >= 20:
        positives.append("orders+20%")
    # конверсия >=4%
    if c_last is not None and c_last >= 4:
        positives.append(f"conv {c_last:.1f}%")
    # эффект снижения цены: цена −5% за 7 дней → заказы +30%
    p_prev = _mean_window(price_arr, len(price_arr) - 7, 7) if len(price_arr) >= 14 else None
    p_last = _mean_last(price_arr, 7) if len(price_arr) >= 7 else None
    if p_prev and p_last and o_prev and o_last:
        price_dp = (p_last - p_prev) / abs(p_prev) * 100
        orders_dp = (o_last - o_prev) / abs(o_prev) * 100
        if price_dp <= -5 and orders_dp >= 30:
            positives.append("эффект скидки")

    if not positives:
        return False, "нет позитивных сигналов"
    return True, ", ".join(positives)


def filter_findings_classification(findings: list) -> list:
    """v2.3: пересортировка opportunity → risk если SKU не проходит good_performer.

    Не выкидываем finding, просто меняем task_type/priority.
    """
    out = []
    for f in findings:
        if f.task_type != "opportunity":
            out.append(f)
            continue
        good, reason = is_good_performer(f.sku_data)
        if good:
            out.append(f)
        else:
            # перевод в risk, +25 к severity чтобы пройти приоритизацию
            f.task_type = "risk"
            f.severity_score = (f.severity_score or 0) + 25
            f.priority = "yellow" if f.severity_score < 120 else "red"
            f.diagnostics = (f.diagnostics or []) + [f"reclassified: {reason}"]
            out.append(f)
    return out


def _build_changes(sku: dict) -> list[dict]:
    """v1.8: для карточки — что изменилось за 7 дней.
    Сравниваем последние 7 дней vs предыдущие 7.
    Метрики: цена после СПП, остаток, заказы/день.
    Возвращает [{label, from, to, delta_pct, delta_dir, is_bad, unit}].
    """
    h = sku.get("history") or {}

    def mean_last(arr, n):
        if not arr:
            return None
        tail = [v for v in arr[-n:] if v is not None]
        return sum(tail) / len(tail) if tail else None

    def mean_window(arr, end_excl, n):
        if not arr or end_excl < n:
            return None
        win = [v for v in arr[end_excl - n:end_excl] if v is not None]
        return sum(win) / len(win) if win else None

    changes = []

    orders = h.get("orders") or []
    if len(orders) >= 14:
        prev7 = mean_window(orders, len(orders) - 7, 7)
        last7 = mean_last(orders, 7)
        if prev7 is not None and last7 is not None and prev7 > 0:
            dp = _delta_pct(prev7, last7)
            changes.append({
                "label": "Заказы/день",
                "from": round(prev7, 1),
                "to": round(last7, 1),
                "delta_pct": round(dp, 1),
                "delta_dir": "up" if dp > 0 else "down" if dp < 0 else "flat",
                "is_bad": dp < -5,
                "unit": "шт",
            })

    price = h.get("price") or []
    if len(price) >= 14:
        prev7 = mean_window(price, len(price) - 7, 7)
        last7 = mean_last(price, 7)
        if prev7 is not None and last7 is not None and prev7 > 0:
            dp = _delta_pct(prev7, last7)
            changes.append({
                "label": "Цена для покупателя",
                "from": round(prev7, 0),
                "to": round(last7, 0),
                "delta_pct": round(dp, 1),
                "delta_dir": "up" if dp > 0 else "down" if dp < 0 else "flat",
                "is_bad": False,  # цена сама по себе не «плохо»/«хорошо»
                "unit": "₽",
            })

    # v2.1: ДРР day-7 → day-1
    drr = h.get("drr") or []
    if len(drr) >= 14:
        prev7 = mean_window(drr, len(drr) - 7, 7)
        last7 = mean_last(drr, 7)
        if prev7 is not None and last7 is not None:
            dp = last7 - prev7  # пп
            # норма роли
            role = ctx.role_of(sku.get("group", "") or "")
            norm_drr = (ctx.ROLE_NORMS.get(role) or {}).get("drr_max")
            is_bad = (norm_drr is not None and last7 > norm_drr) or (dp > 2)
            changes.append({
                "label": "ДРР",
                "from": round(prev7, 1),
                "to": round(last7, 1),
                "delta_pp": round(dp, 1),
                "delta_dir": "up" if dp > 0 else "down" if dp < 0 else "flat",
                "is_bad": is_bad,
                "unit": "%",
            })

    # v2.1: Конверсия в заказ day-7 → day-1
    conv = h.get("conv") or []
    if len(conv) >= 14:
        prev7 = mean_window(conv, len(conv) - 7, 7)
        last7 = mean_last(conv, 7)
        if prev7 is not None and last7 is not None:
            dp = last7 - prev7  # пп
            is_bad = last7 < 3 or dp < -0.5
            changes.append({
                "label": "Конверсия в заказ",
                "from": round(prev7, 1),
                "to": round(last7, 1),
                "delta_pp": round(dp, 1),
                "delta_dir": "up" if dp > 0 else "down" if dp < 0 else "flat",
                "is_bad": is_bad,
                "unit": "%",
            })

    stock = h.get("stock") or []
    if len(stock) >= 14:
        prev_val = stock[-8] if len(stock) >= 8 and stock[-8] is not None else None
        last_val = stock[-1] if stock and stock[-1] is not None else None
        if prev_val is not None and last_val is not None:
            last7_orders = [o for o in (orders[-7:] if orders else []) if o is not None]
            sold_7 = int(round(sum(last7_orders))) if last7_orders else None
            velocity = sum(last7_orders) / len(last7_orders) if last7_orders else None
            days_to_zero = (last_val / velocity) if (velocity and velocity > 0) else None
            # v2.1 — цвет по дням до нуля на складе, тон/знак не имеет
            # значения для семантики «продаётся ли товар нормально».
            stock_growing = (last_val - prev_val) > 0
            orders_dropped_30 = (sold_7 or 0) < (sum([o for o in (orders[-14:-7] if len(orders) >= 14 else []) if o is not None]) * 0.7)
            if days_to_zero is not None and days_to_zero < 7:
                stock_status = "red"
            elif days_to_zero is not None and days_to_zero <= 14:
                stock_status = "yellow"
            elif stock_growing and orders_dropped_30:
                stock_status = "yellow"  # залежался
            else:
                stock_status = "flat"   # норма
            changes.append({
                "label": "Остаток",
                "kind": "stock",
                "sold_7d": sold_7,
                "stock_now": int(last_val),
                "days_to_zero": round(days_to_zero, 1) if days_to_zero is not None else None,
                "status": stock_status,
                "is_bad": stock_status == "red",
                "unit": "шт",
            })

    return changes


def _build_verdict(changes: list[dict], priority: str) -> tuple[str, str]:
    """По changes и priority определяем «стало лучше / хуже / без изменений»."""
    if not changes:
        return ("Изменения за 7 дней не определены.", "same")
    bad = sum(1 for c in changes if c.get("is_bad"))
    bad_dir = sum(1 for c in changes if c.get("delta_dir") == "down" and c.get("label") in ("Заказы/день", "Остаток"))
    if bad >= 2 or priority == "red":
        return ("Стало хуже за 7 дней.", "worse")
    if bad == 0 and bad_dir == 0:
        return ("Стабильно за 7 дней.", "better" if priority == "yellow" else "same")
    return ("Смешанная динамика за 7 дней.", "same")


def _build_reason(changes: list[dict]) -> str | None:
    """v2.3 БЛОК 7: автовывод о возможной причине по динамике changes.

    Возвращает строку или None если данных недостаточно.
    """
    price_dp = orders_dp = drr_dp = conv_dp = stock_dp = None
    stock_status = None
    for c in changes:
        if c.get("label") == "Цена для покупателя":
            price_dp = c.get("delta_pct")
        elif c.get("label") == "Заказы/день":
            orders_dp = c.get("delta_pct")
        elif c.get("label") == "ДРР":
            drr_dp = c.get("delta_pp")
        elif c.get("label") == "Конверсия в заказ":
            conv_dp = c.get("delta_pp")
        elif c.get("kind") == "stock":
            stock_status = c.get("status")
    if all(v is None for v in (price_dp, orders_dp, drr_dp, conv_dp, stock_status)):
        return None

    # ДРР удвоился + конверсия рухнула + цена почти не менялась
    if (drr_dp is not None and drr_dp > 10
            and conv_dp is not None and conv_dp < -10
            and price_dp is not None and abs(price_dp) < 5):
        return ("ДРР удвоился, конверсия рухнула, наша цена почти не менялась. "
                "Скорее всего СПП от ВБ упала + конкуренты ушли в скидку. "
                "Проверить топ-5 в выдаче ВБ.")

    # СПП упала + конверсия упала
    if conv_dp is not None and conv_dp < -10 and (price_dp is None or price_dp >= 0):
        return ("Конверсия в заказ просела при стабильной цене — цена для покупателя выросла "
                "из-за снижения СПП от ВБ. Зайти в ближайшую акцию ВБ, чтобы алгоритм поднял СПП.")

    # ДРР вырос + заказы не растут
    if drr_dp is not None and drr_dp > 5 and orders_dp is not None and abs(orders_dp) < 10:
        return ("ДРР вырос, заказы не выросли — реклама неэффективна. "
                "Проверить ставки и список ключевых запросов.")

    # цена снижена + заказы выросли
    if price_dp is not None and price_dp < -5 and orders_dp is not None and orders_dp > 30:
        return (f"Снижение цены на {abs(price_dp):.1f}% дало рост заказов +{orders_dp:.0f}%. "
                "Снижение работает, держим до восстановления маржи или роста СПП.")

    # цена выросла + заказы упали
    if price_dp is not None and price_dp > 3 and orders_dp is not None and orders_dp < -20:
        return (f"После повышения цены на {price_dp:.1f}% заказы упали на {abs(orders_dp):.0f}%. "
                "Спрос эластичный, рассмотреть откат к прежней цене.")

    # заказы упали + остаток в красной зоне (резкая распродажа)
    if orders_dp is not None and orders_dp < -30 and stock_status == "red":
        return ("Заказы упали при обнулении остатка — возможна ошибка остатков или потеря "
                "выкупа на ВБ. Проверить кабинет.")

    # заказы упали + остаток вырос (товар залегает)
    if orders_dp is not None and orders_dp < -30 and stock_status in (None, "flat", "yellow"):
        return ("Заказы упали при росте/стабильном остатке — товар залегает. "
                "Проверить позицию в выдаче и качество фото.")

    # всё стабильно
    if all(v is None or abs(v) < 5 for v in (orders_dp, drr_dp, conv_dp)) and (price_dp is None or abs(price_dp) < 3):
        return ("Метрики стабильны, видимых причин для проблемы нет. "
                "Возможно сезонное колебание.")

    return None


def _adjust_variants_for_context(variants: list[str], changes: list[dict]) -> list[str]:
    """v2.3 БЛОК 9: если цена снижена >5% за 7 дней — убираем советы «зайти в акцию»
    и подменяем контекстными. Иначе оставляем как есть."""
    price_dp = None
    orders_dp = None
    drr_dp = None
    for c in changes:
        if c.get("label") == "Цена для покупателя":
            price_dp = c.get("delta_pct")
        elif c.get("label") == "Заказы/день":
            orders_dp = c.get("delta_pct")
        elif c.get("label") == "ДРР":
            drr_dp = c.get("delta_pp")
    if price_dp is None or price_dp > -5:
        return variants
    # Цена снижена >5% — товар уже в акции/скидке. Фильтруем «акция/спп через кабинет».
    bad_tokens = ("акци", "скидку wildberries", "ближайшую акцию")
    filtered = [v for v in variants if not any(t.lower() in v.lower() for t in bad_tokens)]
    extra = []
    if drr_dp is not None and drr_dp > 5:
        extra.append("Цена снижена, но ДРР растёт — проверь топ-5 в выдаче ВБ, конкуренты тоже могли уйти в скидку")
    elif orders_dp is not None and orders_dp > 30:
        extra.append("Снижение цены даёт рост заказов — удерживаем цену до восстановления маржи")
    else:
        extra.append("Снижение цены 5+ дней без эффекта — подожди ещё 2-3 дня или проверь позиции в выдаче")
    return extra + filtered


def _build_plan_info(sku: dict, sku_plans: dict, current_month: str) -> dict | None:
    """v2.3 БЛОК 5: блок плана по SKU. None если плана нет."""
    code = sku.get("code")
    if not code or not sku_plans:
        return None
    p = sku_plans.get(code) or {}
    plan_r = p.get("plan_revenue")
    plan_o = p.get("plan_orders")
    fact_r = p.get("fact_revenue") or (sku.get("month") or {}).get("revenue")
    fact_o = p.get("fact_orders") or (sku.get("month") or {}).get("orders")
    if not plan_r and not plan_o:
        return None

    # темп на сегодня (как и в company_summary)
    from datetime import date as _date
    pct = None
    days_left = None
    if current_month:
        try:
            y, m_ = (int(x) for x in current_month.split("-"))
            dim = (_date(y if m_ < 12 else y + 1, (m_ % 12) + 1, 1) - _date(y, m_, 1)).days
            today = _date.today()
            if today.year == y and today.month == m_:
                dop = today.day
                days_left = dim - dop
            elif today > _date(y, m_, dim):
                dop = dim
                days_left = 0
            else:
                dop = 0
                days_left = dim
            if plan_r and fact_r is not None and dop > 0:
                expected_at = plan_r * dop / dim
                if expected_at > 0:
                    pct = fact_r / expected_at * 100
        except Exception:
            pass

    out = {
        "plan_revenue": plan_r,
        "plan_orders": plan_o,
        "fact_revenue": fact_r,
        "fact_orders": fact_o,
        "tempo_pct": round(pct) if pct is not None else None,
        "days_left": days_left,
    }
    return out


def render_task(finding: Finding, sku_plans: dict = None, current_month: str = None) -> dict:
    """v1.8: расширенная структура задачи — attention / changes / variants / verdict."""
    sku = finding.sku_data
    attention = _problem_text(finding)
    variants = actions.get_variants(finding.rule_id)
    changes = _build_changes(sku)
    variants = _adjust_variants_for_context(variants, changes)

    # v2.3 БЛОК 1: если SKU был переклассифицирован из opportunity в risk —
    # переписываем attention под реальную проблему.
    reclass = next((d for d in (finding.diagnostics or []) if isinstance(d, str) and d.startswith("reclassified:")), None)
    if reclass and finding.task_type == "risk":
        symptom = reclass.replace("reclassified:", "").strip()
        attention = f"Несмотря на хорошие месячные показатели — за 7 дней проседает: {symptom}."
    verdict_text, verdict_dir = _build_verdict(changes, finding.priority)
    plan_info = _build_plan_info(sku, sku_plans or {}, current_month) if sku_plans else None
    reason = _build_reason(changes)

    return {
        "sku": finding.sku_code,
        "manager": finding.manager,
        "priority": finding.priority,
        "task_type": finding.task_type,
        # v1.8 daily contract:
        "attention": attention,
        "changes": changes,
        "variants": variants,
        "verdict": verdict_text,
        "verdict_dir": verdict_dir,
        # v2.3:
        "plan_info": plan_info,
        "reason": reason,
        # обратная совместимость для модалки SKU и старого UI:
        "problem": attention,
        "action": variants[0] if variants else "",
        "action_extended": "\n• ".join([""] + variants) if variants else "",
        # технические поля:
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

    # v1.9: убраны захардкоженные цели. Если targets есть в data — используем,
    # иначе просто показываем факт.
    company_targets = data.get("company_targets") or {}
    margin_target = company_targets.get("margin_target")
    drr_target = company_targets.get("drr_target")

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

    # 3. Маржа — без выдуманных целей
    if cur.get("margin") is not None:
        if margin_target is not None:
            gap = margin_target - cur["margin"]
            if cur["margin"] >= margin_target - 1:
                lines.append(phrases.render_phrase("company_margin_ok", value=round(cur["margin"], 1), target=margin_target))
            else:
                lines.append(phrases.render_phrase("company_margin_below",
                                                   value=round(cur["margin"], 1),
                                                   target=margin_target,
                                                   gap=round(gap, 1)))
        else:
            v = round(cur["margin"], 1)
            lines.append(f"Маржа за {fmt_month_ru_genitive(current)} — {v:.1f}%.".replace(".", ","))

    # 4. ДРР — без выдуманных целей
    if cur.get("drr") is not None:
        if drr_target is not None:
            if cur["drr"] <= drr_target + 0.3:
                lines.append(phrases.render_phrase("company_drr_ok", value=round(cur["drr"], 2), target=drr_target))
            else:
                lines.append(phrases.render_phrase("company_drr_high", value=round(cur["drr"], 2), target=drr_target))
        else:
            v = round(cur["drr"], 2)
            lines.append(f"Доля рекламных расходов за {fmt_month_ru_genitive(current)} — {v:.2f}%.".replace(".", ","))

    # 5. Состав задач
    if not findings:
        lines.append(phrases.render_phrase("tasks_summary_clean"))
    else:
        lines.append(phrases.render_phrase("tasks_summary",
                                           total=len(findings),
                                           red=red_n, yellow=yellow_n, opp=opp_n))

    # 6. v1.8: контекст «К вчера» и «К плану мая»
    y = data.get("yesterday") or {}
    last_y = y.get("last") or {}
    prev_y = y.get("prev") or {}
    if last_y and prev_y and last_y.get("revenue") and prev_y.get("revenue"):
        dlt_rev = (last_y["revenue"] - prev_y["revenue"]) / prev_y["revenue"] * 100
        sign = "+" if dlt_rev >= 0 else "−"
        rev_part = f"выручка {sign}{abs(dlt_rev):.0f}%"
        profit_part = "прибыль ждёт загрузки" if not last_y.get("profit_is_clean") else ""
        if last_y.get("profit_is_clean") and prev_y.get("profit_is_clean"):
            dlt_p = (last_y["profit"] - prev_y["profit"]) / abs(prev_y["profit"]) * 100 if prev_y["profit"] else 0
            sgn = "+" if dlt_p >= 0 else "−"
            profit_part = f"прибыль {sgn}{abs(dlt_p):.0f}%"
        try:
            prev_date_human = prev_y.get("date", "").split(".")[0] + "." + prev_y.get("date", "").split(".")[1]
        except Exception:
            prev_date_human = prev_y.get("date", "")
        parts = [rev_part]
        if profit_part:
            parts.append(profit_part)
        lines.append(f"К {prev_date_human} (вчера): {', '.join(parts)}.")

    if pace is not None and plan_company:
        cur_rev_month = cur.get("revenue") or 0
        gap = plan_company - cur_rev_month
        gap_m = f"{abs(gap)/1e6:.2f}".replace(".", ",")
        plan_m = f"{plan_company/1e6:.2f}".replace(".", ",")
        if pace >= 100:
            lines.append(f"К плану мая: {pace:.0f}% темпа, идём с опережением {gap_m} М ₽.")
        else:
            lines.append(f"К плану мая: {pace:.0f}% темпа, отстаём {gap_m} М до плана {plan_m} М.")

    # v2.2: краткие секции менеджеров в формате Антона
    lines.append("")
    icons = {"Виктория": "🟦", "Настя": "🟪", "Владимир": "🟧"}
    for mgr in ctx.ACTIVE_MANAGERS:
        mgr_findings = [f for f in findings if f.manager == mgr]
        mgr_month = ((data.get("manager_monthly") or {}).get(current) or {}).get(mgr) or {}
        plan_m_val = PLANS_BY_MGR.get(current, {}).get(mgr)
        pace_m = None
        if plan_m_val and mgr_month.get("revenue") and dim and dop:
            expected = plan_m_val * dop / dim
            pace_m = mgr_month["revenue"] / expected * 100 if expected else None
        pace_str = f"{pace_m:.0f}%" if pace_m else "—"
        rev_str = _fmt_rub(mgr_month.get("revenue"))
        plan_str = _fmt_rub(plan_m_val) if plan_m_val else "—"
        emoji = "✅" if (pace_m or 0) >= 100 else ("⚠️" if (pace_m or 0) < 85 else "")
        lines.append(f"{icons.get(mgr, '')} {mgr} · темп {pace_str} {emoji}".strip())
        lines.append(f"   {rev_str} ₽ при плане {plan_str} ₽")
        if mgr_findings:
            codes = ", ".join(f.sku_code for f in mgr_findings[:3])
            lines.append(f"   В фокусе: {codes}")
        lines.append("")

    return "\n".join(lines).rstrip()


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
        n = len(mgr_findings)
        word = _plural(n, "приоритет", "приоритета", "приоритетов")
        lines.append(f"У {_gen(manager)} {n} {word} на сегодня — все в списке.")
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
        return "red", "товара нет на складе при активных заказах"
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
        return "red", "товара нет на складе — продаём с прерываниями"
    if days_to_oos is None:
        return "yellow", "нет данных по скорости продаж"
    if days_to_oos < 7:
        return "red", f"хватит на {days_to_oos:.1f} дн — критично"
    if days_to_oos <= 14:
        return "yellow", f"хватит на {days_to_oos:.1f} дн — следить"
    return "green", f"хватит на {days_to_oos:.1f} дн — норма"


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
    """Главная функция — возвращает dict, готовый к сериализации в ai_summary.json.

    v2.3: filter_findings_classification применяется в update.py до prioritize.
    Здесь повторно не вызываем.
    """
    sku_plans = data.get("sku_plans") or {}
    current_month = (data.get("meta") or {}).get("current_month")
    tasks = [render_task(f, sku_plans=sku_plans, current_month=current_month) for f in findings]

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
