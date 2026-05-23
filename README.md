# Telegram-Only Selling Bot

This is a normal Telegram bot, not a Telegram Mini App and not a website. Users buy products inside Telegram. Admins manage everything inside Telegram with `/admin`.

## What It Includes

- Telegram-only admin panel visible only to `ADMIN_IDS`
- Forced join channel setup and verification
- Products with unlimited variants, such as 1 day, 7 days, 30 days
- Product emoji can be set from the Telegram admin panel
- Separate stock for each variant
- Add stock, list available stock, delete stock by stock ID
- User balance wallet
- Users list and users with balance list
- Add or deduct user balance
- Ban and unban users
- Direct message any user from the bot
- Broadcast any text/media message to all users
- Invite Friends referral link with automatic reward credit
- Info Bot screen with configurable Owner and Channel links
- Customer purchase pages use a cleaner single-message flow for product, day/variant and price selection
- Purchase flow includes quantity selection, confirmation page, and success page with monospace license keys
- USD prices display with `$` instead of the text `USD`
- Delivered keys and invite links are sent as separate copy-friendly messages
- Manual top-up request and admin approval
- Order notification to admins with user, product, variant, days and price
- Admin order/top-up notifications include View Details and User Orders buttons
- Admin-managed redeem codes with value and max usage limit
- Profile page is clean, with separate order history and full TXT export
- Neon PostgreSQL support through `DATABASE_URL`
- SQLite fallback for local testing
- Optimized database indexes, cached settings, cached successful join checks and fast bulk stock insert

## Environment Variables

Copy `.env.example` to `.env` for local testing.

```env
BOT_TOKEN=your_bot_token
ADMIN_IDS=your_numeric_telegram_user_id
BOT_USERNAME=your_bot_username
DATABASE_URL=postgresql://user:password@host/dbname?sslmode=require
DB_PATH=data/telegram_selling_bot.sqlite3
JOIN_CACHE_SECONDS=300
SETTINGS_CACHE_SECONDS=5
BROADCAST_DELAY_SECONDS=0.035
```

`ADMIN_IDS` must be numeric Telegram user IDs, not usernames. Only those accounts can open `/admin`.

## Local Run

```powershell
python selling_bot.py --init-db
python selling_bot.py
```

Test database logic:

```powershell
python selling_bot.py --smoke-test
```

## Render + Neon

1. Create a Neon PostgreSQL database.
2. Copy the Neon pooled connection string.
3. Create a Render Background Worker.
4. Set environment variables:
   - `BOT_TOKEN`
   - `ADMIN_IDS`
   - `BOT_USERNAME`
   - `DATABASE_URL`
   - `JOIN_CACHE_SECONDS` optional
   - `SETTINGS_CACHE_SECONDS` optional
   - `BROADCAST_DELAY_SECONDS` optional
5. Render start command:

```bash
python selling_bot.py
```

This bot uses Telegram long polling, so it should be deployed as a single Render Background Worker. Do not run multiple workers for the same bot token.

## Railway + Neon

1. Create a Neon PostgreSQL database.
2. Copy the Neon pooled connection string with `sslmode=require`.
3. Push these project files to GitHub.
4. In Railway, create a new project from the GitHub repository.
5. Add environment variables:
   - `BOT_TOKEN`
   - `ADMIN_IDS`
   - `BOT_USERNAME`
   - `DATABASE_URL`
   - `JOIN_CACHE_SECONDS` optional
   - `SETTINGS_CACHE_SECONDS` optional
   - `BROADCAST_DELAY_SECONDS` optional
6. Railway will use `railway.json` and run:

```bash
python selling_bot.py
```

No public domain or port is needed for this bot. It is an always-on background worker that talks to Telegram with long polling. Run only one Railway service for one bot token.

## User Commands

```text
/start
/shop
/balance
/topup
/pay amount transaction_id
/orders
/invite
/profile
/info
/help
/contact
```

## Admin Commands

```text
/admin
/cancel
```

Inside `/admin`, use the Telegram buttons:

- Products
- Add Product
- Users
- Balances
- Broadcast
- Direct Message
- Channels
- Settings
- Orders
- Top-ups

## Referral and Info Buttons

Users see `Buy Key`, `Invite Friends`, `Profile`, `Info Bot`, and `Redeem` in the main Telegram keyboard.
Admin accounts see only `Admin Panel`; customer buttons are hidden for admin IDs.
Purchase pages edit the same bot message while the user navigates, so the chat does not fill with repeated product pages.

Set these from `/admin > Settings`:

- `Bot Username`: bot username without `@`, used for links like `https://t.me/YourBot?start=USER_ID`
- `Referral Reward`: amount credited to referrer for each new invited user, for example `0.01`
- `Owner URL`: Telegram owner link, for example `https://t.me/ownerusername`
- `Channel URL`: Telegram channel link, for example `https://t.me/channelusername`
- `Info Text`: text shown when user taps `Info Bot`
- `Redeem Text`: text shown when user taps `Redeem`

## Redeem Codes

Admins can create wallet redeem codes from `/admin > Redeem Codes`.

Send:

```text
FREE10
10
10
```

This creates code `FREE10`, value `$10`, usable by 10 different users. Each user can claim the same code only once.

## Product and Stock Flow

1. Admin opens `/admin`.
2. Admin taps `Add Product`.
3. Admin sends:

```text
Fluorite Product
Optional description here
```

4. Admin opens the product and taps `Add Variant`.
5. Admin sends:

```text
7 Day
7
12.50
```

6. Admin opens the variant and taps `Add Stock`.
7. Admin sends one stock item per line:

```text
KEY-001
KEY-002
KEY-003
```

When a user buys that variant, one available stock line is delivered and marked sold.

After creating a product, open it from `/admin > Products` and tap `Set Emoji` to attach an emoji like `💎`, `📦`, `🔑`, or `🎁`.

## Forced Join Verification

From `/admin > Channels > Add Channel`, send:

```text
Channel 1
https://t.me/yourchannel
@yourchannel
```

The third line is important. It must be `@channelusername` or the numeric channel chat ID, and the bot must be admin/member in that channel so Telegram can verify joins.

For private channels, use the numeric channel chat ID. Invite links such as `https://t.me/+...` cannot be verified by Telegram unless the bot also has a real chat ID and access to that channel.
