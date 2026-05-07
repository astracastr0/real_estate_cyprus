import asyncio
import re
import sys
import urllib.request

# ── 1. Sotheby's (plain HTTP) ──────────────────────────────────────────────
def check_sothebys():
    print("=" * 80)
    print("1. SOTHEBY'S (plain HTTP)")
    print("=" * 80)
    url = "https://www.cyprus-sothebysrealty.com/property/3-Bedroom-Apartment-in-Larnaca-f37001"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
            print(f"Status: {resp.status}, HTML length: {len(html)}")

        patterns = {
            "lat/lng attributes": re.findall(r'(?:data-)?(?:lat|lng|latitude|longitude)\s*[=:]\s*["\']?([0-9.\-]+)', html, re.I),
            "LatLng()": re.findall(r'LatLng\(([^)]+)\)', html, re.I),
            "google maps iframe": re.findall(r'<iframe[^>]*(?:maps|google)[^>]*>', html, re.I),
            "google maps embed": re.findall(r'maps\.google[^"\'<>\s]{0,200}', html, re.I),
            "google.com/maps": re.findall(r'google\.com/maps[^"\'<>\s]{0,200}', html, re.I),
            "maps.googleapis": re.findall(r'maps\.googleapis[^"\'<>\s]{0,200}', html, re.I),
            "Cyprus-range coords (34.xx or 33.xx)": re.findall(r'(?<![0-9])3[34]\.\d{3,7}(?!\d)', html),
            "coord JSON keys": re.findall(r'["\'](?:lat|lng|latitude|longitude|coordinates?|location)["\']:\s*["\']?([0-9.\-]+)', html, re.I),
            "mapboxgl/leaflet": re.findall(r'(?:mapboxgl|leaflet|L\.map|L\.marker)[^;\n]{0,100}', html, re.I),
            "center: [": re.findall(r'center\s*:\s*\[[^\]]{5,40}\]', html, re.I),
        }
        found_any = False
        for label, matches in patterns.items():
            if matches:
                found_any = True
                print(f"  FOUND {label}:")
                for m in matches[:10]:
                    print(f"    -> {m[:200]}")
        if not found_any:
            print("  No coordinate patterns found.")
            map_words = re.findall(r'(?i)(map|coord|latit|longit|geoloc)', html)
            if map_words:
                print(f"  But found map-related words: {set(map_words)}")
            else:
                print("  No map-related words at all in the HTML.")
    except Exception as e:
        print(f"  ERROR: {e}")

# ── 2. Bazaraki (Playwright stealth) ───────────────────────────────────────
async def check_bazaraki():
    from playwright.async_api import async_playwright
    print("\n" + "=" * 80)
    print("2. BAZARAKI (Playwright stealth)")
    print("=" * 80)
    url = "https://www.bazaraki.com/adv/6238571_2-bedroom-penthouse-for-sale/"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = { runtime: {} };
        """)
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="networkidle", timeout=45000)
            await page.wait_for_timeout(3000)
            html = await page.content()
            print(f"  Page loaded, HTML length: {len(html)}")

            patterns = {
                "lat/lng attributes": re.findall(r'(?:data-)?(?:lat|lng|latitude|longitude)\s*[=:]\s*["\']?([0-9.\-]+)', html, re.I),
                "LatLng()": re.findall(r'LatLng\(([^)]+)\)', html, re.I),
                "google maps iframe": re.findall(r'<iframe[^>]*(?:maps|google)[^>]*>', html, re.I),
                "google.com/maps": re.findall(r'google\.com/maps[^"\'<>\s]{0,200}', html, re.I),
                "maps.googleapis": re.findall(r'maps\.googleapis[^"\'<>\s]{0,300}', html, re.I),
                "Cyprus-range coords": re.findall(r'(?<![0-9])3[34]\.\d{3,7}(?!\d)', html),
                "coord JSON keys": re.findall(r'["\'](?:lat|lng|latitude|longitude|coordinates?)["\']:\s*["\']?([0-9.\-]+)', html, re.I),
                "center: [": re.findall(r'center\s*:\s*\[[^\]]{5,40}\]', html, re.I),
                "mapboxgl/leaflet": re.findall(r'(?:mapboxgl|leaflet|L\.map|L\.marker)[^;\n]{0,100}', html, re.I),
                "data-lat / data-lng": re.findall(r'data-(?:lat|lng|latitude|longitude)=["\']([^"\']+)', html, re.I),
                "initMap / showMap": re.findall(r'(?:initMap|showMap|renderMap|drawMap)[^;\n]{0,150}', html, re.I),
            }
            found_any = False
            for label, matches in patterns.items():
                if matches:
                    found_any = True
                    print(f"  FOUND {label}:")
                    for m in matches[:10]:
                        print(f"    -> {m[:300]}")
            if not found_any:
                print("  No coordinate patterns found in HTML.")

            print("\n  -- Checking JS variables --")
            js_checks = await page.evaluate("""() => {
                const results = {};
                for (const key of ['lat', 'lng', 'latitude', 'longitude', 'coords', 'coordinates',
                                     'mapLat', 'mapLng', 'markerLat', 'markerLng', 'adLat', 'adLng',
                                     'propertyLat', 'propertyLng', 'LATITUDE', 'LONGITUDE',
                                     'mapCenter', 'mapData', 'adData', 'listingData']) {
                    if (window[key] !== undefined) results[key] = String(window[key]).substring(0, 200);
                }
                if (window.google && window.google.maps) results['google.maps'] = 'LOADED';
                const mapDivs = document.querySelectorAll('[id*="map"], [class*="map"], .gm-style');
                if (mapDivs.length) results['map_divs'] = Array.from(mapDivs).map(d => (d.id || '') + '.' + (d.className || '')).slice(0,5);
                return results;
            }""")
            if js_checks:
                for k, v in js_checks.items():
                    print(f"    JS: {k} = {v}")
            else:
                print("    No JS coordinate variables found.")

        except Exception as e:
            print(f"  ERROR: {e}")
        finally:
            await browser.close()

# ── 3. Dom.cy (Playwright via CDP) ────────────────────────────────────────
async def check_domcy():
    from playwright.async_api import async_playwright
    print("\n" + "=" * 80)
    print("3. DOM.CY (Playwright via CDP port 9222)")
    print("=" * 80)
    url = "https://dom.com.cy/en/catalog/sale/210124/"

    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp("http://localhost:9222")
            print("  Connected to CDP.")
        except Exception as e:
            print(f"  ERROR connecting to CDP: {e}")
            return

        try:
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=60000)
            await page.wait_for_timeout(4000)
            html = await page.content()
            print(f"  Page loaded, HTML length: {len(html)}")

            patterns = {
                "lat/lng attributes": re.findall(r'(?:data-)?(?:lat|lng|latitude|longitude)\s*[=:]\s*["\']?([0-9.\-]+)', html, re.I),
                "LatLng()": re.findall(r'LatLng\(([^)]+)\)', html, re.I),
                "google maps iframe": re.findall(r'<iframe[^>]*(?:maps|google)[^>]*>', html, re.I),
                "maps.googleapis": re.findall(r'maps\.googleapis[^"\'<>\s]{0,300}', html, re.I),
                "Cyprus-range coords": re.findall(r'(?<![0-9])3[34]\.\d{3,7}(?!\d)', html),
                "coord JSON keys": re.findall(r'["\'](?:lat|lng|latitude|longitude|coordinates?|geo)["\']:\s*["\']?([0-9.\-]+)', html, re.I),
                "center: [": re.findall(r'center\s*:\s*\[[^\]]{5,40}\]', html, re.I),
                "ymaps/yandex": re.findall(r'(?:ymaps|yandex)[^;\n]{0,100}', html, re.I),
            }
            found_any = False
            for label, matches in patterns.items():
                if matches:
                    found_any = True
                    print(f"  FOUND {label}:")
                    for m in matches[:10]:
                        print(f"    -> {m[:300]}")
            if not found_any:
                print("  No coordinate patterns found in HTML.")

            print("\n  -- Checking arCatalogElementResult.row --")
            row_info = await page.evaluate("""() => {
                const results = {};
                if (typeof arCatalogElementResult !== 'undefined' && arCatalogElementResult.row) {
                    const row = arCatalogElementResult.row;
                    const allKeys = Object.keys(row);
                    results['total_keys'] = allKeys.length;
                    results['all_keys'] = allKeys;
                    const geoKeys = allKeys.filter(k =>
                        /lat|lng|geo|map|coord|location/i.test(k)
                    );
                    results['geo_keys'] = geoKeys;
                    const geoValues = {};
                    for (const k of geoKeys) {
                        geoValues[k] = JSON.stringify(row[k]).substring(0, 500);
                    }
                    results['geo_values'] = geoValues;
                } else {
                    results['error'] = 'arCatalogElementResult not found or no .row';
                    if (typeof arCatalogElementResult !== 'undefined') {
                        results['arCatalogElementResult_keys'] = Object.keys(arCatalogElementResult);
                    }
                }
                return results;
            }""")

            if 'error' in row_info:
                print(f"    {row_info['error']}")
                if 'arCatalogElementResult_keys' in row_info:
                    print(f"    Top-level keys: {row_info['arCatalogElementResult_keys']}")
            else:
                print(f"    Total keys in row: {row_info.get('total_keys')}")
                geo_keys = row_info.get('geo_keys', [])
                if geo_keys:
                    print(f"    Geo-related keys: {geo_keys}")
                    for k, v in row_info.get('geo_values', {}).items():
                        print(f"      {k} = {v}")
                else:
                    print("    No geo-related keys found.")
                all_keys = row_info.get('all_keys', [])
                print(f"\n    ALL keys in arCatalogElementResult.row ({len(all_keys)} total):")
                for i in range(0, len(all_keys), 4):
                    chunk = all_keys[i:i+4]
                    print(f"      {', '.join(chunk)}")

        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback; traceback.print_exc()
        finally:
            await page.close()

# ── Main ──────────────────────────────────────────────────────────────────
async def main():
    check_sothebys()
    await check_bazaraki()
    await check_domcy()
    print("\n" + "=" * 80)
    print("DONE")

asyncio.run(main())
