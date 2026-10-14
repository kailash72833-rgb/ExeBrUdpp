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
- `MONGODB_CONNECTION_STRING` (or `MONGODB_CONNECTION_STRING_2`) **REQUIRED on Railway / Render**: users, sessions, accounts, order history, free-plan requests, device ids, token cache and EXP stats are stored there. Without it everything lives in local files and is wiped on every restart / redeploy.
- `ZAPUPI_STATUS_URL` (optional), `TRUSTED_PROXY_HOPS` (default 1)
- `SEARCH_STALL_TIMEOUT` (default 240) seconds without a match before the bot logs in again by itself
- `VERBOSE_LOGS=1` (optional) shows every search packet in the log (off by default)

## Data storage (survives restarts / redeploys)
- Everything that matters is saved in MongoDB (`persist.py`): accounts, order history, free-plan requests, device ids, token cache, EXP gained / matches per account (plus users and sessions).
- Writes reach MongoDB within ~1 s and again on a clean shutdown (SIGTERM). Local files are only a mirror / fallback.
- Start-up log shows `[STORAGE] storage mode: MongoDB (...) | accounts=N orders=M ...`. If you see `MongoDB is NOT configured` / `not reachable`, data is NOT being kept.
- A MongoDB hiccup can never wipe data: a document that could not be read is not written back.
- Admin panel: **Download ZIP** = backup of all of the above, **Restore backup** = upload that ZIP (or accounts.json / orders.json) again; existing data is kept, only missing items are added and the restored accounts start right away.
- Update popup (login screen + home page): edit the text in `#annModal` in `templates/index.html` and change `ANN_VER` to show a new update to everybody again.

Run: `pip install -r requirements.txt && python Main.py`


## Level rules (Additional settings when adding accounts)
- **Max level to add**: only accounts up to this level can be added (e.g. 21 = level 21 or lower). Higher accounts are not added at all (removed again right after login, slot is free).
- **Stop level**: when an account reaches it, it goes offline and shows `STOPPED` (it is NOT deleted).
- Both are optional (tick *Additional settings*), also for bulk add. Off = same behaviour as before.
- `GZIP=0` turns response compression off if your proxy already compresses.
