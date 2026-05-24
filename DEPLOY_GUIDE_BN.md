# Telegram Selling Bot Deploy Guide

এই গাইড অনুযায়ী করলে bot-টি GitHub থেকে Railway always-on worker/service হিসেবে deploy হবে, database থাকবে Neon PostgreSQL-এ, এবং admin panel থাকবে শুধুমাত্র Telegram bot-এর ভিতরে `/admin` command-এ। চাইলে Render Background Worker-এও একই code deploy করা যাবে।

## 1. BotFather থেকে Bot Token নিন

1. Telegram-এ `@BotFather` খুলুন।
2. `/newbot` দিন।
3. Bot name দিন।
4. Bot username দিন, যেমন `your_shop_bot`.
5. BotFather যে token দেবে সেটি রাখুন।

Railway বা Render-এ এই token `BOT_TOKEN` environment variable-এ দিতে হবে।

## 2. Admin Telegram User ID বের করুন

`ADMIN_IDS`-এ username নয়, numeric Telegram user ID লাগবে।

সহজ পদ্ধতি:

1. Telegram-এ `@userinfobot` অথবা `@RawDataBot` খুলুন।
2. `/start` দিন।
3. আপনার numeric user ID কপি করুন।
4. একাধিক admin হলে comma দিয়ে দিন:

```text
111111111,222222222
```

শুধু এই ID-গুলোর account `/admin` panel দেখতে পারবে। সাধারণ user বা customer admin panel দেখতে পারবে না।

## 3. Neon Database তৈরি করুন

1. Neon dashboard খুলুন।
2. New Project তৈরি করুন।
3. PostgreSQL database তৈরি হলে connection string কপি করুন।
4. Railway/Render hosting-এর জন্য pooled connection string ব্যবহার করা ভালো।
5. connection string-এর শেষে `sslmode=require` থাকা উচিত।

Example:

```text
postgresql://user:password@host/dbname?sslmode=require
```

Railway বা Render-এ এটি `DATABASE_URL` environment variable-এ দিতে হবে।

## 4. GitHub Repository তৈরি করুন

1. এই zip file unzip করুন।
2. GitHub-এ নতুন repository তৈরি করুন।
3. Unzip করা folder-এর সব file repository root-এ upload করুন।
4. `.env` upload করবেন না। শুধু `.env.example` থাকবে।

Repository root-এ এই files থাকা উচিত:

```text
selling_bot.py
requirements.txt
railway.json
neon_optimize.sql
render.yaml
Procfile
README.md
DEPLOY_GUIDE_BN.md
.env.example
.gitignore
```

## 5. Railway-তে Deploy করুন

1. Railway dashboard খুলুন।
2. `New Project` চাপুন।
3. `Deploy from GitHub repo` নির্বাচন করুন।
4. আপনার GitHub repository select করুন।
5. Railway auto build করবে। এই project-এ `railway.json` আছে, তাই start command automatic হবে:

```bash
python selling_bot.py
```

6. Service-এর `Variables` tab-এ environment variables দিন:

```text
BOT_TOKEN=BotFather থেকে পাওয়া token
ADMIN_IDS=আপনার numeric Telegram user ID
BOT_USERNAME=আপনার bot username, @ ছাড়া
DATABASE_URL=Neon PostgreSQL connection string
JOIN_CACHE_SECONDS=300
SETTINGS_CACHE_SECONDS=5
PG_CONNECT_TIMEOUT=10
PG_KEEPALIVES_IDLE=30
PG_KEEPALIVES_INTERVAL=10
PG_KEEPALIVES_COUNT=5
PG_RECONNECT_LOG_SECONDS=60
BROADCAST_DELAY_SECONDS=0.035
```

7. Deploy শেষ হলে Railway logs খুলে দেখুন `Bot is running` টাইপ log আসে কিনা।

Important: এই bot web server না, তাই public domain বা port দরকার নেই। Railway-তে এটাকে always-on background worker/service হিসেবে চালাবেন। একই bot token দিয়ে একসঙ্গে দুই জায়গায় bot চালাবেন না।

## 5.0 Neon Speed Optimize SQL

Bot নিজেই table/index তৈরি করবে। তারপরও extra speed-এর জন্য deploy-এর পরে Neon SQL Editor-এ `neon_optimize.sql` file-এর code একবার run করতে পারেন।

এটি শুধু missing index তৈরি করে এবং `ANALYZE` চালায়, তাই repeated run করলেও সমস্যা নেই।

Neon idle SSL connection বন্ধ করে দিলে bot নিজে একবার fresh PostgreSQL connection নিয়ে query retry করবে। তাই `SSL connection has been closed unexpectedly` টাইপ error হলে সাধারণত service restart ছাড়াই recover করবে।

## 5.1 Render-এ Deploy করতে চাইলে

1. Render dashboard খুলুন।
2. New > Background Worker নির্বাচন করুন।
3. GitHub repository connect করুন।
4. Environment: Python
5. Build command:

```bash
pip install -r requirements.txt
```

6. Start command:

```bash
python selling_bot.py
```

7. Environment variables দিন:

```text
BOT_TOKEN=BotFather থেকে পাওয়া token
ADMIN_IDS=আপনার numeric Telegram user ID
BOT_USERNAME=আপনার bot username, @ ছাড়া
DATABASE_URL=Neon PostgreSQL connection string
JOIN_CACHE_SECONDS=300
SETTINGS_CACHE_SECONDS=5
PG_CONNECT_TIMEOUT=10
PG_KEEPALIVES_IDLE=30
PG_KEEPALIVES_INTERVAL=10
PG_KEEPALIVES_COUNT=5
PG_RECONNECT_LOG_SECONDS=60
BROADCAST_DELAY_SECONDS=0.035
```

8. Deploy করুন।

Important: একই bot token দিয়ে একসঙ্গে একাধিক worker চালাবেন না। Telegram long polling conflict হবে।

Speed note: `BROADCAST_DELAY_SECONDS=0.035` fast broadcast-এর জন্য balanced value. User অনেক বেশি হলে `0.05` দিতে পারেন।

## 6. Bot চালু আছে কিনা পরীক্ষা করুন

1. Telegram-এ আপনার bot খুলুন।
2. `/start` দিন।
3. Admin account থেকে `/admin` দিন।
4. Admin panel দেখা গেলে deploy ঠিক আছে।

## 6.1 Referral, Invite Friends, Info Bot Setup

Admin account থেকে:

1. `/admin`
2. `Settings`
3. `Bot Username` সেট করুন। username-এ `@` দেবেন না।
4. `Referral Reward` সেট করুন, যেমন:

```text
0.01
```

5. `Owner URL` সেট করুন:

```text
https://t.me/ownerusername
```

6. `Channel URL` সেট করুন:

```text
https://t.me/channelusername
```

User যখন `Invite Friends` চাপবে, bot unique link দেখাবে:

```text
https://t.me/YourBot?start=USER_ID
```

নতুন user ওই link দিয়ে `/start` করলে referrer balance-এ referral reward add হবে।

User যখন `Info Bot` চাপবে, `Info Text` দেখাবে এবং নিচে `CHANNEL` ও `OWNER` button থাকবে। এই button-এর link admin settings থেকে নিয়ন্ত্রণ করা যাবে।

Admin account-এ customer menu দেখাবে না। Admin ID দিয়ে `/start` বা `/admin` দিলে শুধু `👑 Admin Panel` option থাকবে।

Customer purchase page-এ product, day/variant, price এবং stock একই message edit করে দেখাবে, তাই বারবার নতুন page/message আসবে না। USD text-এর বদলে price `$` symbol দিয়ে দেখাবে।

Invite link বা delivered key copy করার জন্য bot আলাদা copy-friendly message পাঠাবে। Telegram normal bot সরাসরি clipboard copy করতে পারে না, তাই user ওই message tap-hold করে copy করবে।

যদি `/admin` কাজ না করে:

- `ADMIN_IDS` ঠিক numeric ID কিনা দেখুন।
- Railway/Render environment variable save করার পর redeploy করুন।
- Railway/Render logs-এ error দেখুন।

## 7. Forced Join Channel Setup

Admin account থেকে:

1. `/admin`
2. `Channels`
3. `Add Channel`
4. এই format-এ message পাঠান:

```text
Channel 1
https://t.me/yourchannel
@yourchannel
```

Private channel হলে bot-কে channel/group-এ add করে admin/member করতে হবে, তারপর correct chat ID দিতে হবে। Public channel হলে `@channelusername` ব্যবহার করা যায়।

Important:

- Third line-এ `@channelusername` অথবা numeric chat ID দিতে হবে।
- Verify button কাজ করার জন্য bot-কে ওই channel-এ admin/member রাখতে হবে।
- User Verify চাপলে bot fresh Telegram check করবে। Join না থাকলে alert-এ কোন channel বাকি আছে দেখাবে।
- Private channel হলে invite link `https://t.me/+...` দিয়ে verify করা যায় না। Third line-এ numeric channel chat ID দিন এবং bot-কে channel admin/member রাখুন।

## 8. Product, Variant, Stock Setup

Admin account থেকে:

1. `/admin`
2. `Add Product`
3. Product name এবং description দিন:

```text
Fluorite Product
Premium account product
```

4. Product list থেকে product খুলুন।
5. চাইলে `Set Emoji` চাপুন এবং product emoji দিন, যেমন:

```text
💎
```

6. `Add Variant` চাপুন।
7. Variant info দিন:

```text
7 Day
7
12.50
```

এখানে:

- `7 Day` = variant title
- `7` = days
- `12.50` = price

8. Variant খুলুন।
9. `Add Stock` চাপুন।
10. এক line-এ এক stock item দিন:

```text
KEY-001
KEY-002
KEY-003
```

User যখন কিনবে, একটি available stock line user-কে delivery যাবে এবং সেটি sold হয়ে যাবে।

Customer purchase flow:

1. User `Buy Key` চাপবে।
2. Product select করবে।
3. Duration/price/stock list দেখবে।
4. Variant select করলে quantity page আসবে: `1x`, `2x`, `3x`, অথবা `Custom Quantity`।
5. Confirm page-এ balance, total, after purchase balance দেখাবে।
6. Confirm করলে success page-এ license key monospace format-এ দেখাবে, যাতে tap-hold করে copy করা যায়।

Admin stock tools:

- `Add Stock` = এক line-এ এক key add করুন।
- `List Stock` = stock ID, key এবং add time দেখাবে।
- `Export Stock TXT` = সব available stock text file হিসেবে export করবে।
- Variant active হলে button/status-এ ✅ দেখাবে, inactive হলে ❌ দেখাবে।

## 8.3 Maintenance Mode

Admin account থেকে `/admin` খুলে `Maintenance` চাপলে bot customerদের জন্য off হবে।

- Maintenance ON: user shop/profile/order/redeem ব্যবহার করতে পারবে না, maintenance text দেখবে।
- Maintenance OFF: bot active হবে এবং সব active user-কে notification যাবে।

Maintenance message edit করতে:

1. `/admin`
2. `Settings`
3. `Maintenance Text` অথবা `Active Notice`

## 8.1 Redeem Code Setup

Admin account থেকে:

1. `/admin`
2. `Redeem Codes`
3. `Add Redeem Code`
4. এই format-এ পাঠান:

```text
FREE10
10
10
```

এখানে:

- `FREE10` = redeem code
- `10` = এক user claim করলে কত dollar/balance add হবে
- `10` = মোট কতজন user এই code claim করতে পারবে

User `Redeem` চাপলে code পাঠাবে। Code valid হলে balance automatic add হবে। একই user একই code একবারের বেশি claim করতে পারবে না।

## 8.2 Profile and Order History

User `Profile` চাপলে শুধু basic info দেখাবে:

- Name
- User ID
- Username
- Member since
- Wallet
- Invite count

Profile-এর নিচে `Order History` button থাকবে। এখানে latest orders দেখা যাবে। `Export Full TXT` চাপলে সব order text file হিসেবে bot পাঠাবে।

## 9. Stock দেখা বা Delete করা

1. `/admin`
2. `Products`
3. Product খুলুন।
4. Variant খুলুন।
5. `List Stock` দিলে first available stock IDs দেখা যাবে।
6. `Delete Stock` চাপুন।
7. delete করার stock IDs comma দিয়ে পাঠান:

```text
12,13,14
```

## 10. User, Balance, Ban/Unban

Admin panel থেকে:

- `Users` = সব user দেখাবে।
- `Balances` = যাদের balance আছে তাদের দেখাবে।
- User খুললে:
  - Add Balance
  - Deduct
  - Direct Message
  - Ban/Unban
  - Custom Price
  - Orders

Balance amount পাঠানোর example:

```text
10.00
```

## 11. Direct Message

1. `/admin`
2. `Direct Message`
3. Target user ID পাঠান।
4. এরপর যে text/media পাঠাবেন, bot সেটি user-কে পাঠাবে।

User detail page থেকেও `Direct Message` করা যায়।

## 12. Broadcast

1. `/admin`
2. `Broadcast`
3. যে message/media সব user-কে পাঠাতে চান সেটি পাঠান।
4. Bot সব active user-কে notification পাঠাবে।

## 13. Top-up Flow

User:

```text
/topup
/pay 10.00 TXN12345
```

Admin:

1. `/admin`
2. `Top-ups`
3. Approve বা Reject করুন।

Approve করলে user balance automatic add হবে।

## 14. Order Notification

User purchase করলে:

- User item delivery পাবে।
- Admin account-এ notification আসবে:
  - order ID
  - user ID
  - product
  - variant
  - days
  - price

## 15. Common Problems

`psycopg` error:

- Railway/Render build dependency install ঠিক হচ্ছে কিনা দেখুন। Render হলে build command:

```bash
pip install -r requirements.txt
```

Database connection error:

- Neon `DATABASE_URL` ঠিক কিনা দেখুন।
- `sslmode=require` আছে কিনা দেখুন।

Admin panel দেখা যাচ্ছে না:

- `/admin` শুধু `ADMIN_IDS` account-এ কাজ করবে।
- `ADMIN_IDS` username নয়, numeric ID।

Bot reply দিচ্ছে না:

- Railway/Render logs দেখুন।
- `BOT_TOKEN` ঠিক কিনা দেখুন।
- একই bot token দিয়ে অন্য কোথাও bot চালু আছে কিনা দেখুন।
