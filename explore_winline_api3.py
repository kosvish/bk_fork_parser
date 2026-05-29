"""
Финальная разведка: читаем структуру карточек события на странице-списке.
Ищем data-атрибуты кнопок коэффициентов и HTML-структуру карточки.
"""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

OUTPUT_DIR = Path("explore_output")
LIVE_URL = "https://winline.ru/stavki/sport/kibersport"


async def explore():
    OUTPUT_DIR.mkdir(exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        await page.goto(LIVE_URL, wait_until="load", timeout=60000)
        await asyncio.sleep(5)

        seychas = await page.query_selector("button:has-text('Сейчас')")
        if seychas:
            await seychas.evaluate("el => el.click()")
            await asyncio.sleep(5)

        # Детальная структура первой карточки
        result = await page.evaluate("""
            () => {
                const cards = [];
                // Все event-карточки
                const cardEls = document.querySelectorAll('a[href*="/stavki/event/"]');
                const seen = new Set();
                for (const link of cardEls) {
                    const eventId = link.href.split('/').pop();
                    if (seen.has(eventId)) continue;
                    seen.add(eventId);

                    // Ищем ближайший контейнер карточки
                    const card = link.closest('[class*="card"], [class*="event"], ww-feature-event-card-dsk') || link.parentElement;

                    // Все атрибуты карточки
                    const cardAttrs = {};
                    for (const attr of card.attributes || []) {
                        cardAttrs[attr.name] = attr.value;
                    }

                    // Команды
                    const teamEls = card.querySelectorAll('[class*="team__name"], [class*="team-name"], [class*="name__"]');
                    const teams = Array.from(teamEls).map(el => el.innerText.trim());

                    // Период/карта
                    const periodEls = card.querySelectorAll('[class*="period"], [class*="map"], [class*="current"]');
                    const periods = Array.from(periodEls).map(el => ({
                        text: el.innerText.trim(),
                        class: el.className
                    }));

                    // Все кнопки коэффициентов с их атрибутами
                    const btnEls = card.querySelectorAll('.coefficient-button, [class*="coefficient-button"]');
                    const buttons = Array.from(btnEls).map(btn => {
                        const attrs = {};
                        for (const a of btn.attributes) attrs[a.name] = a.value;
                        return {
                            text: btn.innerText.trim(),
                            class: btn.className,
                            attrs,
                            parent_class: btn.parentElement?.className || ''
                        };
                    });

                    // HTML карточки (первые 2000 символов)
                    const html = card.outerHTML.substring(0, 3000);

                    cards.push({ eventId, teams, periods, buttons, cardAttrs, html });
                    if (cards.length >= 3) break;
                }
                return cards;
            }
        """)

        out_path = OUTPUT_DIR / "winline_cards.json"
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Сохранено {len(result)} карточек в {out_path}")

        for card in result:
            print(f"\n=== Event {card['eventId']} ===")
            print(f"Teams: {card['teams']}")
            print(f"Periods: {card['periods']}")
            print(f"CardAttrs: {card['cardAttrs']}")
            print(f"Buttons ({len(card['buttons'])}):")
            for b in card['buttons']:
                print(f"  text={b['text']!r:8} class_tail={b['class'].split()[-1]:40} attrs={b['attrs']}")
            print(f"HTML snippet:\n{card['html'][:500]}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(explore())
