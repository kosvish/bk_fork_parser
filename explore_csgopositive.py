"""
Исследует csgopositive.xyz: загружает куки, изучает DOM текущих матчей
и перехватывает WebSocket сообщения odds-сокета.
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

    odds_messages = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 5000},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        await context.add_cookies(cookies)

        page = await context.new_page()

        def on_websocket(ws):
            if "odds" not in ws.url:
                return
            print(f"[WS odds] Подключение: {ws.url}")

            def on_frame(payload):
                data = payload.body if hasattr(payload, "body") else str(payload)
                odds_messages.append(data)
                print(f"  [recv] {str(data)[:300]}")

            ws.on("framereceived", on_frame)

        page.on("websocket", on_websocket)

        await page.goto(URL, wait_until="load", timeout=60000)
        await asyncio.sleep(15)

        # Сохраняем HTML
        html = await page.content()
        (OUTPUT_DIR / "csgopositive_main.html").write_text(html, encoding="utf-8")

        # Исследуем структуру карточек событий
        print("\n=== Структура .event карточек (первые 5) ===")
        events_data = await page.evaluate("""
            () => {
                const events = Array.from(document.querySelectorAll('.event')).slice(0, 5);
                return events.map(el => ({
                    outerHTML: el.outerHTML.slice(0, 500),
                    classes: el.className,
                    dataAttrs: Object.fromEntries(
                        Array.from(el.attributes)
                            .filter(a => a.name.startsWith('data-'))
                            .map(a => [a.name, a.value])
                    ),
                    text: el.innerText?.trim().slice(0, 200)
                }));
            }
        """)
        for i, ev in enumerate(events_data):
            print(f"\n--- .event #{i+1} ---")
            print(f"  classes: {ev['classes']}")
            print(f"  data-attrs: {ev['dataAttrs']}")
            print(f"  text: {ev['text']}")
            print(f"  html: {ev['outerHTML']}")

        # Ищем секцию "Текущие матчи" по тексту
        print("\n=== Поиск секции текущих матчей ===")
        sections = await page.evaluate("""
            () => {
                const all = Array.from(document.querySelectorAll('*'));
                return all
                    .filter(el => {
                        const t = (el.innerText || '').trim();
                        return (t.includes('Текущие') || t.includes('текущие') || t.includes('Live') || t.includes('live'))
                               && t.length < 50;
                    })
                    .map(el => ({
                        tag: el.tagName,
                        class: el.className,
                        text: el.innerText?.trim()
                    }))
                    .slice(0, 10);
            }
        """)
        for s in sections:
            print(f"  <{s['tag']} class='{s['class']}'> '{s['text']}'")

        # Смотрим все [class*="match"] элементы
        print("\n=== [class*='match'] элементы ===")
        matches = await page.evaluate("""
            () => Array.from(document.querySelectorAll('[class*="match"]')).slice(0, 5).map(el => ({
                tag: el.tagName,
                class: el.className,
                dataAttrs: Object.fromEntries(
                    Array.from(el.attributes)
                        .filter(a => a.name.startsWith('data-'))
                        .map(a => [a.name, a.value])
                ),
                text: el.innerText?.trim().slice(0, 150)
            }))
        """)
        for m in matches:
            print(f"  <{m['tag']} class='{m['class']}'>")
            print(f"    data: {m['dataAttrs']}")
            print(f"    text: {m['text']}")

        # Сохраняем WS сообщения
        (OUTPUT_DIR / "csgopositive_odds_ws.json").write_text(
            json.dumps(odds_messages, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        print(f"\nOdds WS сообщений: {len(odds_messages)}, сохранены в explore_output/csgopositive_odds_ws.json")

        await browser.close()


asyncio.run(explore())
