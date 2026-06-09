# capture_bets.py — run: python capture_bets.py
import asyncio, json
from pathlib import Path
from playwright.async_api import async_playwright

COOKIES = Path("cookies_csgopositive.json")

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=False)
        ctx = await b.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        if COOKIES.exists():
            raw = json.loads(COOKIES.read_text(encoding="utf-8"))
            # некоторые экспортёры пишут sameSite как null / "unspecified" / "no_restriction"
            fix = {"strict": "Strict", "lax": "Lax", "none": "None",
                   "no_restriction": "None", "unspecified": "Lax"}
            for c in raw:
                ss = str(c.get("sameSite", "")).lower()
                c["sameSite"] = fix.get(ss, "Lax")  # по умолчанию Lax
                # подчищаем поля, которые иногда ломают add_cookies
                if c.get("expires") in (None, -1):
                    c.pop("expires", None)
            await ctx.add_cookies(raw)
            print(f"[ok] cookies loaded: {len(raw)}")
        else:
            print("[!] cookies_csgopositive.json not found — залогинься вручную в окне")

        pg = await ctx.new_page()

        async def on_req(req):
            if "bets.php" in req.url:
                print("\n=== REQUEST ===")
                print("URL :", req.url)
                print("METHOD:", req.method)
                print("POST:", req.post_data)

        async def on_resp(resp):
            if "bets.php" in resp.url:
                try:
                    print("\n=== RESPONSE (first 4000 chars) ===")
                    print((await resp.text())[:4000])
                except Exception as e:
                    print("read err", e)

        pg.on("request", on_req)
        pg.on("response", on_resp)

        await pg.goto("https://csgopositive.xyz/", wait_until="load")
        print("\n>>> If logged in: click a LIVE match currently on MAP 1.")
        print(">>> Watch this console for REQUEST + RESPONSE. Ctrl+C when captured.\n")
        await asyncio.sleep(180)

asyncio.run(main())