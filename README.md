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
- `APP_VERSION` (optional) force the Free Fire app version used for the version lookup, e.g. `1.132.11`. Normally NOT needed (see "Login / version" below).
- `LOGIN_MODE` (optional, default `auto`): `auto` | `web` | `lean` | `full` | `legacy`, see "Login / version" below.
- `PLAY_VER` (optional, default `1.132.1`): value of the `PlAy_VeR` header of the new login.
- `LOGIN_REGION` (optional, default `ID`): `region` sent to the version server and in the login body (field 26); also picks `lang`. Set `IND` if your accounts are India-server accounts.
- `LOGIN_SERVER_URL` (optional): force the MajorLogin server, e.g. `https://loginbp.ppmainecoonghj.com/`. Normally NOT needed.

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

## Login / version (v9)
**Version lookup.** The app version is asked from the Play Store; a wrong value ("Varies with device", an unknown 2.x.x) is ignored and the version server is asked with a list of candidates until it answers with a login server: `APP_VERSION` -> Play Store -> last version that worked -> built-in fallbacks (`DEFAULT_FALLBACK_VERSIONS`, edit after a new OB). Log: `[VERSION] OK app=... remote=... release=... server=...`.

**Login modes (OB55).** `LOGIN_MODE=auto` tries the modes in this order, remembers the one that works and starts with it next time:
1. `web`    - the login of Web.zip: OB55 payload (build values 57/60-67/77/83/94/95/102/104 of 1.132.x) + `vercfg_sign`, `cdn_url`, `abhotupdate_cdn_url`, `client_ip` taken LIVE from the version server (`channel=3rd_party`, whitelist 1.7.0, device data) + new headers (`X-GA-SV` = current time, no `Expect`) + GetLoginData body with the jwt (field 108). MajorLogin servers: the version server's address, then `loginbp.ggpolarbear.com`; GetLoginData: the address in the MajorLogin answer, then the region's client address. An AES-encrypted answer is decrypted automatically. Each account keeps its own device id (field 19).
2. `lean`   - small LoginReq (open_id, open_id_type, login_token, orign_platform_type) + `PlAy_VeR` / `Ob_VeR` headers. Guest token from `ffmconnect.live.gop.garenanow.com` (old `100067.connect.garena.com` is the fallback).
3. `full`   - the big OLD payload, but with the lean headers.
4. `legacy` - exactly the old login (old payload + old headers + old Host).
A mode is accepted only if MajorLogin answers with token + account id + `aes_ak` / `iv_i` / `server_time` (needed for the game gateway) AND GetLoginData returns a gateway address. `LOGIN_MODE=web` (or `lean` / `full` / `legacy`) forces one style and never falls back.

Why the old payload was a problem: it still carried the values of an older game build, e.g. `vercfg_sign` `1.3fb8adbf45f44ce9`; the version server now answers with a different `vercfg_sign` per version/region (e.g. `1.f03abb0051baf3c6`). The `web` mode always sends the current one.

Log lines worth checking:
- `[WEBVER] OK app=... vercfg=...`  -> version server config for the web login
- `[LOGIN] login mode 'web' works ...`  -> web login OK; `[LOGIN] web login servers: MajorLogin=... | GetLoginData=...`
- `[MAJORLOGIN/<style>] server rejected payload: HTTP ... | url=...` / `answer could not be read` -> why that style failed
- `[GETLOGIN] refused -> ...` -> real reason why GetLoginData (EXP refresh) failed
- `[LOGIN] every login mode failed: ...` -> all styles failed, reasons are listed (incl. which protobuf field numbers the server sent)
- `[FUNCTIONAL] Non-match big packet ... | packet=NB top_fields=[...]` -> what an unrecognised game packet contained

`version_fix.py` (old plug-in) is NOT needed any more - delete it and the `import version_fix` block at the bottom of Main.py if you still have them.
