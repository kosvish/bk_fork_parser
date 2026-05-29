"""
Кликает внутрь live-события на csgopositive.xyz и исследует структуру рынков.
Также перехватывает WS сообщения от odds-сокета.
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
            print(f"[WS odds] {ws.url}")
            def on_frame(payload):
                data = payload.body if hasattr(payload, "body") else str(payload)
                odds_messages.append(data)
                if not str(data).startswith("3") and not str(data).startswith("2"):
                    print(f"  [recv] {str(data)[:400]}")
            ws.on("framereceived", on_frame)

        page.on("websocket", on_websocket)

        await page.goto(URL, wait_until="load", timeout=60000)
        await asyncio.sleep(5)

        # Находим первое live событие
        live_event = await page.query_selector(".event.live_betting")
        if not live_event:
            print("Live событий не найдено!")
            await browser.close()
            return

        event_id = await live_event.get_attribute("data-id")
        app_id = await live_event.get_attribute("data-app_id")
        print(f"\nПервое live событие: data-id={event_id}, data-app_id={app_id}")

        # Читаем структуру карточки до клика
        card_html = await live_event.evaluate("el => el.outerHTML")
        (OUTPUT_DIR / "csgopositive_card_before.html").write_text(card_html, encoding="utf-8")
        print(f"HTML карточки сохранён")

        # Читаем коэффициенты с карточки (до клика)
        card_data = await page.evaluate(f"""
            () => {{
                const ev = document.querySelector('.event[data-id="{event_id}"]');
                if (!ev) return null;

                const teams = [];
                for (const side of ev.querySelectorAll('a.m_open')) {{
                    const name = side.querySelector('.team_name')?.innerText?.trim();
                    const oddsEl = side.querySelector('.bet_val, .coef, [class*="odds"], [class*="coef"], [class*="val"]');
                    const rawId = side.getAttribute('data-raw_id');
                    teams.push({{
                        name,
                        rawId,
                        oddsHtml: side.innerHTML.slice(0, 300),
                        allText: side.innerText?.trim()
                    }});
                }}

                // Ищем все числа похожие на коэффициенты
                const allSpans = Array.from(ev.querySelectorAll('span, div, b'))
                    .filter(el => /^\\d+\\.\\d+$/.test(el.innerText?.trim()))
                    .map(el => ({{
                        tag: el.tagName,
                        class: el.className,
                        text: el.innerText.trim(),
                        parent: el.parentElement?.className
                    }}));

                return {{ teams, allSpans }};
            }}
        """)
        print(f"\nДанные карточки:")
        if card_data:
            for t in card_data['teams']:
                print(f"  Команда: '{t['name']}' raw_id={t['rawId']}")
                print(f"    text: {t['allText']}")
            print(f"\n  Все числа (коэффициенты):")
            for s in card_data['allSpans']:
                print(f"    <{s['tag']} class='{s['class']}' parent='{s['parent']}'> {s['text']}")

        # Кликаем на левую команду (home)
        left_link = await live_event.query_selector("a.left.m_open, a.m_open:first-of-type")
        if left_link:
            raw_id = await left_link.get_attribute("data-raw_id")
            print(f"\nКликаем на левую команду (raw_id={raw_id})...")
            await left_link.click()
            await asyncio.sleep(3)

            # Что открылось?
            modal_html = await page.evaluate("""
                () => {
                    const modal = document.querySelector('.market_modal, .modal.active, [class*="bet_modal"], [class*="market"]');
                    return modal ? modal.outerHTML.slice(0, 3000) : 'Модал не найден';
                }
            """)
            (OUTPUT_DIR / "csgopositive_modal.html").write_text(modal_html, encoding="utf-8")
            print(f"HTML модала сохранён ({len(modal_html)} байт)")

            # Структура модала
            modal_data = await page.evaluate("""
                () => {
                    // Ищем открытый модал/панель с рынками
                    const containers = document.querySelectorAll(
                        '.market_modal, .bet_panel, [class*="market"], [class*="bet_list"], [class*="odds_list"]'
                    );
                    const results = [];
                    for (const c of containers) {
                        if (c.offsetParent === null && !c.closest('body')) continue; // невидимые
                        const rows = Array.from(c.querySelectorAll('[class*="bet"], [class*="market"], [class*="odd"]'));
                        if (rows.length > 0) {
                            results.push({
                                class: c.className,
                                innerText: c.innerText?.trim().slice(0, 500),
                                childCount: c.children.length
                            });
                        }
                    }
                    return results;
                }
            """)
            print(f"\nОткрытые контейнеры с рынками ({len(modal_data)}):")
            for m in modal_data:
                print(f"  class='{m['class']}' children={m['childCount']}")
                print(f"  text: {m['innerText']}")

            # Полный список видимых элементов на странице после клика
            visible_new = await page.evaluate("""
                () => {
                    const bets = document.querySelectorAll('.bets, .bet_list, .market_list, .odds');
                    return Array.from(bets).map(el => ({
                        class: el.className,
                        text: el.innerText?.trim().slice(0, 300)
                    }));
                }
            """)
            print(f"\n.bets/.bet_list/.market_list/.odds элементы после клика ({len(visible_new)}):")
            for v in visible_new:
                print(f"  '{v['class']}': {v['text']}")

        # Ждём WS сообщения ещё 15 секунд
        print(f"\nЖдём WS odds сообщения (15 сек)...")
        await asyncio.sleep(15)

        print(f"\nWS odds сообщений: {len(odds_messages)}")
        for msg in odds_messages:
            s = str(msg)
            if not s.startswith("3") and not s.startswith("2"):
                print(f"  {s[:400]}")

        (OUTPUT_DIR / "csgopositive_odds_ws.json").write_text(
            json.dumps(odds_messages, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8"
        )

        await browser.close()


asyncio.run(explore())
