# dom.com.cy Parser

Парсер недвижимости с dom.com.cy (Кипр). Собирает квартиры и дома на продажу из Лимассола, Ларнаки и Пафоса.

## Требования

- Python 3.12+
- Playwright (`pip install playwright`)
- Google Chrome (реальный, не Chromium от Playwright)
- python-dotenv

## Почему Chrome, а не Playwright Chromium?

dom.com.cy использует SafeLine WAF (аналог Cloudflare), который детектит автоматизированные браузеры. Playwright Chromium блокируется. Решение — подключаться к реальному Chrome через CDP (Chrome DevTools Protocol).

Chrome запускается автоматически при первом запуске скрипта.

## Запуск

```bash
# Быстрый режим (только карточки, без детальных страниц)
python3 scraper.py 1 --fast

# Полный режим с детальными страницами (1 стр на запрос)
python3 scraper.py 1

# Полный прогон (10 стр на запрос, ~1200 объявлений)
python3 scraper.py 10

# Фильтр по спальням
python3 scraper.py 5 --bedrooms 2,3
```

## Архитектура

```
config.py  — URL-ы, селекторы, лимиты, 6 запросов (3 города × 2 типа)
scraper.py — Playwright через CDP → Chrome, парсинг карточек + JS-объект на детальных
db.py      — SQLite (dom_cy.db), upsert, 24 поля + source
notify.py  — Telegram-нотификации, p25-логика поиска выгодных предложений
```

## Данные

На карточках извлекаются: ID, название, цена, цена/м², площадь, спальни, район.

На детальных страницах — JS-объект `arCatalogElementResult` (Bitrix CMS) с полными данными:
- Цена, площадь, участок
- Спальни, ванные
- Город, район, расстояние до моря
- Год постройки, энергоэффективность
- Удобства (бассейн, парковка, мебель и т.д.)

## БД

```bash
# Проверить данные
sqlite3 dom_cy.db "SELECT COUNT(*), district, property_type FROM listings GROUP BY district, property_type"

# Средняя цена за м² по районам
sqlite3 dom_cy.db "SELECT district, ROUND(AVG(price_per_sqm)) FROM listings WHERE price_per_sqm > 0 GROUP BY district"
```

## Telegram

Копируем `.env` из bazaraki (те же креды):

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

После каждого прогона автоматически ищет объявления ниже p25 €/м² в сегменте (район, спальни, состояние) и шлет в Telegram.
