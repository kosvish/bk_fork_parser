"""
Открывает браузер на csgopositive.xyz, ждёт авторизации.
После Enter в терминале сохраняет куки в cookies_csgopositive.json.
"""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

URL = "https://csgopositive.xyz/"
COOKIES_FILE = Path("cookies_csgopositive.json")


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        await page.goto(URL)

        print("Авторизуйся на сайте, затем нажми Enter...")
        input()

        cookies = await context.cookies()
        COOKIES_FILE.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Куки сохранены в {COOKIES_FILE} ({len(cookies)} шт.)")

        await browser.close()


asyncio.run(main())
