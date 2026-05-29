"""
Перехватывает все сетевые запросы Winline (XHR/fetch/WS) при открытии
страницы киберспорта. Цель — найти API эндпоинты для получения списка
событий и коэффициентов без лишних браузерных вкладок.
"""

import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

EVENTS_URL = "https://winline.ru/stavki/sport/kibersport"
OUTPUT_DIR = Path("explore_output")


async def explore():
    OUTPUT_DIR.mkdir(exist_ok=True)
    api_calls = []
    ws_frames = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        # Перехватываем все XHR/fetch ответы
        async def on_response(response):
            url = response.url
            ct = response.headers.get("content-type", "")
            # Только JSON и интересные API
            if "json" in ct or any(k in url for k in ["/api/", "/stavki/", "graphql", "sport"]):
                try:
                    body = await response.text()
                    if len(body) > 5:
                        entry = {"url": url, "status": response.status, "body": body[:3000]}
                        api_calls.append(entry)
                        print(f"[RESP] {response.status} {url[:100]}")
                except Exception:
                    pass

        page.on("response", on_response)

        # Перехватываем WebSocket
        def on_websocket(ws):
            print(f"[WS OPEN] {ws.url}")
            ws_frames.append({"event": "open", "url": ws.url})

            def on_frame(payload):
                body = payload.body if hasattr(payload, "body") else str(payload)
                if body and len(body) < 2000:
                    ws_frames.append({"event": "frame", "url": ws.url, "body": body})
                    print(f"[WS FRAME] {ws.url.split('/')[-1]}: {str(body)[:120]}")

            ws.on("framereceived", on_frame)
            ws.on("framesent", lambda p: ws_frames.append({"event": "sent", "body": str(p)[:200]}))

        page.on("websocket", on_websocket)

        print("Открываю страницу киберспорта...")
        await page.goto(EVENTS_URL, wait_until="load", timeout=60000)
        await asyncio.sleep(5)

        # Кликаем Сейчас
        seychas = await page.query_selector("button:has-text('Сейчас')")
        if seychas:
            await seychas.evaluate("el => el.click()")
            print("Кликнул 'Сейчас'")
            await asyncio.sleep(5)

        # Читаем список событий из DOM чтобы взять реальный event_id
        events = await page.evaluate("""
            () => {
                const links = Array.from(document.querySelectorAll('a[href*="/stavki/event/"]'));
                return [...new Set(links.map(a => a.href))].slice(0, 3);
            }
        """)
        print(f"Нашёл ссылки на события: {events}")

        if events:
            print(f"\nОткрываю первое событие: {events[0]}")
            await page.goto(events[0], wait_until="load", timeout=60000)
            await asyncio.sleep(8)

        print(f"\nСобрано API-ответов: {len(api_calls)}")
        print(f"WS-кадров: {len(ws_frames)}")

        # Сохраняем результаты
        result = {"api_calls": api_calls, "ws_frames": ws_frames}
        out_file = OUTPUT_DIR / "winline_api.json"
        out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nСохранено в {out_file}")

        # Краткий вывод уникальных URL
        print("\n=== Уникальные API URL ===")
        seen = set()
        for c in api_calls:
            base = c["url"].split("?")[0]
            if base not in seen:
                seen.add(base)
                print(f"  {c['status']} {base}")

        print("\n=== WS эндпоинты ===")
        ws_urls = {f["url"] for f in ws_frames if "url" in f}
        for u in ws_urls:
            print(f"  {u}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(explore())
