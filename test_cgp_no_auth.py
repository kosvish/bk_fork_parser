"""
Тест: работает ли WS csgopositive без авторизации?
Открываем страницу без куков и ждём koef_change фреймы 60 секунд.
"""
import asyncio
import re
import json
from playwright.async_api import async_playwright

URL = "https://csgopositive.xyz/"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu"]
        )
        # Контекст БЕЗ куков
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        ws_connected = []
        koef_frames = []
        all_frames = []

        def on_ws(ws):
            print(f"[WS] Подключён: {ws.url}")
            ws_connected.append(ws.url)

            def on_frame(payload):
                body = payload.body if hasattr(payload, "body") else str(payload)
                s = str(body)
                all_frames.append(s[:100])

                if "koef_change" in s:
                    koef_frames.append(s[:300])
                    print(f"[koef_change] {s[:200]}")
                elif len(all_frames) <= 20:
                    print(f"[frame] {s[:80]}")

            ws.on("framereceived", on_frame)
            ws.on("close", lambda: print(f"[WS] Закрыт: {ws.url}"))

        page.on("websocket", on_ws)

        print("Открываю csgopositive.xyz без куков...")
        await page.goto(URL, wait_until="load", timeout=60000)
        print("Страница загружена. Жду WS фреймы 60 секунд...")
        await asyncio.sleep(60)

        print(f"\n=== РЕЗУЛЬТАТ ===")
        print(f"WS соединений: {len(ws_connected)}")
        print(f"Всего фреймов: {len(all_frames)}")
        print(f"koef_change фреймов: {len(koef_frames)}")

        if koef_frames:
            print("\nПример koef_change:")
            for f in koef_frames[:3]:
                print(f"  {f}")
            print("\n✓ WS РАБОТАЕТ БЕЗ АВТОРИЗАЦИИ!")
        else:
            print("\n✗ koef_change фреймов нет — нужна авторизация или ставки закрыты")
            print("Первые фреймы:")
            for f in all_frames[:10]:
                print(f"  {f}")

        await browser.close()


asyncio.run(main())
