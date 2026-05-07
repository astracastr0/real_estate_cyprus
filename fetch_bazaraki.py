import asyncio
from playwright.async_api import async_playwright


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,  # headed mode bypasses some checks
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720},
            locale="en-US",
        )

        page = await context.new_page()

        # Remove webdriver flag
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        """)

        print("Navigating to bazaraki.com...")
        await page.goto("https://www.bazaraki.com/", wait_until="domcontentloaded")

        # Wait longer for Cloudflare challenge to resolve
        print("Waiting for Cloudflare challenge to pass...")
        for i in range(20):
            await page.wait_for_timeout(2000)
            title = await page.title()
            print(f"  [{i*2}s] Title: {title}")
            if "just a moment" not in title.lower():
                break

        title = await page.title()
        url = page.url
        print(f"\nFinal title: {title}")
        print(f"Final URL: {url}")

        content = await page.inner_text("body")
        print("\n--- Page Content (first 5000 chars) ---")
        print(content[:5000])

        await browser.close()


asyncio.run(main())
