"""
Собирает все уникальные bet_type из WS odds-сокета за 2 минуты.
Также читает структуру модала для первого live-события.
"""
import asyncio
import json
import re
from pathlib import Path
from playwright.async_api import async_playwright

URL = "https://csgopositive.xyz/"
COOKIES_FILE = Path("cookies_csgopositive.json")
OUTPUT_DIR = Path("explore_output")


async def explore():
    OUTPUT_DIR.mkdir(exist_ok=True)
    cookies = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))

    # event_id -> {bet_type -> {koef_1, koef_2, status}}
    all_koefs: dict[str, dict] = {}

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
            def on_frame(payload):
                data = payload.body if hasattr(payload, "body") else str(payload)
                s = str(data)
                m = re.match(r'42\["koef_change",(\{.*\})\]', s)
                if not m:
                    return
                try:
                    obj = json.loads(m.group(1))
                    eid = obj.get("id", "?")
                    bt = obj.get("bet_type", "?")
                    if eid not in all_koefs:
                        all_koefs[eid] = {}
                    all_koefs[eid][bt] = {
                        "koef_1": obj.get("koef_1"),
                        "koef_2": obj.get("koef_2"),
                        "status": obj.get("status"),
                    }
                except Exception:
                    pass
            ws.on("framereceived", on_frame)

        page.on("websocket", on_websocket)

        await page.goto(URL, wait_until="load", timeout=60000)
        await asyncio.sleep(5)

        # Читаем все live события из DOM
        events_dom = await page.evaluate("""
            () => {
                const events = Array.from(document.querySelectorAll('.event.live_betting'));
                return events.map(ev => {
                    const left = ev.querySelector('a.left.m_open, a.m_open:first-of-type');
                    const right = ev.querySelector('a.right.m_open, a.m_open:last-of-type');
                    const getTeam = (el) => el ? {
                        name: el.querySelector('.team_name')?.innerText?.trim() || '',
                        rawId: el.getAttribute('data-raw_id'),
                        odds: el.querySelector('.sum.odds_icon')?.innerText?.trim() || '',
                        suspended: !!el.querySelector('.sum.odds_icon.deactivated'),
                    } : null;

                    // Турнир
                    const tournEl = ev.querySelector('.tournament_name, .league_name, [class*="tourn"], [class*="league"]');
                    const tourn = tournEl?.innerText?.trim() || '';

                    return {
                        id: ev.getAttribute('data-id'),
                        appId: ev.getAttribute('data-app_id'),
                        tournament: tourn,
                        home: getTeam(left),
                        away: getTeam(right),
                    };
                });
            }
        """)
        print(f"Live событий в DOM: {len(events_dom)}")
        for ev in events_dom:
            print(f"  [{ev['id']}] app={ev['appId']} '{ev.get('tournament')}' | {ev['home']['name'] if ev['home'] else '?'} vs {ev['away']['name'] if ev['away'] else '?'}")

        # Кликаем в первое live событие — читаем модал
        live_events = await page.query_selector_all(".event.live_betting")
        if live_events:
            first_ev = live_events[0]
            eid = await first_ev.get_attribute("data-id")
            left_link = await first_ev.query_selector("a.left.m_open, a.m_open:first-of-type")
            if left_link:
                await left_link.click()

                # Ждём пока "......" (загрузка) исчезнет из модала
                try:
                    await page.wait_for_function(
                        """() => {
                            const modal = document.querySelector('.modal.bets');
                            if (!modal) return false;
                            const text = modal.innerText || '';
                            // Ждём пока появится реальный контент (коэффициент или "нет ставок")
                            return text.includes('Победа') || text.includes('Нет') ||
                                   text.includes('победа') || /\\d+\\.\\d+/.test(text);
                        }""",
                        timeout=8000
                    )
                except Exception:
                    pass  # Таймаут — читаем что есть
                await asyncio.sleep(1)

                # Читаем содержимое открытого модала
                modal_data = await page.evaluate("""
                    () => {
                        const modal = document.querySelector('.modal.bets.fixed_odds, .modal.bets');
                        if (!modal) return null;
                        return {
                            fullText: modal.innerText?.trim(),
                            fullHtml: modal.outerHTML.slice(0, 8000),
                        };
                    }
                """)
                if modal_data:
                    print(f"\n=== Модал события {eid} ===")
                    print(f"Текст:\n{modal_data['fullText']}")
                    (OUTPUT_DIR / "csgopositive_modal_full.html").write_text(
                        modal_data['fullHtml'], encoding="utf-8"
                    )
                    print(f"\nHTML модала сохранён")

                # Закрываем через Escape
                await page.keyboard.press("Escape")
                await asyncio.sleep(1)

        # Ждём 2 минуты, собираем все bet_types
        print(f"\nСобираем bet_types 2 минуты...")
        await asyncio.sleep(120)

        # Итог
        all_bet_types: set[str] = set()
        for eid, bts in all_koefs.items():
            all_bet_types.update(bts.keys())

        print(f"\n=== Все уникальные bet_type ({len(all_bet_types)}) ===")
        for bt in sorted(all_bet_types):
            print(f"  {bt}")

        print(f"\n=== По событиям ===")
        for eid, bts in sorted(all_koefs.items()):
            print(f"\n  [{eid}]:")
            for bt, vals in sorted(bts.items()):
                status = "✓" if str(vals['status']) == "1" else "✗"
                print(f"    {status} {bt}: {vals['koef_1']} / {vals['koef_2']}")

        # Сохраняем
        (OUTPUT_DIR / "csgopositive_bet_types.json").write_text(
            json.dumps(all_koefs, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        print(f"\nСохранено в explore_output/csgopositive_bet_types.json")

        await browser.close()


asyncio.run(explore())
