#!/usr/bin/env python3
"""update.py — генерация AI-сводки через Gemini.

Запуск: python3 update.py

Что делает:
  1) Парсит ~/niti-dashboard/.env, читает GEMINI_API_KEY.
  2) Запускает build.py (для свежих data.json и месячных).
  3) Собирает payload: вчерашние цифры, топ-50 SKU за месяц,
     30-дневная история каждого, состояние звёзд.
  4) Дёргает gemini-1.5-flash:generateContent с промптом.
  5) Парсит JSON-ответ, сохраняет в data/ai_summary.json.
  6) Если ключа нет / запрос упал / ответ невалиден — пишет
     заглушку с пояснением, дашборд продолжает работать.
  7) Запускает build.py второй раз — встраивает свежую сводку в HTML.
"""
from __future__ import annotations

import csv
import json
import subprocess
import sys
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
DATA_DIR = ROOT / "data"
CSV_DIR = DATA_DIR / "csv-year"
PROCESSED_DIR = DATA_DIR / "processed"
AI_SUMMARY_FILE = DATA_DIR / "ai_summary.json"
DATA_JSON = ROOT / "data.json"

GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

# звёзды и локомотивы — фиксированный список приоритетов
STAR_CODES = ["КЛ007", "КЛ003", "ЛК013", "ПВС002"]
LOCO_RISK_CODES = ["ПС002", "ФМПНН0032", "ЮТ001", "ЮТ002"]

# показатели для истории SKU
DAILY_FILES = {
    "orders": "Дневная_Факт_заказов.csv",
    "revenue": "Дневная_Сумма_заказов.csv",
    "profit": "Дневная_Прибыль.csv",
    "stock": "Дневная_Остаток.csv",
    "spp": "Дневная_СПП.csv",
    "price": "Дневная_Цена_после_СПП.csv",
    "ad_spend": "Дневная_Рекламные_расходы.csv",
}


# ============================================================
# .env
# ============================================================
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


# ============================================================
# build.py
# ============================================================
def run_build():
    print("→ Запускаю build.py...")
    r = subprocess.run([sys.executable, "build.py"], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("  ✗ build.py упал:")
        print(r.stdout)
        print(r.stderr)
        sys.exit(1)
    # покажем последние строки
    last_lines = r.stdout.strip().splitlines()[-4:]
    for l in last_lines:
        print(f"    {l}")


# ============================================================
# Чтение дневных CSV (длинный формат: код, SKU, показатель, название, дата, значение)
# ============================================================
def read_daily_long(name):
    """Возвращает {code: {date: value}}."""
    path = CSV_DIR / name
    if not path.exists():
        return {}
    out = defaultdict(dict)
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            code = row.get("Артикул поставщика")
            date = row.get("Дата")
            try:
                v = float(row.get("Значение", "") or 0)
            except (TypeError, ValueError):
                continue
            if not code or not date:
                continue
            out[code][date] = v
    return dict(out)


def last_n_dates(daily_map, n=30):
    """Глобально берём n последних дат из любого артикула."""
    all_dates = set()
    for code, dates in daily_map.items():
        all_dates.update(dates.keys())
    return sorted(all_dates)[-n:]


# ============================================================
# Подготовка payload
# ============================================================
def build_payload():
    if not DATA_JSON.exists():
        raise SystemExit("data.json не найден — запусти build.py сначала.")
    data = json.loads(DATA_JSON.read_text(encoding="utf-8"))
    current_month = data["meta"].get("current_month", "2026-05")
    months_file = PROCESSED_DIR / "months" / f"{current_month}.json"
    sku_month = json.loads(months_file.read_text(encoding="utf-8")) if months_file.exists() else []

    # топ-50 по выручке за месяц
    sku_month = [r for r in sku_month if r.get("revenue") and r["revenue"] > 0]
    sku_month.sort(key=lambda r: r.get("revenue", 0), reverse=True)
    top50 = sku_month[:50]
    top50_codes = [r["code"] for r in top50]

    # дневные данные за 30 дней для топ-50
    daily_orders = read_daily_long(DAILY_FILES["orders"])
    daily_revenue = read_daily_long(DAILY_FILES["revenue"])
    daily_price = read_daily_long(DAILY_FILES["price"])
    daily_stock = read_daily_long(DAILY_FILES["stock"])
    daily_spp = read_daily_long(DAILY_FILES["spp"])
    daily_ad = read_daily_long(DAILY_FILES["ad_spend"])

    dates_30 = last_n_dates(daily_orders, n=30)
    last_date = dates_30[-1] if dates_30 else None
    prev_date = dates_30[-2] if len(dates_30) >= 2 else None
    week_ago_date = dates_30[-8] if len(dates_30) >= 8 else None

    def history_for(code):
        return {
            "orders": [daily_orders.get(code, {}).get(d) for d in dates_30],
            "revenue": [daily_revenue.get(code, {}).get(d) for d in dates_30],
            "price": [daily_price.get(code, {}).get(d) for d in dates_30],
            "stock": [daily_stock.get(code, {}).get(d) for d in dates_30],
            "spp": [daily_spp.get(code, {}).get(d) for d in dates_30],
            "ad_spend": [daily_ad.get(code, {}).get(d) for d in dates_30],
        }

    sku_history = []
    for r in top50:
        h = history_for(r["code"])
        # компактная сводка изменений (вчера vs неделю назад)
        def at(arr, d):
            try:
                idx = dates_30.index(d)
                return arr[idx]
            except (ValueError, IndexError):
                return None
        compact = {
            "code": r["code"],
            "name": r.get("name"),
            "category": r.get("category"),
            "manager": r.get("manager"),
            "group": r.get("group"),
            "month_revenue": r.get("revenue"),
            "month_profit": r.get("profit"),
            "month_margin": r.get("margin"),
            "month_drr": r.get("drr"),
            "month_orders": r.get("orders"),
            "stock_now": r.get("stock"),
            "turnover_days": r.get("turnover"),
            "history_30d": {
                "dates": dates_30,
                "orders": h["orders"],
                "revenue": h["revenue"],
                "price_after_spp": h["price"],
                "stock": h["stock"],
                "spp_pct": h["spp"],
                "ad_spend": h["ad_spend"],
            },
            "yesterday_vs_prev": {
                "orders": [at(h["orders"], prev_date), at(h["orders"], last_date)],
                "stock":  [at(h["stock"], prev_date),  at(h["stock"], last_date)],
                "spp":    [at(h["spp"], prev_date),    at(h["spp"], last_date)],
                "price":  [at(h["price"], prev_date),  at(h["price"], last_date)],
            },
            "vs_week_ago": {
                "orders": [at(h["orders"], week_ago_date), at(h["orders"], last_date)],
                "price":  [at(h["price"], week_ago_date),  at(h["price"], last_date)],
            },
        }
        sku_history.append(compact)

    # звёзды и локомотивы
    star_codes_in_top = [r for r in sku_history if r["code"] in STAR_CODES]
    # если каких-то звёзд нет в топ-50 — добавим их отдельно
    have = {r["code"] for r in star_codes_in_top}
    for code in STAR_CODES:
        if code in have:
            continue
        # ищем в полном sku_month
        match = next((r for r in sku_month if r["code"] == code), None)
        if match:
            h = history_for(code)
            star_codes_in_top.append({
                "code": code,
                "name": match.get("name"),
                "manager": match.get("manager"),
                "month_revenue": match.get("revenue"),
                "month_profit": match.get("profit"),
                "month_margin": match.get("margin"),
                "stock_now": match.get("stock"),
                "history_30d": {
                    "dates": dates_30,
                    "orders": h["orders"],
                    "revenue": h["revenue"],
                },
            })

    # вчерашняя сводка из data.json
    yesterday = data.get("yesterday")

    return {
        "month": current_month,
        "yesterday": yesterday,
        "company_targets": {"margin_target": 28.5, "drr_target": 5.5},
        "top50": sku_history,
        "stars": star_codes_in_top,
        "loco_risk": [
            {"code": c, **(data.get("oos_days") or {}).get(c, {})}
            for c in LOCO_RISK_CODES
        ],
    }


# ============================================================
# Промпт
# ============================================================
PROMPT_TEMPLATE = """Ты аналитик бренда "Нити Творчества" (товары для рукоделия на Wildberries). Пишешь ежедневную сводку для команды из 3 менеджеров: Виктория (бусины, аксессуары для бижутерии), Настя (аксессуары для рукоделия — тросики, лески, нити), Владимир (термопистолеты, стержни, проволоки).

ВАЖНЫЕ ОГРАНИЧЕНИЯ:
- НЕ начинай с "Привет", "Доброе утро", "Сегодня"
- НЕ благодари, НЕ желай удачи
- НЕ используй markdown заголовки # ##
- НЕ повторяй цифры которые уже видны на дашборде в блоке "За <дата>"
- ТОН верхнего блока (company_summary): формальный, как пишет руководитель отдела. Сжато, по делу, без воды.
- ТОН блоков менеджеров: суше, конкретнее
- ТОН задач (action): конкретное действие + обоснование на истории ("3 февраля было аналогично, после X продажи восстановились за 4 дня")
- Пиши на русском, на "ты" (внутренняя команда)

ПРИОРИТЕТЫ ДЛЯ ВЫБОРА ЗАДАЧ:
- 🔴 red: маржа<0 ИЛИ остаток=0 при продажах ИЛИ ДРР>1.5× нормы роли ИЛИ оборачиваемость>120д
- 🟡 yellow: маржа<нормы роли ИЛИ ДРР>нормы роли ИЛИ заказы упали >30% за неделю

Выбирай для tasks не более 10 артикулов, СТРОГО по важности. ОБЯЗАТЕЛЬНО включай в tasks звёзды плана если у них есть проблемы (КЛ007, КЛ003, ЛК013, ПВС002) — даже маленькие отклонения важны.

ДАННЫЕ ЗА ДЕНЬ:
__DAY_DATA__

ИСТОРИЯ ТОП-50 SKU за 30 дней:
__SKU_HISTORY__

ЗВЁЗДЫ ПЛАНА и их текущее состояние:
__STARS__

ВОЗВРАТИ JSON БЕЗ markdown-обёртки (никаких ```json):
{
  "company_summary": "5-7 строк формального текста",
  "manager_blocks": {
    "Виктория": "3-5 строк по её SKU",
    "Настя": "3-5 строк по её SKU",
    "Владимир": "3-5 строк по его SKU"
  },
  "tasks": [
    {
      "sku": "БП0215",
      "manager": "Виктория",
      "priority": "red",
      "problem": "Заказы упали с 22 до 13/день за 3 дня",
      "action": "Проверь СПП в кабинете WB. Если есть — активируй до 25%.",
      "action_extended": "Развёрнутое объяснение с историей SKU за 30 дней и ссылкой на похожие прошлые ситуации, для модалки SKU"
    }
  ],
  "generated_at": "__TS__"
}
"""


def build_prompt(payload):
    return (PROMPT_TEMPLATE
        .replace("__DAY_DATA__", json.dumps(payload["yesterday"], ensure_ascii=False, indent=2))
        .replace("__SKU_HISTORY__", json.dumps(payload["top50"], ensure_ascii=False, indent=1))
        .replace("__STARS__", json.dumps({"stars": payload["stars"], "loco_risk": payload["loco_risk"]}, ensure_ascii=False, indent=2))
        .replace("__TS__", datetime.now().isoformat(timespec="seconds"))
    )


# ============================================================
# Gemini API
# ============================================================
def _call_gemini_once(api_key, prompt, timeout=120):
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "responseMimeType": "application/json",
        },
    }
    req = urllib.request.Request(
        f"{GEMINI_URL}?key={api_key}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
        envelope = json.loads(raw)
        candidates = envelope.get("candidates") or []
        if not candidates:
            return None, f"Gemini вернул пустой candidates: {str(envelope)[:300]}", None
        text = "".join(p.get("text", "") for p in candidates[0].get("content", {}).get("parts", []))
        if not text.strip():
            return None, "Gemini вернул пустой текст", None
        try:
            return json.loads(text), None, None
        except json.JSONDecodeError as e:
            t = text.strip()
            if t.startswith("```"):
                t = t.strip("`").lstrip("json").strip()
                try:
                    return json.loads(t), None, None
                except Exception:
                    pass
            return None, f"Не удалось распарсить JSON ответа: {e}", text[:500]
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        return None, f"HTTP {e.code}: {err_body[:500]}", e.code
    except Exception as e:
        return None, f"{type(e).__name__}: {e}", None


def call_gemini(api_key, prompt):
    """Один вызов + retry на 429 (rate limit) с задержкой 30с."""
    import time
    res, err, extra = _call_gemini_once(api_key, prompt)
    if err and extra == 429:
        print("  ⏳ 429 rate limit. Жду 30 сек и повторяю один раз...")
        time.sleep(30)
        res, err, extra = _call_gemini_once(api_key, prompt)
    return res, err


def stub_summary(reason):
    return {
        "company_summary": (
            "AI-сводка не сгенерирована. "
            f"Причина: {reason}. "
            "Проверь ключ GEMINI_API_KEY в .env и запусти `python3 update.py` снова."
        ),
        "manager_blocks": {"Виктория": "", "Настя": "", "Владимир": ""},
        "tasks": [],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


# ============================================================
# Валидация ответа
# ============================================================
def validate(answer):
    if not isinstance(answer, dict):
        return None, "ответ не объект"
    required = ["company_summary", "manager_blocks", "tasks"]
    for k in required:
        if k not in answer:
            return None, f"в ответе нет поля {k}"
    if not isinstance(answer.get("manager_blocks"), dict):
        return None, "manager_blocks не объект"
    if not isinstance(answer.get("tasks"), list):
        return None, "tasks не массив"
    # нормализуем: дозаполняем недостающих менеджеров
    for m in ["Виктория", "Настя", "Владимир"]:
        answer["manager_blocks"].setdefault(m, "")
    answer.setdefault("generated_at", datetime.now().isoformat(timespec="seconds"))
    return answer, None


# ============================================================
# MAIN
# ============================================================
def main():
    print("=" * 60)
    print("UPDATE.PY — сборка дашборда + AI-сводка от Gemini")
    print("=" * 60)

    # 1. .env
    env = load_env()
    api_key = env.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("\n⚠ В .env нет GEMINI_API_KEY. Записываю заглушку и собираю дашборд без AI.")
        ai = stub_summary("ключ GEMINI_API_KEY не найден")
    else:
        print(f"\n✓ Ключ найден: {api_key[:10]}…{api_key[-4:]}")
        ai = None  # будет получен ниже

    # 2. первый прогон build.py
    print("\n[1/3] build.py для свежего data.json")
    run_build()

    # 3. payload + вызов
    if api_key:
        print("\n[2/3] Готовлю payload для Gemini...")
        try:
            payload = build_payload()
        except Exception as e:
            print(f"  ✗ Не удалось собрать payload: {e}")
            ai = stub_summary(f"ошибка сборки payload: {e}")
        else:
            print(f"  payload: топ-{len(payload['top50'])} SKU, звёзд {len(payload['stars'])}")
            prompt = build_prompt(payload)
            print(f"  размер промпта: {len(prompt)/1024:.1f} КБ")
            print(f"  → вызываю {GEMINI_MODEL}...")
            answer, err = call_gemini(api_key, prompt)
            if err:
                print(f"  ✗ {err}")
                ai = stub_summary(err.splitlines()[0][:200])
            else:
                clean, vErr = validate(answer)
                if vErr:
                    print(f"  ✗ Ответ не валиден: {vErr}")
                    ai = stub_summary(f"невалидный ответ Gemini: {vErr}")
                else:
                    print(f"  ✓ Получено: company_summary {len(clean['company_summary'])} симв, tasks {len(clean['tasks'])}")
                    ai = clean

    # 4. сохраняем AI-сводку
    AI_SUMMARY_FILE.parent.mkdir(parents=True, exist_ok=True)
    AI_SUMMARY_FILE.write_text(json.dumps(ai, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✓ Записал {AI_SUMMARY_FILE.relative_to(ROOT)} ({AI_SUMMARY_FILE.stat().st_size} байт)")

    # 5. второй прогон build.py — встроит свежий ai_summary в index.html
    print("\n[3/3] build.py для встраивания AI-сводки в HTML")
    run_build()

    print("\n✓ Готово. Открывай index.html.")


if __name__ == "__main__":
    main()
