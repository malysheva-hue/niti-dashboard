"""Контексты — статичные словари знаний о бренде «Нити Творчества».

PEER_GROUPS строятся ДИНАМИЧЕСКИ из данных в engine.py — здесь их нет.

Используется engine.py и assembler.py.
"""
from __future__ import annotations
from datetime import date


# ============================================================
# ROLE_NORMS — нормы по 6 ролям
# ============================================================
# margin_min/margin_max в процентах. drr_max — порог классического ДРР.
# turnover_target — целевая оборачиваемость в днях. budget_share — доля
# рекламного бюджета (для будущего расчёта).
ROLE_NORMS = {
    "Локомотив":    {"margin_min": 22, "margin_max": 32,   "drr_max": 10, "turnover_target": 60,  "budget_share": 65},
    "Маржинальный": {"margin_min": 30, "margin_max": 45,   "drr_max": 8,  "turnover_target": 60,  "budget_share": 20},
    "Допродажный":  {"margin_min": 20, "margin_max": 35,   "drr_max": 6,  "turnover_target": 90,  "budget_share": 10, "_status": "Этап 3"},
    "Новинка":      {"margin_min": 20, "margin_max": None, "drr_max": 15, "turnover_target": 90,  "budget_share": 5},
    "Распродажа":   {"margin_min": 15, "margin_max": None, "drr_max": 5,  "turnover_target": 120, "budget_share": 0},
    "Неликвид":     {"margin_min": None, "margin_max": None, "drr_max": 5, "turnover_target": None, "budget_share": 0},
    "База":         {"margin_min": 25, "margin_max": 40,   "drr_max": 8,  "turnover_target": 60,  "budget_share": 15},
}

# ============================================================
# GROUP_TO_ROLE — соответствие группы и методологической роли
# ============================================================
GROUP_TO_ROLE = {
    "Фокус": "Локомотив",
    "Масштаб": "Локомотив",
    "База": "Маржинальный",
    "Новинка": "Новинка",
    "Распродажа": "Распродажа",
    "Неликвид": "Неликвид",
}


# ============================================================
# UNIVERSAL_THRESHOLDS — универсальные пороги вне зависимости от роли
# ============================================================
UNIVERSAL_THRESHOLDS = {
    "margin_critical": 0,         # маржа ниже = убыточный
    "margin_warning": 20,         # красная линия для всех ролей (кроме Распродажа/Неликвид)
    "drr_red_with_low_margin": {"drr": 15, "margin": 25},
    "turnover_warning": 90,
    "turnover_critical": 120,
    "vyvod": {"revenue_max": 5000, "margin_max": 15, "turnover_min": 180},
    "drr_runaway_multiplier": 1.5,   # ДРР > N × нормы роли → runaway
    "order_drop_threshold_pct": -30, # падение заказов >30% за 7д
    "growth_threshold_pct": 30,      # рост >30% — точка роста
    "tacos_low_for_growth": 3,       # TACOS <3% при росте — органика
}


# ============================================================
# STARS — 4 артикула-приоритета на 2026
# ============================================================
STARS = [
    {"code": "КЛ007",  "name": "Клеевые стержни 11 мм 30 см",  "manager": "Владимир",
     "role": "Локомотив", "note": "Активный рост в мае (+44% восстановление)",
     "target_growth_pct": 40, "boost": 30},
    {"code": "КЛ003",  "name": "Клеевые стержни 7мм 15 см",    "manager": "Владимир",
     "role": "Локомотив", "note": "Оборачиваемость 10 дней, в дефиците",
     "target_growth_pct": 30, "boost": 30},
    {"code": "ЛК013",  "name": "Леска прозрачная 0,6мм 100м",  "manager": "Настя",
     "role": "Локомотив", "note": "Звезда плана отскочила, эффект правки x2",
     "target_growth_pct": 50, "boost": 30},
    {"code": "ПВС002", "name": "Проволока синельная 200шт",    "manager": "Настя",
     "role": "Локомотив", "note": "Пик ноября ×13 к лету — главный риск сезона",
     "target_growth_pct": 25, "boost": 30},
]
STARS_CODES = {s["code"] for s in STARS}


# ============================================================
# LOCO_RISK_LIST — топ-локомотивы под контролем поставок
# ============================================================
LOCO_RISK_LIST = [
    {"code": "ПС002",     "name": "Клеевой термопистолет 7мм + 25 стержней", "manager": "Владимир", "boost": 25},
    {"code": "ФМПНН0032", "name": "Фоамиран 50x50 см 2мм 10 листов",         "manager": "Владимир", "boost": 25},
    {"code": "ЮТ001",     "name": "Ювелирный тросик ланка 0,3 мм",           "manager": "Настя",     "boost": 25},
    {"code": "ЮТ002",     "name": "Ювелирный тросик ланка 0,38 мм",          "manager": "Настя",     "boost": 25},
]
LOCO_RISK_CODES = {l["code"] for l in LOCO_RISK_LIST}


# ============================================================
# MANAGER_SPECIALIZATION — какие категории ведёт каждый менеджер
# ============================================================
# Используется в assembler.py для тонких формулировок.
MANAGER_SPECIALIZATION = {
    "Виктория": {
        "categories": ["Бусины", "Аксессуары для бижутерии", "Брелоки",
                       "Подвески", "Аксессуары для брелков"],
        "tone": "детальный, бусины и фурнитура — ценит точечные наблюдения",
    },
    "Настя": {
        "categories": ["Аксессуары для рукоделия", "Леска", "Тросики",
                       "Нити", "Шнуры", "Резинки", "Резинка-нить"],
        "tone": "ритмичный, реагирует на дневные тренды быстро",
    },
    "Владимир": {
        "categories": ["Пистолеты термоклеевые", "Стержни клеевые",
                       "Проволоки", "Швейная фурнитура", "Ткани",
                       "Фоамиран", "Пуговицы"],
        "tone": "новый, без накопительных ярлыков, нужна базовая инструкция",
    },
}
ACTIVE_MANAGERS = ["Виктория", "Настя", "Владимир"]


# ============================================================
# SUPPLY_SCHEDULE — еженедельный подсорт
# ============================================================
SUPPLY_SCHEDULE = {
    "warehouse": "Королёв",
    "destination": "WB FBO",
    "weekday": "пятница",
    "weekday_idx": 4,             # понедельник=0
    "lead_time_days": 3,
}


# ============================================================
# SEASONAL_CATEGORIES — категории с выраженной сезонностью
# ============================================================
SEASONAL_CATEGORIES = {
    "Проволоки для рукоделия": {"peak_months": [10, 11, 12], "trough_months": [6, 7]},
    "Фоамиран":                {"peak_months": [11, 12, 2, 3], "trough_months": [7, 8]},
    "Бусины":                  {"peak_months": [11, 12], "trough_months": [6]},
    "Пистолеты термоклеевые":  {"peak_months": [10, 11, 12], "trough_months": [6, 7]},
}


def is_seasonal_category(category: str) -> bool:
    return category in SEASONAL_CATEGORIES


def get_seasonal_phase(category: str, ref_date: date) -> str:
    """Возвращает 'peak', 'trough', 'normal' или 'unknown'."""
    spec = SEASONAL_CATEGORIES.get(category)
    if not spec:
        return "unknown"
    m = ref_date.month
    if m in spec.get("peak_months", []):
        return "peak"
    if m in spec.get("trough_months", []):
        return "trough"
    return "normal"


# ============================================================
# ANOMALY_RULES — настройки детектора шумных дней
# ============================================================
ANOMALY_RULES = {
    "window_days": 7,                  # сколько закрытых дней брать для базы
    "mad_multiplier": 3,                # порог в MAD
    "applies_to_metrics": ["profit", "margin"],   # только эти метрики
    "applies_to_sources": ["mpstats"],            # не для воронки
    "min_history_points": 3,                      # без 3+ точек медиана недостоверна
}


def role_of(group: str) -> str:
    """Маппинг группы товара в методологическую роль.

    Поддерживает 2 формата:
      1. Чистая группа: "Фокус" → "Локомотив" через GROUP_TO_ROLE.
      2. Составная строка: "Фокус, Локомотив" — берём вторую часть как роль,
         если она есть в ROLE_NORMS.

    Реальная выгрузка WB использует формат №2.
    """
    if not group:
        return ""
    if ", " in group:
        parts = [p.strip() for p in group.split(",")]
        if len(parts) >= 2 and parts[1] in ROLE_NORMS:
            return parts[1]
        return GROUP_TO_ROLE.get(parts[0], parts[0])
    return GROUP_TO_ROLE.get(group, group)


def boost_for_sku(code: str) -> int:
    """Приоритетный буст для звёзд (+20) и локо-риска (+15). Иначе 0.

    v1.1: понижены с +30 / +25, чтобы давать разнообразие severity.
    """
    if code in STARS_CODES:
        return 20
    if code in LOCO_RISK_CODES:
        return 15
    return 0


def manager_of_star(code: str) -> str | None:
    for s in STARS:
        if s["code"] == code:
            return s["manager"]
    return None


def manager_of_loco_risk(code: str) -> str | None:
    for l in LOCO_RISK_LIST:
        if l["code"] == code:
            return l["manager"]
    return None
