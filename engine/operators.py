"""Операторы — детерминированные функции анализа над временными рядами.

Контракт каждого оператора:
  - принимает values (list/dict) и параметры
  - возвращает OpResult(matched: bool, score: int 0-100, details: dict)
  - НЕ выбрасывает исключений (данные могут быть None/пустые)
  - идемпотентен

Используются YAML-правилами через rule_loader.py.
"""
from __future__ import annotations

import math
import statistics
from collections import namedtuple
from typing import Iterable

from . import contexts as ctx

OpResult = namedtuple("OpResult", "matched score details")
EMPTY = OpResult(False, 0, {})


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ
# ============================================================
def _clean(series) -> list[float]:
    """Отфильтровать None/NaN и привести к float."""
    out = []
    if not series:
        return out
    for v in series:
        if v is None:
            continue
        try:
            f = float(v)
            if math.isnan(f) or math.isinf(f):
                continue
            out.append(f)
        except (TypeError, ValueError):
            continue
    return out


def safe_div(a, b):
    """Делит a/b, при b=0 или None — возвращает None.

    Пример: safe_div(100, 0) → None; safe_div(50, 200) → 0.25.
    """
    if a is None or b is None:
        return None
    try:
        b = float(b)
        if b == 0:
            return None
        return float(a) / b
    except (TypeError, ValueError):
        return None


def null_safe_pct(curr, prev):
    """Процентное изменение curr к prev. None если деление не возможно.

    Пример: null_safe_pct(120, 100) → 20.0; null_safe_pct(80, 100) → -20.0.
    """
    if curr is None or prev is None:
        return None
    try:
        if prev == 0:
            return None
        return (curr - prev) / abs(prev) * 100
    except (TypeError, ValueError):
        return None


def last_value(series):
    """Последнее не-None значение в серии. None если ничего нет."""
    if not series:
        return None
    for v in reversed(list(series)):
        if v is not None:
            return v
    return None


def first_non_null(series):
    """Первое не-None значение в серии."""
    if not series:
        return None
    for v in series:
        if v is not None:
            return v
    return None


def velocity_avg(series, window: int = 7):
    """Средние продажи за последние window дней.

    Пример: velocity_avg([5,8,6,7,9,4,6], 7) → 6.43.
    """
    if not series:
        return None
    tail = list(series)[-window:]
    clean = _clean(tail)
    if not clean:
        return None
    return sum(clean) / len(clean)


def velocity_recent(series):
    """Средние продажи за последние 7 дней (alias)."""
    return velocity_avg(series, 7)


def role_of_sku(sku: dict) -> str:
    return ctx.role_of(sku.get("group", ""))


# ============================================================
# THRESHOLD
# ============================================================
def threshold(value, min=None, max=None):
    """Проверяет попадание value в диапазон [min, max].

    matched=True если value за пределами.
    Пример: threshold(15, min=20) → matched=True (15<20).
    """
    if value is None:
        return EMPTY
    min_v, max_v = min, max
    if min_v is not None and value < min_v:
        gap = (min_v - value) / abs(min_v) * 100 if min_v != 0 else 0
        score = 100 if abs(gap) >= 100 else int(abs(gap))
        return OpResult(True, score, {"value": value, "min": min_v, "kind": "below"})
    if max_v is not None and value > max_v:
        gap = (value - max_v) / abs(max_v) * 100 if max_v != 0 else 0
        score = 100 if abs(gap) >= 100 else int(abs(gap))
        return OpResult(True, score, {"value": value, "max": max_v, "kind": "above"})
    return EMPTY


def threshold_by_role(value, metric: str, role: str):
    """Применяет нормы конкретной роли из ROLE_NORMS.

    metric ∈ {"margin", "drr", "turnover"}.
    Пример: threshold_by_role(18, "margin", "Локомотив") →
            matched=True (норма ≥22%).
    """
    norms = ctx.ROLE_NORMS.get(role, {})
    if not norms:
        return EMPTY
    if metric == "margin":
        return threshold(value, min=norms.get("margin_min"), max=norms.get("margin_max"))
    if metric == "drr":
        return threshold(value, max=norms.get("drr_max"))
    if metric == "turnover":
        return threshold(value, max=norms.get("turnover_target"))
    return EMPTY


def absolute_gap(value, target):
    """Абсолютное отклонение от целевого значения."""
    if value is None or target is None:
        return EMPTY
    gap = value - target
    return OpResult(True, min(100, int(abs(gap))), {"gap": gap, "value": value, "target": target})


def relative_gap(value, target):
    """Относительное (%) отклонение от целевого значения."""
    if value is None or target is None or target == 0:
        return EMPTY
    pct = (value - target) / abs(target) * 100
    return OpResult(True, min(100, int(abs(pct))), {"pct": pct, "value": value, "target": target})


# ============================================================
# TREND
# ============================================================
def trend(series, window_days: int = 7, direction: str = "down", min_change_pct: float = -30):
    """Тренд за последние window_days. direction: 'down'|'up'|'any'.

    matched=True если изменение в нужном направлении ≥ min_change_pct.
    Пример: trend([20,18,16,12,10,8,7], 7, "down", -30) → matched=True (-65%).
    """
    clean = _clean(series)
    if len(clean) < 2:
        return EMPTY
    tail = clean[-window_days:]
    if len(tail) < 2:
        return EMPTY
    start = sum(tail[:max(1, len(tail) // 3)]) / max(1, len(tail) // 3)
    end = sum(tail[-max(1, len(tail) // 3):]) / max(1, len(tail) // 3)
    if start == 0:
        return EMPTY
    pct = (end - start) / abs(start) * 100
    matched = False
    if direction == "down" and pct <= min_change_pct:
        matched = True
    elif direction == "up" and pct >= min_change_pct:
        matched = True
    elif direction == "any" and abs(pct) >= abs(min_change_pct):
        matched = True
    if not matched:
        return EMPTY
    return OpResult(True, min(100, int(abs(pct))), {"pct": round(pct, 1),
                                                     "start": round(start, 2),
                                                     "end": round(end, 2),
                                                     "window": window_days})


def moving_average(series, window: int = 7):
    """Скользящее среднее. Возвращает список значений MA."""
    clean = _clean(series)
    if len(clean) < window:
        return OpResult(False, 0, {"ma": []})
    ma = []
    for i in range(window - 1, len(clean)):
        ma.append(sum(clean[i - window + 1:i + 1]) / window)
    return OpResult(True, 0, {"ma": ma})


def acceleration(series):
    """Ускорение тренда: меняется ли скорость падения/роста.

    Положительное → ускорение роста или замедление падения.
    Пример: acceleration([10,9,8,5,2,1,0.5]) → отрицательное (ускоряется падение).
    """
    clean = _clean(series)
    if len(clean) < 4:
        return EMPTY
    half = len(clean) // 2
    first = clean[:half]
    last = clean[half:]
    if len(first) < 2 or len(last) < 2:
        return EMPTY
    slope_a = (first[-1] - first[0]) / max(1, len(first) - 1)
    slope_b = (last[-1] - last[0]) / max(1, len(last) - 1)
    delta = slope_b - slope_a
    return OpResult(True, min(100, int(abs(delta) * 10)),
                    {"slope_first": round(slope_a, 3), "slope_last": round(slope_b, 3),
                     "delta": round(delta, 3)})


def volatility(series, window: int = 14):
    """Коэффициент вариации (stdev/mean) за окно.

    >0.5 — высокая волатильность.
    """
    clean = _clean(series)[-window:]
    if len(clean) < 3:
        return EMPTY
    m = statistics.mean(clean)
    if m == 0:
        return EMPTY
    cv = statistics.pstdev(clean) / abs(m)
    return OpResult(True, min(100, int(cv * 100)), {"cv": round(cv, 3), "mean": round(m, 2)})


# ============================================================
# COMPARISON
# ============================================================
def compare_periods(series, period_a_days: int = 7, period_b_days: int = 7):
    """Сравнение двух последних периодов equal-длины.

    Пример: compare_periods([1..14], 7, 7) → сравнивает первые 7 vs последние 7.
    """
    clean = _clean(series)
    if len(clean) < period_a_days + period_b_days:
        return EMPTY
    a = clean[-(period_a_days + period_b_days):-period_b_days]
    b = clean[-period_b_days:]
    sum_a = sum(a)
    sum_b = sum(b)
    if sum_a == 0:
        return EMPTY
    pct = (sum_b - sum_a) / abs(sum_a) * 100
    return OpResult(True, min(100, int(abs(pct))),
                    {"sum_a": sum_a, "sum_b": sum_b, "pct": round(pct, 1)})


def yoy_compare(this_year, last_year):
    """Сравнение год к году. На текущем этапе данных за прошлый год нет —
    возвращает EMPTY если last_year пуст."""
    if last_year is None:
        return EMPTY
    return relative_gap(this_year, last_year)


def mom_compare(this_month, last_month):
    """Месяц к месяцу — относительное изменение."""
    return relative_gap(this_month, last_month)


def vs_peer_group(value, peers: list, deviation_pct: float = 20):
    """Отклонение value от медианы peer-группы в %.

    Если <3 пиров — matched=False (мало для статистики).
    Пример: vs_peer_group(15, [20,25,22,18], 20) → matched=True (-32%).
    """
    if value is None or not peers:
        return EMPTY
    clean_peers = _clean(peers)
    if len(clean_peers) < 3:
        return EMPTY
    median = statistics.median(clean_peers)
    if median == 0:
        return EMPTY
    pct = (value - median) / abs(median) * 100
    if abs(pct) < deviation_pct:
        return EMPTY
    return OpResult(True, min(100, int(abs(pct))),
                    {"value": value, "median": round(median, 2), "pct": round(pct, 1),
                     "n_peers": len(clean_peers)})


# ============================================================
# EVENT DETECTION
# ============================================================
def spp_drop_detected(spp_series, window_days: int = 7, threshold_pp: float = 5):
    """Падение СПП на N процентных пунктов за окно.

    Пример: spp_drop_detected([27,27,26,25,22,20,19], 7, 5) → matched (−8 пп).
    """
    clean = _clean(spp_series)
    if len(clean) < 2:
        return EMPTY
    tail = clean[-window_days:]
    if len(tail) < 2:
        return EMPTY
    start_avg = sum(tail[:max(1, len(tail) // 3)]) / max(1, len(tail) // 3)
    end_avg = sum(tail[-max(1, len(tail) // 3):]) / max(1, len(tail) // 3)
    drop_pp = start_avg - end_avg
    if drop_pp < threshold_pp:
        return EMPTY
    return OpResult(True, min(100, int(drop_pp * 5)),
                    {"start": round(start_avg, 1), "end": round(end_avg, 1),
                     "drop_pp": round(drop_pp, 1)})


def price_jump_detected(price_series, window_days: int = 7, jump_pct: float = 10):
    """Скачок цены вверх за окно.

    Пример: price_jump_detected([85,85,86,99,99,99,99], 7, 10) → matched (+16%).
    """
    clean = _clean(price_series)
    if len(clean) < 2:
        return EMPTY
    tail = clean[-window_days:]
    if len(tail) < 2:
        return EMPTY
    min_v = min(tail)
    max_v = max(tail)
    if min_v == 0:
        return EMPTY
    jump = (max_v - min_v) / min_v * 100
    if jump < jump_pct:
        return EMPTY
    # фиксация: был рост (последняя ближе к max чем к min)
    if (tail[-1] - min_v) < (max_v - tail[-1]):
        return EMPTY
    return OpResult(True, min(100, int(jump)),
                    {"min": round(min_v, 2), "max": round(max_v, 2), "jump_pct": round(jump, 1)})


def promo_started(price_series, window_days: int = 7, drop_pct: float = 15):
    """Запуск акции — резкое падение цены."""
    clean = _clean(price_series)
    if len(clean) < 2:
        return EMPTY
    tail = clean[-window_days:]
    max_v = max(tail)
    last = tail[-1]
    if max_v == 0:
        return EMPTY
    drop = (max_v - last) / max_v * 100
    if drop < drop_pct:
        return EMPTY
    return OpResult(True, min(100, int(drop)), {"max": max_v, "last": last, "drop_pct": round(drop, 1)})


def promo_ended(price_series, window_days: int = 7, recovery_pct: float = 10):
    """Возврат цены после акции — рост от минимума."""
    clean = _clean(price_series)
    if len(clean) < 2:
        return EMPTY
    tail = clean[-window_days:]
    min_v = min(tail)
    last = tail[-1]
    if min_v == 0:
        return EMPTY
    rec = (last - min_v) / min_v * 100
    if rec < recovery_pct:
        return EMPTY
    return OpResult(True, min(100, int(rec)), {"min": min_v, "last": last, "recovery_pct": round(rec, 1)})


def correlation(series_a, series_b, lag_days: int = 0):
    """Корреляция Пирсона между двумя сериями (со сдвигом).

    Пример: correlation(orders, ad_spend, lag_days=2) — заказы коррелируют
    с тратами 2 дня назад.
    """
    a = _clean(series_a)
    b = _clean(series_b)
    if lag_days > 0:
        a = a[lag_days:]
        b = b[:-lag_days] if lag_days < len(b) else []
    n = min(len(a), len(b))
    if n < 5:
        return EMPTY
    a, b = a[-n:], b[-n:]
    mean_a, mean_b = sum(a) / n, sum(b) / n
    num = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    den_a = sum((a[i] - mean_a) ** 2 for i in range(n)) ** 0.5
    den_b = sum((b[i] - mean_b) ** 2 for i in range(n)) ** 0.5
    if den_a == 0 or den_b == 0:
        return EMPTY
    r = num / (den_a * den_b)
    return OpResult(True, min(100, int(abs(r) * 100)), {"r": round(r, 3), "n": n})


# ============================================================
# INVENTORY
# ============================================================
def days_to_zero(stock, daily_velocity):
    """Сколько дней до OOS при текущей скорости.

    Пример: days_to_zero(140, 10) → 14.0.
    """
    if stock is None or daily_velocity is None or daily_velocity <= 0:
        return EMPTY
    days = stock / daily_velocity
    return OpResult(True, min(100, max(0, int(100 - days * 5))),
                    {"days": round(days, 1), "stock": stock, "velocity": round(daily_velocity, 2)})


def days_to_zero_with_safety(stock, daily_velocity, safety_days: int = 7):
    """Дни до OOS с учётом запаса безопасности.

    Возвращает matched=True ТОЛЬКО если days ≤ safety_days.
    """
    base = days_to_zero(stock, daily_velocity)
    if not base.matched:
        return EMPTY
    days = base.details["days"]
    if days > safety_days:
        return OpResult(False, 0, base.details)
    score = min(100, int(100 * (1 - days / safety_days)))
    return OpResult(True, score, {**base.details, "safety_days": safety_days})


def restock_needed(stock, lead_time_days: int, daily_velocity):
    """Нужен ли подсорт сейчас: stock < lead_time × velocity."""
    if stock is None or daily_velocity is None or daily_velocity <= 0:
        return EMPTY
    threshold = lead_time_days * daily_velocity
    if stock >= threshold:
        return EMPTY
    return OpResult(True, min(100, int(100 - stock / max(1, threshold) * 100)),
                    {"stock": stock, "threshold": round(threshold, 1),
                     "lead_time_days": lead_time_days})


def overstocked(stock, daily_velocity, threshold_days: int = 120):
    """Перетарка — оборачиваемость выше N дней."""
    if stock is None or daily_velocity is None or daily_velocity <= 0:
        return EMPTY
    days = stock / daily_velocity
    if days < threshold_days:
        return EMPTY
    return OpResult(True, min(100, int(days / 4)),
                    {"days": round(days, 1), "threshold_days": threshold_days})


# ============================================================
# VALIDATION
# ============================================================
def is_active_sku(sku_data: dict, min_orders_30d: int = 5):
    """SKU активен если за 30 дней было ≥ min_orders_30d заказов."""
    h = sku_data.get("history", {}) or {}
    orders = h.get("orders") or []
    total = sum(v for v in orders if v is not None)
    if total < min_orders_30d:
        return EMPTY
    return OpResult(True, 0, {"orders_30d": int(total)})


def is_seasonal(category: str):
    """Категория сезонная или нет."""
    if ctx.is_seasonal_category(category):
        return OpResult(True, 0, {"category": category})
    return EMPTY


def is_in_stars_list(code: str):
    """SKU в списке звёзд плана."""
    if code in ctx.STARS_CODES:
        return OpResult(True, 0, {"code": code})
    return EMPTY


def is_in_loco_risk_list(code: str):
    """SKU в списке топ-локомотивов в риске."""
    if code in ctx.LOCO_RISK_CODES:
        return OpResult(True, 0, {"code": code})
    return EMPTY


# ============================================================
# ANOMALY DETECTION (v1.5) — MAD-based outlier для прибыли/маржи
# ============================================================
def _median(values):
    s = sorted(values)
    n = len(s)
    if n == 0:
        return None
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2


def median_mad(values):
    """Median + MAD (median absolute deviation) для списка чисел.

    Возвращает (median, mad). Если данных <3 — (None, None).
    MAD устойчивее std к одиночным выбросам, что и нужно для шумных
    дней с разноской.

    Пример: median_mad([100,120,110,130,90,200,−50]) → (110, 20).
    """
    clean = [v for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
    if len(clean) < 3:
        return (None, None)
    med = _median(clean)
    deviations = [abs(v - med) for v in clean]
    mad = _median(deviations)
    return (med, mad)


def compute_anomaly_mad(day_value, history_values, mad_multiplier: float = 3.0):
    """Определяет, является ли day_value аномалией относительно history_values.

    Правила (любое срабатывает):
      1) sign_flip — знак противоположен медиане истории
      2) mad_outlier — |day_value − median| > mad_multiplier × MAD

    Возвращает dict:
      {"is_anomaly": bool, "median": float|None, "mad": float|None,
       "reason": str|None, "n_history": int}

    Пример: compute_anomaly_mad(-67198, [193000,210000,205000,180000,
                                         215000,170000,225000], 3)
        → {is_anomaly: True, reason: "sign_flip", median: 205000, ...}
    """
    clean_history = [v for v in (history_values or [])
                     if v is not None and not (isinstance(v, float) and math.isnan(v))]
    n = len(clean_history)
    if day_value is None or n < 3:
        return {"is_anomaly": False, "median": None, "mad": None,
                "reason": None, "n_history": n}
    med, mad = median_mad(clean_history)
    if med is None:
        return {"is_anomaly": False, "median": None, "mad": None,
                "reason": None, "n_history": n}
    # 1. sign_flip
    if (med > 0 and day_value < 0) or (med < 0 and day_value > 0):
        return {"is_anomaly": True, "median": round(med, 2),
                "mad": round(mad, 2) if mad is not None else None,
                "reason": "sign_flip", "n_history": n}
    # 2. mad_outlier
    if mad is not None and mad > 0:
        if abs(day_value - med) > mad_multiplier * mad:
            return {"is_anomaly": True, "median": round(med, 2),
                    "mad": round(mad, 2),
                    "reason": "mad_outlier", "n_history": n}
    return {"is_anomaly": False, "median": round(med, 2),
            "mad": round(mad, 2) if mad is not None else None,
            "reason": None, "n_history": n}


def consecutive_days(series_with_flags, predicate, n_required: int = 3):
    """Считает максимальный стрик подряд идущих дней удовлетворяющих predicate,
    ПРОПУСКАЯ дни с is_anomaly=True (они не разрывают стрик и не учитываются).

    series_with_flags: list of dicts [{"value": v, "is_anomaly": bool}, ...]
    predicate: callable(value) → bool

    matched=True если найден стрик ≥ n_required.

    Пример: ДРР>10 три дня подряд при пропуске аномалий →
        consecutive_days([{value:12,is_anomaly:False},
                          {value:11,is_anomaly:False},
                          {value:99,is_anomaly:True},
                          {value:11,is_anomaly:False}],
                         lambda v: v>10, 3) → matched=True
    """
    streak = 0
    max_streak = 0
    for point in series_with_flags or []:
        if point.get("is_anomaly"):
            continue  # пропускаем, не разрываем
        v = point.get("value")
        if v is None:
            streak = 0
            continue
        if predicate(v):
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    matched = max_streak >= n_required
    return OpResult(matched, min(100, max_streak * 20),
                    {"max_streak": max_streak, "required": n_required})


# ============================================================
# Реестр операторов — для YAML rule_loader
# ============================================================
OPERATORS = {
    # threshold
    "threshold": threshold,
    "threshold_by_role": threshold_by_role,
    "absolute_gap": absolute_gap,
    "relative_gap": relative_gap,
    # trend
    "trend": trend,
    "moving_average": moving_average,
    "acceleration": acceleration,
    "volatility": volatility,
    # comparison
    "compare_periods": compare_periods,
    "yoy_compare": yoy_compare,
    "mom_compare": mom_compare,
    "vs_peer_group": vs_peer_group,
    # event-detection
    "spp_drop_detected": spp_drop_detected,
    "price_jump_detected": price_jump_detected,
    "promo_started": promo_started,
    "promo_ended": promo_ended,
    "correlation": correlation,
    # inventory
    "days_to_zero": days_to_zero,
    "days_to_zero_with_safety": days_to_zero_with_safety,
    "restock_needed": restock_needed,
    "overstocked": overstocked,
    # validation
    "is_active_sku": is_active_sku,
    "is_seasonal": is_seasonal,
    "is_in_stars_list": is_in_stars_list,
    "is_in_loco_risk_list": is_in_loco_risk_list,
    # anomaly
    "consecutive_days": consecutive_days,
}
