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
- `ADMIN_PATH`       secret address of the admin page, e.g. `exe-owner-8841` -> `https://www.levelupbot.site/exe-owner-8841`. The public site contains NO admin code or button. If not set, a private address is printed in the server log at start-up.
- `APP_URL`         public https URL of the site -> `https://www.levelupbot.site` (set the same value on Railway; ZapUPI success/webhook URLs are built from it)
- `Key` / `ZAPUPI_API_KEY`  ZapUPI key
- `WEBHOOK_SECRET`  random secret (signs payment callbacks)
- `MONGODB_CONNECTION_STRING` (optional, users in MongoDB)
- `ZAPUPI_STATUS_URL` (optional), `TRUSTED_PROXY_HOPS` (default 1)
- `SEARCH_STALL_TIMEOUT` (default 240) seconds without a match before the bot logs in again by itself
- `VERBOSE_LOGS=1` (optional) shows every search packet in the log (off by default)

Run: `pip install -r requirements.txt && python Main.py`


## Level rules (Additional settings when adding accounts)
- **Min level**: accounts below it are not added at all (removed again right after login, slot is free).
- **Stop level**: when an account reaches it, it goes offline and shows `STOPPED` (it is NOT deleted).
- Both are optional (tick *Additional settings*), also for bulk add. Off = same behaviour as before.
- `GZIP=0` turns response compression off if your proxy already compresses.
