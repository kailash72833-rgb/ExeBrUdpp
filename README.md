# EXE LEVEL UP

Support: https://t.me/Brexxee
Channel: https://t.me/BrExeee
Proof channel: https://t.me/Exeprooff

## Plans (price unchanged)
| Plan  | Duration | Price | Slots |
|-------|----------|-------|-------|
| Basic | 1 Day    | Rs 99   | 5   |
| Pro   | 3 Days   | Rs 299  | 15  |
| Ultra | 10 Days  | Rs 599  | 40  |
| Mega  | 20 Days  | Rs 1499 | 100 |

Edit them in `DEFAULT_PLANS` (dashboard_server.py). If you change them later, also change `PLANS_VERSION`
so the server rewrites plans.json once.

## Environment variables
- `ADMIN_PASSWORD`  (required, 8+ chars; the old default is rejected)
- `APP_URL`         public https URL of the site -> `https://www.levelupbot.site` (set the same value on Railway; ZapUPI success/webhook URLs are built from it)
- `Key` / `ZAPUPI_API_KEY`  ZapUPI key
- `WEBHOOK_SECRET`  random secret (signs payment callbacks)
- `MONGODB_CONNECTION_STRING` (optional, users in MongoDB)
- `ZAPUPI_STATUS_URL` (optional), `TRUSTED_PROXY_HOPS` (default 1)
- `SEARCH_STALL_TIMEOUT` (default 240) seconds without a match before the bot logs in again by itself
- `VERBOSE_LOGS=1` (optional) shows every search packet in the log (off by default)

Run: `pip install -r requirements.txt && python Main.py`
