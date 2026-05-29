"""
Глубокое исследование Winline API:
1. Ловим ВСЕ запросы (не только JSON)
2. Ищем Angular-компоненты со ставками в DOM
3. Декодируем WS-фреймы (бинарный протокол)
4. Ищем REST-эндпоинты с odds/markets
"""

import asyncio
import gzip
import json
import re
from pathlib import Path
from playwright.async_api import async_playwright

OUTPUT_DIR = Path("explore_output")
LIVE_URL = "https://winline.ru/stavki/sport/kibersport"


async def explore():
    OUTPUT_DIR.mkdir(exist_ok=True)
    all_requests = []
    ws_frames_decoded = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        # Логируем ВСЕ запросы
        async def on_request(req):
            url = req.url
            # Только winline домены
            if "winline" in url or "wss.winline" in url:
                all_requests.append({"method": req.method, "url": url})

        async def on_response(resp):
            url = resp.url
            if not ("winline" in url):
                return
            ct = resp.headers.get("content-type", "")
            # Любые потенциально полезные ответы
            keywords = ["live", "odd", "market", "bet", "koef", "event", "cyber", "sport", "api"]
            if any(k in url.lower() for k in keywords) or "json" in ct:
                try:
                    body = await resp.text()
                    if len(body) > 10:
                        all_requests.append({
                            "type": "response",
                            "url": url,
                            "status": resp.status,
                            "ct": ct,
                            "body": body[:5000]
                        })
                except Exception:
                    pass

        page.on("request", on_request)
        page.on("response", on_response)

        # WS перехват
        def on_ws(ws):
            if "wss.winline" not in ws.url:
                return
            print(f"[WS] Подключён: {ws.url}")

            def on_frame(payload):
                try:
                    b = payload.body if hasattr(payload, "body") else bytes(payload)
                    dec = gzip.decompress(b)
                    # Ищем читаемый текст
                    text_parts = re.findall(rb'[\x20-\x7e\xc0-\xff]{4,}', dec)
                    readable = [p.decode("utf-8", errors="ignore") for p in text_parts]

                    # Ищем числа похожие на кэфы (1.xx - 9.xx) в бинарном виде
                    # Odds хранятся как uint16 little-endian, в единицах 0.01
                    odds_found = []
                    for i in range(len(dec) - 1):
                        val = int.from_bytes(dec[i:i+2], "little")
                        if 101 <= val <= 999:  # 1.01 - 9.99
                            odds_found.append(val / 100)

                    entry = {
                        "size": len(dec),
                        "readable": readable[:20],
                        "possible_odds": sorted(set(odds_found))[:20],
                        "hex_start": dec[:32].hex()
                    }
                    ws_frames_decoded.append(entry)

                    if readable:
                        print(f"[WS FRAME] size={len(dec)}, text={readable[:5]}, odds={sorted(set(odds_found))[:5]}")
                except Exception as e:
                    pass  # не gzip

            ws.on("framereceived", on_frame)

        page.on("websocket", on_ws)

        print("Загружаю страницу кибerspорта...")
        await page.goto(LIVE_URL, wait_until="load", timeout=60000)
        await asyncio.sleep(6)

        # Кликаем Сейчас
        seychas = await page.query_selector("button:has-text('Сейчас')")
        if seychas:
            await seychas.evaluate("el => el.click()")
            print("Клик 'Сейчас'")
            await asyncio.sleep(6)

        # Читаем Angular-компоненты с кэфами напрямую из JS-памяти
        print("\nЧитаю Angular-компоненты...")
        ng_data = await page.evaluate("""
            () => {
                const result = {};

                // Способ 1: ищем данные в ng.__ngContext__
                const allEls = document.querySelectorAll('[data-event-id], [ng-reflect-event-id], [ng-reflect-model]');
                result.ng_els = allEls.length;

                // Способ 2: читаем коэффициенты прямо из DOM
                const odds = [];
                document.querySelectorAll('.row-btn__coef, [class*="coef"], [class*="odds"], [class*="kof"]').forEach(el => {
                    const text = el.innerText.trim();
                    if (/^[1-9]\\.[0-9]{2}$/.test(text)) {
                        odds.push({
                            text,
                            class: el.className,
                            parentClass: el.parentElement?.className || '',
                            grandClass: el.parentElement?.parentElement?.className || ''
                        });
                    }
                });
                result.odds_in_dom = odds.slice(0, 30);

                // Способ 3: Angular root data
                try {
                    const root = document.querySelector('ww-app') || document.querySelector('app-root');
                    if (root && root.__ngContext__) {
                        result.has_ng_context = true;
                        // Пробуем получить компонент
                        const comp = window.ng && window.ng.getComponent && window.ng.getComponent(root);
                        result.ng_component_keys = comp ? Object.keys(comp).slice(0, 20) : [];
                    }
                } catch(e) {
                    result.ng_error = String(e);
                }

                // Способ 4: window.__store__ или аналоги
                const storeKeys = Object.keys(window).filter(k =>
                    k.includes('store') || k.includes('state') || k.includes('redux') || k.includes('ngrx')
                );
                result.global_stores = storeKeys;

                return result;
            }
        """)
        print(f"Angular данные: {json.dumps(ng_data, ensure_ascii=False, indent=2)}")

        # Ищем событие с кэфами напрямую на странице списка
        print("\nЧитаю кэфы прямо с DOM...")
        page_odds = await page.evaluate("""
            () => {
                // Полная структура карточки события
                const cards = [];
                const eventCards = document.querySelectorAll(
                    'ww-feature-block-tournament-dsk ww-feature-event-card-dsk, ' +
                    '.event-card, [class*="event-card"]'
                );
                for (const card of Array.from(eventCards).slice(0, 5)) {
                    const link = card.querySelector('a[href*="/stavki/event/"]');
                    const eventId = link ? link.href.split('/').pop() : null;
                    const teams = Array.from(card.querySelectorAll('[class*="team"], [class*="name"]'))
                        .map(el => el.innerText.trim()).filter(t => t.length > 0);
                    const odds = Array.from(card.querySelectorAll('span, button'))
                        .filter(el => /^[1-9]\\.[0-9]{2}$/.test(el.innerText.trim()))
                        .map(el => ({
                            text: el.innerText.trim(),
                            tag: el.tagName,
                            class: el.className.substring(0, 60)
                        }));
                    cards.push({eventId, teams: teams.slice(0, 4), odds: odds.slice(0, 6)});
                }
                return cards;
            }
        """)
        print(f"Карточки событий: {json.dumps(page_odds, ensure_ascii=False, indent=2)}")

        # Сохраняем всё
        out = {
            "all_requests": all_requests,
            "ws_frames": ws_frames_decoded,
        }
        out_path = OUTPUT_DIR / "winline_api2.json"
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nСохранено в {out_path}")

        # Уникальные API URL
        print("\n=== Уникальные Winline API URL ===")
        seen = set()
        for r in all_requests:
            if r.get("type") == "response":
                base = r["url"].split("?")[0]
                if base not in seen:
                    seen.add(base)
                    print(f"  {r['status']} {base}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(explore())
