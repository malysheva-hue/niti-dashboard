#!/usr/bin/env python3
"""Сборка дашборда «Нити Творчества» — Этап 1.

Читает:
  data/csv-year/*.csv          — историческая база (12 месяцев)
  inputs/*.xlsx                — свежак (план-факт мая, дневные воронки, шаблон цен)
  data/daily_summary.md        — ежедневная сводка от РОПа

Пишет:
  data.json                    — общий лёгкий срез (KPI, агрегаты, OOS, цены, сводка)
  data/processed/months/*.json — полный срез SKU за каждый месяц
  index.html                   — встраивает копию data.json в маркер /* APP_DATA_PLACEHOLDER */
"""
from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CSV_DIR = ROOT / "data" / "csv-year"
INPUTS_DIR = ROOT / "inputs"
PROCESSED_DIR = ROOT / "data" / "processed"
MONTHS_DIR = PROCESSED_DIR / "months"
DAILY_SUMMARY_MD = ROOT / "data" / "daily_summary.md"
INDEX_HTML = ROOT / "index.html"
DATA_JSON = ROOT / "data.json"

INCOMPLETE_MONTHS = {"2025-06", "2026-05"}
NO_MANAGERS_MONTHS = {"2026-02"}
DEFAULT_MONTH = "2026-04"

NAME_NORMALIZE = {
    "Цигельникова Виктория": "Виктория",
}

STAR_CODES = {"КЛ007", "КЛ003", "ЛК013", "ПВС002"}
LOCO_RISK_CODES = {"ПС002", "ФМПНН0032", "ЮТ001", "ЮТ002"}
ACTIVE_MANAGERS = {"Виктория", "Настя", "Антон", "Владимир"}


def num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if s in ("", "-", "Не указан план", "—"):
        return None
    s = s.replace(",", ".").replace("\xa0", "").replace(" ", "")
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def round_n(v, ndigits=2):
    if v is None:
        return None
    return round(v, ndigits)


def parse_summary_csv():
    out = []
    with open(CSV_DIR / "00_Сводка_по_месяцам.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            m = row.get("Месяц")
            if not m:
                continue
            out.append({
                "month": m,
                "revenue": num(row.get("Сумма заказов, руб")),
                "orders": num(row.get("Факт заказов, шт")),
                "profit": num(row.get("Прибыль, руб")),
                "ad_spend": num(row.get("Рекламные расходы, руб")),
                "frozen": num(row.get("Замороженный капитал")),
                "margin": num(row.get("Маржа %")),
                "drr": num(row.get("ДРР %")),
                "incomplete": m in INCOMPLETE_MONTHS,
            })
    return out


def parse_managers_csv():
    out = defaultdict(dict)
    with open(CSV_DIR / "01_Менеджеры_по_месяцам.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            m = row.get("Месяц")
            mgr = row.get("Менеджер")
            if not m or not mgr:
                continue
            mgr = NAME_NORMALIZE.get(mgr, mgr)
            out[m][mgr] = {
                "revenue": num(row.get("Сумма заказов, руб")),
                "orders": num(row.get("Факт заказов, шт")),
                "profit": num(row.get("Прибыль, руб")),
                "ad_spend": num(row.get("Рекламные расходы, руб")),
                "frozen": num(row.get("Замороженный капитал")),
                "margin": num(row.get("Маржа %")),
            }
    return dict(out)


def parse_categories_csv():
    out = defaultdict(list)
    with open(CSV_DIR / "03_Категории_по_месяцам.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            m = row.get("Месяц")
            if not m:
                continue
            out[m].append({
                "name": row.get("Категория"),
                "revenue": num(row.get("Сумма заказов, руб")),
                "orders": num(row.get("Факт заказов, шт")),
                "profit": num(row.get("Прибыль, руб")),
                "ad_spend": num(row.get("Рекламные расходы, руб")),
                "frozen": num(row.get("Замороженный капитал")),
            })
    return dict(out)


def parse_sku_year_csv():
    out = []
    with open(CSV_DIR / "04_SKU_итоги_года.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out.append({
                "code": row.get("Артикул поставщика"),
                "sku": row.get("SKU"),
                "name": row.get("Название товара"),
                "category": row.get("Категория"),
                "revenue": num(row.get("Сумма заказов, руб")),
                "orders": num(row.get("Факт заказов, шт")),
                "profit": num(row.get("Прибыль, руб")),
                "ad_spend": num(row.get("Рекламные расходы, руб")),
                "frozen": num(row.get("Замороженный капитал")),
                "margin": num(row.get("Маржа %")),
                "drr": num(row.get("ДРР %")),
            })
    return out


def parse_sku_monthly_csv():
    out = {}
    for path in sorted(CSV_DIR.glob("SKU_*.csv")):
        m = re.search(r"SKU_(\d{4}-\d{2})\.csv", path.name)
        if not m:
            continue
        month = m.group(1)
        rows = []
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                mgr = row.get("Менеджер") or ""
                mgr = NAME_NORMALIZE.get(mgr, mgr)
                rows.append({
                    "code": row.get("Артикул поставщика"),
                    "sku": row.get("SKU"),
                    "name": row.get("Название товара"),
                    "category": row.get("Категория"),
                    "group": row.get("Группа товаров") or "",
                    "manager": mgr,
                    "revenue": num(row.get("Сумма заказов, руб")),
                    "orders": num(row.get("Факт заказов, шт")),
                    "profit": num(row.get("Прибыль, руб")),
                    "margin": num(row.get("Маржинальность")),
                    "ad_spend": num(row.get("Рекламные расходы, руб")),
                    "drr": num(row.get("ДРР от рекламных продаж")),
                    "tacos": num(row.get("ДРР от продаж")),
                    "stock": num(row.get("Остаток")),
                    "turnover": num(row.get("Оборачиваемость, дней")),
                    "frozen": num(row.get("Замороженный капитал")),
                })
        out[month] = rows
    return out


def parse_plan_fact_xlsx():
    """Самый свежий План_факт_*.xlsx → {code: {indicator: {totals, day_str: value}}}."""
    import openpyxl
    candidates = sorted(INPUTS_DIR.glob("План_факт_*.xlsx"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return {}, None
    src = candidates[0]
    wb = openpyxl.load_workbook(src, read_only=True, data_only=True)
    ws = wb["План-Факт"]

    header = None
    data = defaultdict(dict)
    current_code = None

    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            header = row
            continue
        code = row[2]
        if code:
            current_code = code
        if not current_code:
            continue
        indicator = row[7]
        if not indicator:
            continue
        row_data = {"totals": row[8]}
        for c in range(9, len(row)):
            day_label = header[c] if c < len(header) else None
            if day_label:
                row_data[str(day_label)] = row[c]
        data[current_code][indicator] = row_data

    wb.close()
    return dict(data), src.name


def parse_funnels_xlsx():
    """Дневные воронки → {
        'by_code': {date: {code: {shows,ctr,clicks,carts}}}  # ТОЛЬКО для звёзд и топ-локо
        'overall': {date: {shows, clicks, carts, ctr_avg}}    # суммарно по всем SKU за день
    }."""
    import openpyxl
    targeted = STAR_CODES | LOCO_RISK_CODES
    by_code = {}
    overall = {}
    for path in sorted(INPUTS_DIR.glob("*воронка*.xlsx")):
        m = re.search(r"с (\d+)-(\d+)-(\d+) по", path.name)
        if not m:
            continue
        day_iso = f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb["Товары"]
        rows = list(ws.iter_rows(values_only=True))
        if len(rows) < 3:
            wb.close()
            continue
        header = rows[1]
        idx = {h: i for i, h in enumerate(header) if h}
        day_data = {}
        sum_shows = sum_clicks = sum_carts = 0
        ctr_values = []
        for r in rows[2:]:
            code = r[idx["Артикул продавца"]] if "Артикул продавца" in idx else None
            if not code:
                continue
            shows = num(r[idx["Показы"]]) if "Показы" in idx else None
            clicks = num(r[idx["Переходы в карточку"]]) if "Переходы в карточку" in idx else None
            carts = num(r[idx["Положили в корзину"]]) if "Положили в корзину" in idx else None
            ctr = num(r[idx["CTR"]]) if "CTR" in idx else None
            if shows: sum_shows += shows
            if clicks: sum_clicks += clicks
            if carts: sum_carts += carts
            if ctr is not None: ctr_values.append(ctr)
            if code in targeted:
                day_data[code] = {"shows": shows, "ctr": ctr, "clicks": clicks, "carts": carts}
        by_code[day_iso] = day_data
        overall[day_iso] = {
            "shows": int(sum_shows),
            "clicks": int(sum_clicks),
            "carts": int(sum_carts),
            "ctr_avg": round_n(sum(ctr_values)/len(ctr_values), 2) if ctr_values else None,
        }
        wb.close()
    return {"by_code": by_code, "overall": overall}


def parse_prices_xlsx():
    """Шаблон цен → {code: {price, discount, stock, turnover}}."""
    import openpyxl
    candidates = sorted(INPUTS_DIR.glob("Шаблон обновления цен*.xlsx"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return {}, None
    src = candidates[0]
    wb = openpyxl.load_workbook(src, data_only=True)
    ws = wb["Лист1"]
    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 2:
        wb.close()
        return {}, src.name
    header = rows[0]
    idx = {h: i for i, h in enumerate(header) if h}
    out = {}
    for r in rows[1:]:
        code = r[idx["Артикул продавца"]] if "Артикул продавца" in idx else None
        if not code:
            continue
        s_wb = num(r[idx["Остатки WB"]]) if "Остатки WB" in idx else None
        s_seller = num(r[idx["Остатки продавца"]]) if "Остатки продавца" in idx else None
        stock_total = None
        if s_wb is not None or s_seller is not None:
            stock_total = (s_wb or 0) + (s_seller or 0)
        out[code] = {
            "price": num(r[idx["Текущая цена"]]) if "Текущая цена" in idx else None,
            "discount": num(r[idx["Текущая скидка"]]) if "Текущая скидка" in idx else None,
            "price_with_discount": num(r[idx["Цена со скидкой"]]) if "Цена со скидкой" in idx else None,
            "stock_wb": s_wb,
            "stock_seller": s_seller,
            "stock_total": stock_total,
            "turnover": num(r[idx["Оборачиваемость"]]) if "Оборачиваемость" in idx else None,
        }
    wb.close()
    return out, src.name


def merge_plan_fact_into_may(plan_fact, sku_may):
    """План-Факт перебивает метрики мая."""
    INDICATOR_TO_FIELD = {
        "Сумма заказов, руб": "revenue",
        "Факт заказов, шт": "orders",
        "Прибыль, руб": "profit",
        "Маржинальность": "margin",
        "Рекламные расходы, руб": "ad_spend",
        "ДРР от рекламных продаж": "drr",
        "ДРР от продаж": "tacos",
        "Остаток": "stock",
        "Оборачиваемость, дней": "turnover",
        "Замороженный капитал": "frozen",
    }
    by_code = {row["code"]: row for row in sku_may}
    for code, indicators in plan_fact.items():
        target = by_code.get(code)
        if not target:
            continue
        for indicator, field in INDICATOR_TO_FIELD.items():
            v = num(indicators.get(indicator, {}).get("totals"))
            if v is not None:
                target[field] = v
    return list(by_code.values())


def compute_oos(plan_fact, prices):
    """Дни до OOS для TOP_LOCOMOTIVES_RISK."""
    last7 = [f"{d:02d}.05.2026" for d in range(13, 20)]
    out = {}
    for code in LOCO_RISK_CODES:
        stock = None
        if code in prices:
            stock = prices[code].get("stock_total")
        if stock is None and code in plan_fact:
            stock = num(plan_fact[code].get("Остаток", {}).get("totals"))
        avg7 = None
        if code in plan_fact:
            fakt = plan_fact[code].get("Факт заказов, шт", {})
            vals = [num(fakt.get(k)) for k in last7]
            vals = [v for v in vals if v is not None]
            if vals:
                avg7 = sum(vals) / len(vals)
        days_left = None
        if stock is not None and avg7 and avg7 > 0:
            days_left = stock / avg7
        out[code] = {
            "stock": stock,
            "avg7": round_n(avg7, 1),
            "days_left": round_n(days_left, 1),
        }
    return out


def compute_top_by_manager(sku_year, sku_monthly):
    """Подмерживаем менеджера к 04_SKU_итоги_года из последнего месяца,
    где артикул встречался. Возвращаем top-5 best/worst для каждого активного менеджера."""
    # 1. Карта code → manager (из самого свежего месяца, где артикул есть)
    code_to_manager = {}
    for month in sorted(sku_monthly.keys(), reverse=True):
        for row in sku_monthly[month]:
            code = row.get("code")
            mgr = row.get("manager")
            if code and mgr and code not in code_to_manager:
                code_to_manager[code] = mgr

    # 2. Раскидываем 04_итоги по менеджерам
    by_mgr = defaultdict(list)
    for r in sku_year:
        mgr = code_to_manager.get(r.get("code"))
        if mgr in ACTIVE_MANAGERS:
            by_mgr[mgr].append({
                "code": r.get("code"),
                "name": r.get("name"),
                "category": r.get("category"),
                "revenue": int(r["revenue"]) if r.get("revenue") is not None else None,
                "profit": int(r["profit"]) if r.get("profit") is not None else None,
                "margin": round_n(r.get("margin"), 1),
                "drr": round_n(r.get("drr"), 1),
            })

    # 3. Топ-5 по прибыли и худшие 5 по сумме «низкая маржа + высокий ДРР»
    out = {}
    for mgr, items in by_mgr.items():
        with_profit = [x for x in items if x["profit"] is not None]
        best5 = sorted(with_profit, key=lambda x: x["profit"], reverse=True)[:5]

        def worst_score(x):
            # чем меньше маржа и больше ДРР, тем хуже. Игнорим None.
            margin = x["margin"] if x["margin"] is not None else 0
            drr = x["drr"] if x["drr"] is not None else 0
            return drr - margin

        worst5 = sorted(items, key=worst_score, reverse=True)[:5]
        out[mgr] = {"best5": best5, "worst5": worst5}
    return out


def compute_stars(sku_monthly):
    """Срез по 4 артикулам-звёздам для каждого месяца."""
    out = {}
    for month, rows in sku_monthly.items():
        by_code = {r["code"]: r for r in rows}
        month_data = {}
        for code in STAR_CODES:
            if code in by_code:
                r = by_code[code]
                month_data[code] = {
                    "revenue": r.get("revenue"),
                    "profit": r.get("profit"),
                    "margin": r.get("margin"),
                    "tacos": r.get("tacos"),
                    "drr": r.get("drr"),
                    "stock": r.get("stock"),
                    "turnover": r.get("turnover"),
                    "manager": r.get("manager"),
                    "group": r.get("group"),
                }
        out[month] = month_data
    return out


def read_daily_summary():
    if DAILY_SUMMARY_MD.exists():
        return DAILY_SUMMARY_MD.read_text(encoding="utf-8")
    return ("# Сводка дня\n\n"
            "_Файл `data/daily_summary.md` не найден. "
            "Создайте его — он будет показан здесь._\n")


def latest_mtime():
    paths = list(CSV_DIR.glob("*.csv")) + list(INPUTS_DIR.glob("*.xlsx"))
    if not paths:
        return datetime.now().isoformat(timespec="seconds")
    return datetime.fromtimestamp(max(p.stat().st_mtime for p in paths)).isoformat(timespec="seconds")


INT_FIELDS = {"revenue", "orders", "profit", "ad_spend", "frozen", "stock"}
DEC1_FIELDS = {"margin", "drr", "tacos", "turnover"}


def compact_sku_row(row):
    """Урезаем числа до int/1 знака — экономия места в months/*.json."""
    out = {}
    for k, v in row.items():
        if k in INT_FIELDS and isinstance(v, (int, float)):
            out[k] = int(round(v))
        elif k in DEC1_FIELDS and isinstance(v, (int, float)):
            out[k] = round(v, 1)
        else:
            out[k] = v
    return out


def emit(common_data, months_data):
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    MONTHS_DIR.mkdir(parents=True, exist_ok=True)

    compact_months = {m: [compact_sku_row(r) for r in rows] for m, rows in months_data.items()}

    with open(DATA_JSON, "w", encoding="utf-8") as f:
        json.dump(common_data, f, ensure_ascii=False, separators=(",", ":"))

    for month, rows in compact_months.items():
        with open(MONTHS_DIR / f"{month}.json", "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, separators=(",", ":"))

    if INDEX_HTML.exists():
        html = INDEX_HTML.read_text(encoding="utf-8")
        common_json = json.dumps(common_data, ensure_ascii=False, separators=(",", ":"))
        months_json = json.dumps(compact_months, ensure_ascii=False, separators=(",", ":"))
        html = html.replace("/* APP_DATA_PLACEHOLDER */", common_json)
        html = html.replace("/* MONTHS_DATA_PLACEHOLDER */", months_json)
        INDEX_HTML.write_text(html, encoding="utf-8")


def main():
    print("[1/8] CSV-агрегаты...")
    summary = parse_summary_csv()
    managers = parse_managers_csv()
    categories = parse_categories_csv()
    sku_year = parse_sku_year_csv()

    print("[2/8] Месячные SKU (12 файлов)...")
    sku_monthly = parse_sku_monthly_csv()

    print("[3/8] inputs/План-Факт...")
    plan_fact, pf_name = parse_plan_fact_xlsx()
    print(f"        источник: {pf_name}, артикулов: {len(plan_fact)}")

    print("[4/8] inputs/Цены...")
    prices, prices_name = parse_prices_xlsx()
    print(f"        источник: {prices_name}, артикулов: {len(prices)}")

    print("[5/8] inputs/Воронки...")
    funnels = parse_funnels_xlsx()
    print(f"        дней с воронками: {len(funnels['overall'])} ({sorted(funnels['overall'].keys())})")

    print("[6/8] Перебиваем май свежим планом-фактом...")
    if "2026-05" in sku_monthly and plan_fact:
        sku_monthly["2026-05"] = merge_plan_fact_into_may(plan_fact, sku_monthly["2026-05"])

    print("[7/8] OOS + срезы звёзд + топы по менеджерам...")
    oos = compute_oos(plan_fact, prices)
    stars = compute_stars(sku_monthly)
    top_by_manager = compute_top_by_manager(sku_year, sku_monthly)

    print("[8/8] daily_summary.md и финальная сборка...")
    summary_md = read_daily_summary()

    common = {
        "meta": {
            "last_updated": latest_mtime(),
            "current_month": DEFAULT_MONTH,
            "incomplete_months": sorted(INCOMPLETE_MONTHS),
            "no_managers_months": sorted(NO_MANAGERS_MONTHS),
            "available_months": [r["month"] for r in summary],
            "sources": {
                "plan_fact": pf_name,
                "prices": prices_name,
                "funnels_days": sorted(funnels["overall"].keys()),
            },
        },
        "monthly_totals": summary,
        "manager_monthly": managers,
        "category_monthly": categories,
        "top_by_manager": top_by_manager,
        "stars_monthly": stars,
        "oos_days": oos,
        "prices": prices,
        "funnels": funnels,
        "daily_summary_md": summary_md,
    }

    emit(common, sku_monthly)

    total = DATA_JSON.stat().st_size
    months_total = sum(p.stat().st_size for p in MONTHS_DIR.glob("*.json"))
    print()
    print(f"data.json: {total/1024:.1f} КБ")
    print(f"data/processed/months/*.json: {months_total/1024:.1f} КБ "
          f"({len(list(MONTHS_DIR.glob('*.json')))} файлов)")
    print(f"Суммарно: {(total+months_total)/1024/1024:.2f} МБ")


if __name__ == "__main__":
    main()
