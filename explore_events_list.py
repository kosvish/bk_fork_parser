"""
Исследует страницу списка событий: сохраняет HTML после скролла и показывает все найденные события.
"""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

EVENTS_LIST_URL = "https://winline.ru/stavki/sport/kibersport"
OUTPUT_DIR = Path("explore_output")

async def explore():
    OUTPUT_DIR.mkdir(exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 5000},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        await page.goto(EVENTS_LIST_URL, wait_until="load", timeout=60000)
        await asyncio.sleep(4)

        seychas = await page.query_selector("button:has-text('Сейчас')")
        if seychas:
            await seychas.evaluate("el => el.click()")
            await asyncio.sleep(3)

        # Скроллим до конца — ищем реальный скроллируемый контейнер
        await page.evaluate("""
            async () => {
                function findScrollable() {
                    const candidates = document.querySelectorAll(
                        'main, .content, [class*="content"], ' +
                        'ww-feature-block-sport-dsk, ww-feature-eventpage-dsk'
                    );
                    for (const el of candidates) {
                        const style = window.getComputedStyle(el);
                        if ((style.overflow === 'auto' || style.overflow === 'scroll' ||
                             style.overflowY === 'auto' || style.overflowY === 'scroll') &&
                            el.scrollHeight > el.clientHeight) {
                            return el;
                        }
                    }
                    return null;
                }
                const container = findScrollable() || document.documentElement;
                console.log('Scroll container:', container.tagName, container.className);

                await new Promise(resolve => {
                    let prev = -1;
                    const step = () => {
                        container.scrollBy(0, 600);
                        window.scrollBy(0, 600);
                        setTimeout(() => {
                            const cur = container.scrollTop + document.documentElement.scrollTop;
                            if (cur === prev) { container.scrollTo(0,0); window.scrollTo(0,0); resolve(); }
                            else { prev = cur; step(); }
                        }, 300);
                    };
                    step();
                });
            }
        """)
        await asyncio.sleep(2)

        # Сохраняем HTML
        html = await page.content()
        (OUTPUT_DIR / "events_list_scrolled.html").write_text(html, encoding="utf-8")
        print(f"HTML сохранён ({len(html)} байт)")

        # Считаем все event-ссылки
        all_links = await page.eval_on_selector_all(
            "a[href*='/stavki/event/']",
            "els => [...new Set(els.map(a => a.getAttribute('href')))]"
        )
        print(f"\nВсего уникальных event-ссылок: {len(all_links)}")
        for l in all_links:
            print(f"  {l}")

        # Все уникальные ww-* компоненты на странице
        components = await page.evaluate("""
            () => [...new Set(
                Array.from(document.querySelectorAll('*'))
                    .map(el => el.tagName.toLowerCase())
                    .filter(t => t.startsWith('ww-'))
            )]
        """)
        print(f"\nww-* компоненты на странице:")
        for c in components:
            count = await page.evaluate(f"() => document.querySelectorAll('{c}').length")
            print(f"  <{c}> × {count}")

        # Блоки ww-feature-block-tournament-dsk
        blocks_data = await page.evaluate("""
            () => {
                const blocks = document.querySelectorAll('ww-feature-block-tournament-dsk, ww-block-tournament-dsk');
                return Array.from(blocks).map(b => ({
                    tag: b.tagName.toLowerCase(),
                    title: b.querySelector('.block-tournament-header__title, .block-tournament__title')?.innerText?.trim() || '?',
                    eventCount: b.querySelectorAll('a[href*="/stavki/event/"]').length
                }));
            }
        """)
        print(f"\nТурнирные блоки ({len(blocks_data)}):")
        for b in blocks_data:
            print(f"  <{b['tag']}> '{b['title']}' — {b['eventCount']} событий")

        await browser.close()

asyncio.run(explore())
