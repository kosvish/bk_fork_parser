"""
Отчёт по памяти — запусти утром:
  python memory_report.py
"""

import os
from datetime import datetime

LOG_FILE = "memory_log.csv"


def parse_log():
    if not os.path.exists(LOG_FILE):
        print(f"Файл {LOG_FILE} не найден. Запусти сканер сначала.")
        return []

    rows = []
    with open(LOG_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("timestamp"):
                continue
            parts = line.split(",")
            if len(parts) < 3:
                continue
            try:
                ts      = datetime.strptime(parts[0], "%Y-%m-%d %H:%M:%S")
                total   = float(parts[1])
                peak    = float(parts[2])
                py_mb   = float(parts[3]) if len(parts) > 3 else 0
                chr_mb  = float(parts[4]) if len(parts) > 4 else 0
                cgp     = int(parts[5])   if len(parts) > 5 else 0
                wl      = int(parts[6])   if len(parts) > 6 else 0
                rows.append((ts, total, peak, py_mb, chr_mb, cgp, wl))
            except (ValueError, IndexError):
                continue
    return rows


def ascii_bar(value, max_val, width=40):
    filled = int(value / max_val * width) if max_val > 0 else 0
    return "█" * filled + "░" * (width - filled)


def ascii_chart(values, width=55, height=8):
    if not values:
        return ""
    mn, mx = min(values), max(values)
    if mx == mn:
        mx = mn + 1
    lines = []
    for row in range(height, 0, -1):
        threshold = mn + (mx - mn) * row / height
        step = max(1, len(values) // width)
        bar = "".join("█" if values[i] >= threshold else " "
                      for i in range(0, len(values), step))
        lines.append(f"  {threshold:6.0f} MB │{bar}")
    lines.append("         └" + "─" * (len(values) // max(1, len(values) // width)))
    return "\n".join(lines)


def main():
    rows = parse_log()
    if not rows:
        print("Данных нет.")
        return

    timestamps  = [r[0] for r in rows]
    total_vals  = [r[1] for r in rows]
    peak_vals   = [r[2] for r in rows]
    py_vals     = [r[3] for r in rows]
    chrome_vals = [r[4] for r in rows]

    start_ts  = timestamps[0]
    end_ts    = timestamps[-1]
    duration  = end_ts - start_ts
    hours     = max(duration.total_seconds() / 3600, 0.01)

    start_mb  = total_vals[0]
    end_mb    = total_vals[-1]
    peak_mb   = max(peak_vals)
    min_mb    = min(total_vals)
    avg_mb    = sum(total_vals) / len(total_vals)
    growth_mb = end_mb - start_mb
    gph       = growth_mb / hours  # growth per hour

    avg_py     = sum(py_vals) / len(py_vals)     if py_vals     else 0
    avg_chrome = sum(chrome_vals) / len(chrome_vals) if chrome_vals else 0

    print("=" * 65)
    print("           ОТЧЁТ ПО ПАМЯТИ СКАНЕРА")
    print("=" * 65)
    print(f"  Период:       {start_ts.strftime('%d.%m %H:%M')} → {end_ts.strftime('%d.%m %H:%M')}")
    print(f"  Длительность: {int(hours)}ч {int((hours % 1) * 60)}м  ({len(rows)} замеров)")
    print()
    print(f"  {'Показатель':<22} {'Значение':>10}")
    print(f"  {'-'*34}")
    print(f"  {'Старт (общее)':<22} {start_mb:>9.0f} МБ")
    print(f"  {'Финиш (общее)':<22} {end_mb:>9.0f} МБ")
    print(f"  {'Пик (общее)':<22} {peak_mb:>9.0f} МБ")
    print(f"  {'Среднее (общее)':<22} {avg_mb:>9.0f} МБ")
    print(f"  {'  из них Python':<22} {avg_py:>9.0f} МБ")
    print(f"  {'  из них Chrome':<22} {avg_chrome:>9.0f} МБ")
    print()

    # Вердикт
    if gph > 50:
        verdict = f"🔴 УТЕЧКА! +{gph:.0f} МБ/час → за сутки +{gph*24:.0f} МБ"
    elif gph > 25:
        verdict = f"🟡 Умеренный рост: +{gph:.0f} МБ/час → за сутки +{gph*24:.0f} МБ"
    elif gph > 10:
        verdict = f"🟢 Небольшой рост: +{gph:.0f} МБ/час — норма для браузера"
    else:
        verdict = f"🟢 Стабильно: {gph:+.1f} МБ/час — отлично"

    print(f"  Рост: {growth_mb:+.0f} МБ за сессию ({gph:+.1f} МБ/час)")
    print(f"  {verdict}")
    print()

    # Прогноз на 24 часа
    predicted_24h = end_mb + gph * (24 - hours)
    print(f"  Прогноз через 24ч от старта: ~{predicted_24h:.0f} МБ")
    if predicted_24h > 1500:
        print(f"  ⚠️  Может переполнить сервер с 2 ГБ RAM!")
    elif predicted_24h > 1000:
        print(f"  ⚡ Следи за памятью на сервере.")
    else:
        print(f"  ✅ Для 24ч работы всё в порядке.")
    print()

    # График
    print("  График общей RAM (МБ) по времени:")
    print()
    print(ascii_chart(total_vals))
    print()

    # Почасовая разбивка
    if hours >= 1:
        print(f"  {'Час':>4}  {'Python МБ':>10}  {'Chrome МБ':>10}  {'Итого МБ':>10}  {'Δ МБ':>8}")
        print("  " + "-" * 50)
        for h in range(int(hours) + 1):
            hr = [(ts, tot, py, ch) for ts, tot, _, py, ch, *_ in rows
                  if 0 <= (ts - start_ts).total_seconds() / 3600 - h < 1]
            if not hr:
                continue
            tots = [r[1] for r in hr]
            pys  = [r[2] for r in hr]
            chs  = [r[3] for r in hr]
            delta = tots[-1] - tots[0]
            print(f"  {h+1:>3}ч  "
                  f"{sum(pys)/len(pys):>10.0f}  "
                  f"{sum(chs)/len(chs):>10.0f}  "
                  f"{sum(tots)/len(tots):>10.0f}  "
                  f"{delta:>+8.0f}")

    print("=" * 65)
    print(f"\n  Полные данные: {LOG_FILE}  (можно открыть в Excel)")


if __name__ == "__main__":
    main()
