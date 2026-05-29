"""
Читаем структуру карточек: card__competitors + card__coeffs — сиблинги.
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

        result = await page.evaluate("""
            () => {
                const cards = [];

                // card__coeffs содержит кэфы; ищем его сиблинга card__competitors
                const coeffsDivs = document.querySelectorAll('.card__coeffs');
                console.log('Found card__coeffs:', coeffsDivs.length);

                for (const coeffsDiv of coeffsDivs) {
                    // Родитель должен содержать и card__competitors
                    const parent = coeffsDiv.parentElement;
                    const compDiv = parent ? parent.querySelector('.card__competitors') : null;

                    // EventId из ссылки
                    const link = compDiv ? compDiv.querySelector('a[href*="/stavki/event/"]') : null;
                    const eventId = link ? link.href.split('/').pop() : null;

                    // Команды
                    const nameEls = compDiv ? compDiv.querySelectorAll('.name') : [];
                    const teams = Array.from(nameEls).map(el => el.innerText.trim()).filter(t => t);

                    // Период (заголовок над коэффициентами)
                    const marketHeaders = coeffsDiv.querySelectorAll('[class*="market__header"], [class*="market__title"], [class*="period"]');
                    const periods = Array.from(marketHeaders).map(el => el.innerText.trim());

                    // Все market-блоки
                    const marketBlocks = coeffsDiv.querySelectorAll('.card__market');
                    const markets = [];
                    for (const mb of marketBlocks) {
                        // Заголовок этого рынка
                        const header = mb.previousElementSibling;
                        const headerText = header ? header.innerText.trim() : '';

                        const btns = mb.querySelectorAll('[class*="coefficient-button"]');
                        const btnData = Array.from(btns).map(btn => ({
                            text: btn.innerText.trim(),
                            class: btn.className,
                            // все data-атрибуты
                            data: Object.fromEntries(
                                Array.from(btn.attributes)
                                     .filter(a => a.name.startsWith('data-') || a.name.startsWith('ng-reflect'))
                                     .map(a => [a.name, a.value])
                            )
                        }));

                        // Тип рынка по классу кнопки
                        const firstBtnClass = btns[0]?.className || '';
                        const marketType = firstBtnClass.includes('generic2') ? 'winner' :
                                           firstBtnClass.includes('handicap2') ? 'handicap' :
                                           firstBtnClass.includes('total2') ? 'total' : 'other';
                        markets.push({ headerText, marketType, btns: btnData });
                    }

                    // Полный HTML родителя
                    const html = parent ? parent.outerHTML.substring(0, 1500) : '';

                    cards.push({ eventId, teams, periods, markets, html });
                    if (cards.length >= 5) break;
                }

                return { count: coeffsDivs.length, cards };
            }
        """)

        out_path = OUTPUT_DIR / "winline_cards2.json"
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Всего карточек на странице: {result['count']}")
        print(f"Сохранено в {out_path}")

        for card in result['cards']:
            print(f"\n=== Event {card['eventId']} teams={card['teams']} ===")
            for m in card['markets']:
                btns = [(b['text'], b['class'].split()[-1]) for b in m['btns']]
                print(f"  [{m['marketType']:8}] header={m['headerText']!r:20} btns={btns}")
            if card['html']:
                print(f"HTML[0:400]:\n{card['html'][:400]}\n")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(explore())
