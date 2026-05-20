#!/usr/bin/env python3
"""build_history.py — склеить 12 месячных parquet План_факт в long-format history.parquet.

Запуск один раз при добавлении нового месячного parquet (обычно раз в месяц).
Источник: data/Сжатые файлы НТ год/План_факт_*.parquet
Выход:   data/history.parquet

Long-format:
  code        str   — артикул поставщика
  sku         int   — WB SKU
  manager     str
  group       str
  category    str
  name        str
  indicator   str   — «Прибыль, руб», «Сумма заказов, руб» и т.д.
  date        str   — «DD.MM.YYYY»
  value       float

В data/ai_summary.json эта таблица не попадает (слишком большая).
Build.py будет читать её при сборке для выдачи sku_history и
для year-операторов движка.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PARQUET_DIR = ROOT / "data" / "Сжатые файлы НТ год"
OUT_FILE = ROOT / "data" / "history.parquet"


def main():
    print("=" * 60)
    print("build_history.py — годовая история из parquet")
    print("=" * 60)

    if not PARQUET_DIR.exists():
        print(f"✗ Папка не найдена: {PARQUET_DIR}")
        sys.exit(1)

    try:
        import pyarrow.parquet as pq
        import pandas as pd
    except ImportError as e:
        print(f"✗ Нужны pyarrow и pandas: {e}")
        sys.exit(1)

    files = sorted(PARQUET_DIR.glob("План_факт_*.parquet"))
    if not files:
        print(f"✗ В {PARQUET_DIR} нет parquet-файлов с префиксом 'План_факт_'")
        sys.exit(1)

    print(f"\nНайдено {len(files)} файлов:")
    for f in files:
        print(f"  {f.name} ({f.stat().st_size/1024:.0f} КБ)")

    frames = []
    total_rows_in = 0
    for path in files:
        try:
            t = pq.read_table(path)
        except Exception as e:
            print(f"  ✗ {path.name}: {e}")
            continue
        df = t.to_pandas()
        total_rows_in += len(df)

        # колонки-даты — все, что матчат "DD.MM.YYYY"
        date_cols = [c for c in df.columns
                     if re.match(r"^\d{2}\.\d{2}\.\d{4}$", str(c))]
        if not date_cols:
            print(f"  ⚠ {path.name}: нет дневных колонок, пропускаю")
            continue

        id_cols = [c for c in ["Артикул поставщика", "SKU", "Менеджер",
                               "Группа товаров", "Категория",
                               "Название товара", "Показатель"]
                   if c in df.columns]

        melted = df.melt(
            id_vars=id_cols,
            value_vars=date_cols,
            var_name="date",
            value_name="value",
        )
        # дропаем NaN
        melted = melted.dropna(subset=["value"])
        # переименуем под snake_case
        melted = melted.rename(columns={
            "Артикул поставщика": "code",
            "Менеджер": "manager",
            "Группа товаров": "group",
            "Категория": "category",
            "Название товара": "name",
            "Показатель": "indicator",
        })
        # remap Антон → Владимир (как в основном пайплайне)
        if "manager" in melted.columns:
            melted["manager"] = melted["manager"].replace({"Антон": "Владимир"})
        frames.append(melted)
        print(f"  ✓ {path.name}: {len(df):,} строк → {len(melted):,} long-точек")

    if not frames:
        print("\n✗ Не удалось ничего прочитать")
        sys.exit(1)

    print("\nСклеиваю...")
    big = pd.concat(frames, ignore_index=True)

    # дедупликация: одна строка на (code, indicator, date) — берём последнюю встретившуюся
    before = len(big)
    big = big.drop_duplicates(subset=["code", "indicator", "date"], keep="last")
    after = len(big)
    print(f"  дедуп: {before:,} → {after:,} (удалено {before - after:,})")

    # диагностика
    unique_codes = big["code"].nunique() if "code" in big.columns else 0
    unique_indicators = big["indicator"].nunique() if "indicator" in big.columns else 0
    dates_sorted = sorted(big["date"].unique(),
                          key=lambda s: tuple(int(p) for p in str(s).split(".")[::-1]))
    print(f"\nИтого:")
    print(f"  строк:              {len(big):,}")
    print(f"  уникальных SKU:     {unique_codes:,}")
    print(f"  показателей:        {unique_indicators}")
    print(f"  диапазон дат:       {dates_sorted[0]} … {dates_sorted[-1]}")

    print(f"\nСохраняю в {OUT_FILE.relative_to(ROOT)}...")
    big.to_parquet(OUT_FILE, compression="zstd", index=False)
    print(f"✓ Готово. Размер: {OUT_FILE.stat().st_size/1024/1024:.1f} МБ")


if __name__ == "__main__":
    main()
