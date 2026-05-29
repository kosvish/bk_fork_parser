"""
Скрипт-исследователь DOM структуры Winline.
Сохраняет HTML для анализа селекторов.
"""

import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

EVENTS_URL = "https://winline.ru/stavki/sport/kibersport"
EXAMPLE_EVENT_URL = "https://winline.ru/stavki/sport/kibersport/dota_2/european_pro_league/15582676"
OUTPUT_DIR = Path("explore_output")


async def explore():
    OUTPUT_DIR.mkdir(exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)  # headless=False чтобы видеть что происходит
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # --- Шаг 1: Страница со всеми событиями ---
        print("Открываю страницу кибerspорта...")
        await page.goto(EVENTS_URL, wait_until="load", timeout=60000)
        await asyncio.sleep(4)

        # Ищем таб "Сейчас"
        print("Ищу таб 'Сейчас'...")
        await page.screenshot(path=str(OUTPUT_DIR / "1_before_click.png"))

        # Сохраняем HTML до клика чтобы найти таб
        html_before = await page.content()
        (OUTPUT_DIR / "1_events_before_click.html").write_text(html_before, encoding="utf-8")
        print(f"  HTML до клика сохранён ({len(html_before)} байт)")

        # Ищем все элементы которые могут быть табами
        tabs = await page.query_selector_all("button, a, li, span")
        print(f"  Найдено {len(tabs)} элементов, ищу 'Сейчас'...")

        seychas_element = None
        for el in tabs:
            text = await el.inner_text()
            if "сейчас" in text.lower() or "сейчас" in text.lower():
                tag = await el.evaluate("el => el.tagName")
                print(f"  Кандидат: <{tag}> '{text.strip()}'")
                seychas_element = el

        if seychas_element:
            print("Кликаю на 'Сейчас'...")
            await seychas_element.click()
            await asyncio.sleep(3)

            await page.screenshot(path=str(OUTPUT_DIR / "2_after_click.png"))
            html_after = await page.content()
            (OUTPUT_DIR / "2_events_after_click.html").write_text(html_after, encoding="utf-8")
            print(f"  HTML после клика сохранён ({len(html_after)} байт)")
        else:
            print("  Таб 'Сейчас' не найден автоматически — смотри скриншот 1_before_click.png")

        # --- Шаг 2: Страница конкретного события ---
        print(f"\nОткрываю страницу события...")
        event_page = await context.new_page()
        await event_page.goto(EXAMPLE_EVENT_URL, wait_until="load", timeout=60000)
        await asyncio.sleep(3)  # Даём время на полный рендер

        await event_page.screenshot(path=str(OUTPUT_DIR / "3_event_page.png"))
        html_event = await event_page.content()
        (OUTPUT_DIR / "3_event_page.html").write_text(html_event, encoding="utf-8")
        print(f"  HTML события сохранён ({len(html_event)} байт)")

        # Ищем элементы с коэффициентами (обычно содержат числа типа 1.85, 2.10)
        print("  Ищу элементы с коэффициентами...")
        odds_candidates = await event_page.evaluate("""
            () => {
                const results = [];
                const allElements = document.querySelectorAll('*');
                for (const el of allElements) {
                    const text = el.innerText || '';
                    // Ищем числа похожие на коэффициенты (1.xx - 9.xx)
                    if (/^[1-9]\\.[0-9]{2}$/.test(text.trim())) {
                        results.push({
                            tag: el.tagName,
                            text: text.trim(),
                            className: el.className,
                            id: el.id,
                            parentClass: el.parentElement?.className || ''
                        });
                    }
                }
                return results.slice(0, 30); // первые 30
            }
        """)

        print(f"  Найдено {len(odds_candidates)} элементов похожих на коэффициенты:")
        for el in odds_candidates:
            print(f"    <{el['tag']}> text='{el['text']}' class='{el['className']}' parentClass='{el['parentClass']}'")

        await browser.close()
        print(f"\nГотово! Все файлы в папке '{OUTPUT_DIR}/'")
        print("Открой скриншоты и HTML файлы для анализа.")


if __name__ == "__main__":
    asyncio.run(explore())
