"""
Проверяем: загружает ли короткая ссылка /stavki/event/{id} контент события в headless браузере?
"""
import asyncio
from playwright.async_api import async_playwright

SHORT_URL = "https://winline.ru/stavki/event/15582676"

async def test():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        print(f"Навигация на {SHORT_URL}")
        await page.goto(SHORT_URL, wait_until="load", timeout=60000)

        # Ждём максимум 15 секунд пока URL изменится или появятся .odd-btn
        for i in range(15):
            await asyncio.sleep(1)
            current_url = page.url
            odd_btns = await page.query_selector_all(".odd-btn")
            print(f"  [{i+1}s] URL: {current_url} | .odd-btn найдено: {len(odd_btns)}")
            if len(odd_btns) > 0 or "stavki/event" not in current_url:
                break

        print(f"\nИтоговый URL: {page.url}")
        await browser.close()

asyncio.run(test())
