"""
Перехватывает ответ /lib/bets.php при клике на команду.
Также ищет турнирное название в DOM карточки.
"""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

URL = "https://csgopositive.xyz/"
COOKIES_FILE = Path("cookies_csgopositive.json")
OUTPUT_DIR = Path("explore_output")


async def explore():
    OUTPUT_DIR.mkdir(exist_ok=True)
    cookies = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))

    bets_responses = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 5000},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        await context.add_cookies(cookies)
        page = await context.new_page()

        # Перехватываем ответы bets.php
        async def on_response(response):
            if "bets.php" in response.url:
                try:
                    body = await response.text()
                    bets_responses.append({
                        "url": response.url,
                        "status": response.status,
                        "body": body
                    })
                    print(f"\n[bets.php] Ответ ({response.status}):")
                    print(body[:2000])
                except Exception as e:
                    print(f"[bets.php] Ошибка чтения: {e}")

        page.on("response", on_response)

        await page.goto(URL, wait_until="load", timeout=60000)
        await asyncio.sleep(5)

        # Читаем подробную структуру DOM первого live события
        live_events = await page.query_selector_all(".event.live_betting")
        print(f"Live событий: {len(live_events)}")

        if live_events:
            for i, ev in enumerate(live_events[:3]):
                eid = await ev.get_attribute("data-id")
                app_id = await ev.get_attribute("data-app_id")

                # Полный HTML карточки для изучения структуры
                html = await ev.evaluate("el => el.outerHTML")
                (OUTPUT_DIR / f"card_{eid}.html").write_text(html, encoding="utf-8")

                # Ищем турнир внутри карточки
                card_text = await page.evaluate(f"""
                    () => {{
                        const ev = document.querySelector('.event[data-id="{eid}"]');
                        if (!ev) return {{}};

                        // Все элементы с их классами и текстом
                        const allEls = Array.from(ev.querySelectorAll('*'))
                            .filter(el => el.children.length === 0 && (el.innerText || '').trim())
                            .map(el => ({{
                                tag: el.tagName,
                                class: el.className,
                                text: el.innerText?.trim()
                            }}));

                        return {{ allEls }};
                    }}
                """)
                print(f"\n--- Карточка {eid} (app={app_id}) ---")
                for el in card_text.get('allEls', []):
                    print(f"  <{el['tag']} class='{el['class']}'> '{el['text']}'")

        # Кликаем на team_id=1 первого события
        if live_events:
            ev = live_events[0]
            eid = await ev.get_attribute("data-id")
            left = await ev.query_selector("a.left.m_open")
            if left:
                print(f"\nКликаем на левую команду события {eid}...")
                await left.click()
                await asyncio.sleep(4)

            # Кликаем на правую команду
            await page.keyboard.press("Escape")
            await asyncio.sleep(1)

            right = await ev.query_selector("a.right.m_open")
            if right:
                raw_id = await right.get_attribute("data-raw_id")
                print(f"\nКликаем на правую команду (raw_id={raw_id})...")
                await right.click()
                await asyncio.sleep(4)

            await page.keyboard.press("Escape")
            await asyncio.sleep(1)

        # Сохраняем все ответы
        (OUTPUT_DIR / "bets_php_responses.json").write_text(
            json.dumps(bets_responses, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        print(f"\nВсего bets.php ответов: {len(bets_responses)}")

        await browser.close()


asyncio.run(explore())
