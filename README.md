# Telegram AI Sticker Bot 💗

A Telegram bot (Python 3.11+, **aiogram 3**, **MongoDB**, Pillow, rembg, OpenAI Images API) that

* **searches images** (Pinterest-compatible public index, Pixabay, Openverse) and turns them into stickers,
* **converts photos** into WEBP stickers (background removal, border, shadow, text, emoji, crop),
* **generates original AI stickers** in 13 styles,
* makes **text stickers** (fonts, colours, outlines, gradient/transparent backgrounds),
* **publishes real Telegram sticker packs** titled `Food : @Advik_prakashh`, `Cats : @Advik_prakashh`, …,
* ships a user dashboard, free/premium limits and a full admin panel.

Sticker pack credit: **@Advik_prakashh** (change with `PACK_CREDIT`).

---

## 1. Quick start with Docker (recommended)

Works on Ubuntu 22.04 and 24.04.

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git curl ca-certificates
curl -fsSL https://get.docker.com | sudo sh          # installs Docker Engine + Compose plugin
sudo systemctl enable --now docker

git clone https://github.com/YOUR_USERNAME/telegram-ai-sticker-bot.git
cd telegram-ai-sticker-bot
cp .env.example .env
nano .env            # set BOT_TOKEN (required), OPENAI_API_KEY, ADMIN_IDS
```

Leave `MONGO_URL=mongodb://mongo:27017` to use the MongoDB container bundled in `docker-compose.yml`.
To use MongoDB Atlas instead, paste your Atlas URL into `MONGO_URL` and start only the bot:
`sudo docker compose up -d --build --no-deps bot`.

```bash
sudo docker compose up -d --build        # build image + start bot + MongoDB
sudo docker compose logs -f --tail=100 bot   # view logs  (Ctrl+C to leave)
sudo docker compose ps                   # status / healthcheck
sudo docker compose restart bot          # restart
sudo docker compose down                 # stop (data volumes are kept)
```

### Update from GitHub

```bash
cd ~/telegram-ai-sticker-bot
git pull
sudo docker compose up -d --build
sudo docker compose logs -f --tail=50 bot
```

Data lives in Docker volumes (`mongo_data`, `bot_data`, `bot_logs`) and survives rebuilds.
The restart policy is `unless-stopped`, so the bot comes back after crashes and reboots.

---

## 2. Deploy without Docker (venv + systemd)

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git curl libgomp1 libglib2.0-0 fonts-dejavu-core
# Python 3.11+ is required.
#   Ubuntu 24.04 ships 3.12:   sudo apt install -y python3 python3-venv python3-pip
#   Ubuntu 22.04 ships 3.10:   use the deadsnakes PPA:
sudo apt install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa && sudo apt update
sudo apt install -y python3.11 python3.11-venv

# MongoDB: use MongoDB Atlas (free tier) OR run it in Docker:
#   curl -fsSL https://get.docker.com | sudo sh
#   sudo docker run -d --name mongo --restart unless-stopped -p 127.0.0.1:27017:27017 -v mongo_data:/data/db mongo:7

sudo useradd --system --create-home --home-dir /home/stickerbot --shell /usr/sbin/nologin stickerbot || true
sudo git clone https://github.com/YOUR_USERNAME/telegram-ai-sticker-bot.git /opt/telegram-ai-sticker-bot
sudo chown -R stickerbot:stickerbot /opt/telegram-ai-sticker-bot
cd /opt/telegram-ai-sticker-bot

sudo -u stickerbot python3.11 -m venv .venv          # on 24.04 you can use python3
sudo -u stickerbot .venv/bin/pip install --upgrade pip
sudo -u stickerbot .venv/bin/pip install -r requirements.txt

sudo -u stickerbot cp .env.example .env
sudo -u stickerbot nano .env       # BOT_TOKEN, MONGO_URL (e.g. mongodb://127.0.0.1:27017), OPENAI_API_KEY ...
sudo chmod 600 .env

sudo cp telegram-ai-sticker-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-ai-sticker-bot
sudo systemctl status telegram-ai-sticker-bot
```

Logs, restart and update:

```bash
sudo journalctl -u telegram-ai-sticker-bot -f          # live logs
sudo tail -f /opt/telegram-ai-sticker-bot/logs/bot.log # rotating file log
sudo systemctl restart telegram-ai-sticker-bot

cd /opt/telegram-ai-sticker-bot
sudo -u stickerbot git pull
sudo -u stickerbot .venv/bin/pip install -r requirements.txt
sudo systemctl restart telegram-ai-sticker-bot
```

The unit uses `Restart=always`, so the bot restarts automatically after crashes, `/restart` and reboots.

---

## 3. Using the bot

| Command | What it does |
|---|---|
| `/start`, `/menu` | Main dashboard (search, AI, photo, cute/food/lovely, text, packs, settings) |
| `/find cute cats` | Search images → browse with ⬅️ ➡️, **Download**, **Create sticker**, **Add to pack** |
| `/sticker pink cat holding a strawberry` | AI sticker – then pick a style from the keyboard |
| *send a photo / image file* | “Choose your sticker style 💗” – toggle options, then **Create Sticker** |
| `/text Good Morning` | Text sticker (font, colour, outline, background) |
| `/addpack` | Queue your latest sticker for a pack |
| `/createpack`, `/mypacks` | Preview the queue (contact sheet) → **Publish pack** → get `t.me/addstickers/...` link |
| `/packclear` | Empty the queue |
| `/profile`, `/settings`, `/bgcolor #FFAACC` | Stats & limits; background mode / border / shadow / default style |
| `/cancel` | Leave the current step |

* **Categories** are detected from your search query, image title/tags, photo caption or AI prompt (and optionally by a
  vision model, `ENABLE_VISION_CATEGORY=true`). Tap **🏷 Category** under any sticker to correct it.
* Each category becomes its own pack: `Food : @Advik_prakashh`, `Cats : @Advik_prakashh`, `Cats 2 : …` when a pack is full.
* Queued stickers are stored in MongoDB and `data/stickers/`, so a restart never loses them. Duplicates are skipped.
* Background modes: automatic, keep original, transparent, white, custom colour. The bot only says a sticker is
  transparent when the result really has an alpha channel.

### Automatic mode (default)

Every sticker anyone creates is **added automatically to the owner’s account** (`OWNER_ID`, default = first `ADMIN_IDS`) in the pack for its
category. A pack holds **max 50 stickers** (`MAX_PACK_STICKERS`, hard-capped at 50). When a pack is full the bot starts a new pack with a
similar name: `Food : @Advik_prakashh` → `Food Vibes : …` → `Food Treats : …` → `Food Club : …`. Identical stickers are never added twice.
The first sticker of every new pack is posted to your public channel (`CHANNEL_ID`) with an **➕ Add pack** button; `/postpacks` posts any
pack that wasn’t announced yet. Requirements: press **Start** in the bot with the owner account, and make the bot an **admin of the channel**.
Set `AUTO_PACK=false` to go back to the manual queue → preview → publish flow.

### Quiet mode (default) and cloning

`QUIET_MODE=true`: send an image (or many at once) → you get the sticker back, already added to the owner pack, with just an
**➕ Add pack** button. No menus, previews or extra notes. Flat cartoon artwork automatically uses the `isnet-anime` cut-out model
(downloaded on first use); photos use `REMBG_MODEL`. `/find` turns the top results straight into stickers.

**Clone a pack (admins only):** send any sticker from a pack, or `/clone https://t.me/addstickers/PackName`. The bot copies it into
packs owned by `OWNER_ID` titled `<original title> : @Advik_prakashh` (e.g. `Food : @Advik_prakashh`), keeps static/animated/video
formats, splits into similarly named packs at 50 stickers, posts one sticker to the channel and never clones the same pack twice.
Only clone packs you have the right to reuse.

### If packs/clone don't work: the owner ID

Telegram says `user not found` when `OWNER_ID` isn't the numeric ID of an account that pressed **Start** in the bot, and `/clone` is silent
when your ID isn't in `ADMIN_IDS`. Send `/id` to the bot, then put that number in `.env` as `ADMIN_IDS=` **and** `OWNER_ID=`, and
`docker compose up -d`. Or, as an admin, send `/owner` to make your own account the pack owner without editing `.env`. The startup log
prints `Pack owner account: …` or an explicit error.

### Image generation without OpenAI

`AI_PROVIDERS=openai,pollinations` (default): if the OpenAI key is missing/rejected, the free hosted Pollinations generator is used and the
bot cuts the subject out automatically. For generation on **your own server CPU** set `AI_PROVIDERS=pollinations,local`, `INSTALL_LOCAL_AI=1` in
`.env` and run `sudo docker compose up -d --build` (adds PyTorch + diffusers, SD-Turbo; needs ~6 GB RAM, 20–90 s per image, downloads ~2 GB once).

### Admin commands (IDs from `ADMIN_IDS`)

`/admin` dashboard · `/stats` · `/users [id]` · `/broadcast <html text>` (preview + confirm, rate-limited) ·
`/ban <id> [reason]` · `/unban <id>` · `/setlimit <generations|searches|stickers|packs> <n>` ·
`/premium grant <id> [days]` · `/premium revoke <id>` · `/premium status <id>` · `/maintenance on|off` · `/postpacks` · `/owner` · `/restart`. Everyone: `/id`.
Non-admins get no response from these commands. Admins have no usage limits and can tap
**🌐 Add to category pack** to put a sticker into the shared bot-owned pack for its category.

### Free vs premium

Free limits come from `.env` (`MAX_DAILY_GENERATIONS`, `MAX_DAILY_SEARCHES`, `MAX_DAILY_STICKERS`, `MAX_PACKS_FREE`).
Premium users get those × `PREMIUM_MULTIPLIER` (default 5), jump the processing queue and can use the 💎 styles
(3D, Realistic, Chibi, Pixel art, Hand-drawn). Grant/revoke with `/premium`. No payment credentials exist in the code.

---

## 4. Configuration (`.env`)

| Variable | Default | Notes |
|---|---|---|
| `BOT_TOKEN` | – | **Required**, from @BotFather |
| `MONGO_URL` | `mongodb://mongo:27017` | **Required** |
| `OPENAI_API_KEY` | empty | Needed only for AI stickers / photo styles. Without it those buttons explain it is not configured |
| `ADMIN_IDS` | empty | Comma-separated numeric Telegram IDs |
| `PACK_CREDIT` | `@Advik_prakashh` | Appended to pack titles |
| `BOT_USERNAME` | auto | Detected from Telegram if empty |
| `MAX_DAILY_GENERATIONS` / `MAX_DAILY_SEARCHES` | 15 / 30 | Free-user daily limits |
| `MAX_CONCURRENT_TASKS` | 5 | Concurrent heavy jobs (AI / image processing) |
| `MAX_IMAGE_SIZE_MB` | 15 | Max download/upload size |
| `MAX_PACK_STICKERS` | 120 | Per pack (Telegram max for static packs is 120) |
| `PIXABAY_API_KEY` | empty | Optional, enables the Pixabay provider |
| `ENABLE_PINTEREST`, `ENABLE_OPENVERSE` | true | Search providers |
| `IMAGE_MODEL`, `IMAGE_QUALITY` | `gpt-image-1`, `medium` | OpenAI image model |
| `REMBG_MODEL` | `u2netp` | Small/fast; `u2net` is more accurate but ~170 MB |
| `LOG_LEVEL` | `INFO` | |

See `.env.example` for the complete list. **Never commit `.env`** (it is in `.gitignore`).

---

## 5. How image search works (and what it does not do)

* Pinterest’s official API needs OAuth and offers no anonymous public image search, so there is **no fake Pinterest
  endpoint** here. The Pinterest-compatible provider reads a public web-search index restricted to `pinterest.com`
  (via the `ddgs` package) and keeps **only results hosted on Pinterest’s public image CDN (`i.pinimg.com`)**. A Pinterest
  *page* URL is never treated as an image. It never logs in, never touches private boards and never bypasses any protection.
  Pinterest results carry no licence info — the bot says so and links to the source page.
* **Pixabay** (API key) and **Openverse** (no key) are supported, licence-aware alternatives; Openverse results include
  licence and attribution, which are shown under each preview.
* Providers are tried in order; if one is unavailable the next is used. Results are cached for 10 minutes, filtered
  (broken/unsupported/tiny/duplicate) and stored per search so pagination works after restarts.
* Downloads are SSRF-safe: http/https on ports 80/443 only, every redirect re-validated, DNS resolved once and the
  connection pinned to a public IP, byte/time limits, and the bytes must decode as a real JPEG/PNG/WEBP/GIF.

Please respect image owners’ rights: a sticker made from someone else’s picture may still be their copyrighted work.

---

## 6. Telegram sticker notes

* Created with the official Bot API methods `createNewStickerSet` and `addStickerToSet`, using
  `InputSticker(format="static", emoji_list=[…])`. Static WEBP, 512×512, ≤ 512 KB.
* Telegram has **no filename field** for stickers. What users see is the pack **title**, each sticker’s **emoji**
  and the pack link; that is what the bot sets.
* Pack names follow Telegram’s rules (`a-z0-9_`, begins with a letter, no `__`, ends with `_by_<botusername>`).
* Animated (TGS) and video (WEBM) stickers are not supported.
* Emoji typed into a text sticker become the sticker’s emoji metadata; they are not drawn into the picture (Pillow
  cannot render colour emoji without extra fonts).
* Custom fonts: drop `.ttf`/`.otf` files into `data/fonts/` (the bundled DejaVu fonts are used by default).
* FloodWait (`retry_after`) is honoured automatically and network errors are retried.

---

## 7. Tests

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

Tests use mocked Telegram/OpenAI/MongoDB (in-memory fakes) and a local HTTP server for the downloader – no credentials needed.
They cover: startup wiring, database connection, image download + SSRF, image validation, background removal
(mock backend, plus the real `rembg` when installed), WEBP conversion, sticker-pack creation (rollover, FloodWait,
errors), duplicate detection, free/premium limits, admin authorisation and error handling.

## 8. Project layout

```
bot/
  main.py  config.py  database.py  keyboards.py  middleware.py  context.py  limits.py
  handlers/  start search ai_sticker photo text_sticker packs profile admin (+ common)
  services/  pinterest image_search downloader image_processor background_removal
             ai_generator sticker_manager category_detector (+ search_types)
  utils/     validators logger helpers
tests/  data/  logs/  Dockerfile  docker-compose.yml  telegram-ai-sticker-bot.service  .env.example
```

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| `Configuration error: Missing … BOT_TOKEN` | Create `.env` from `.env.example` |
| Bot starts but AI buttons say “not configured” | Set `OPENAI_API_KEY`; check your OpenAI account has image access |
| First photo is slow | `rembg` downloads its model (~4 MB for `u2netp`) once into `data/models` |
| “Background removal isn’t installed” | `pip install -r requirements.txt` failed for rembg/onnxruntime; stickers are still made with the original background |
| Pack not created | Open the bot and press **Start** first; check logs for Telegram’s error text; names must be unique |
| `/find` finds nothing | Add `PIXABAY_API_KEY`, or check outbound network access |

## 10. Honest status

Everything was verified by static checks and automated tests with mocked services (see the final report in the
delivery message). **Not verified against live services:** real Telegram API calls, a real OpenAI image generation,
the Pinterest/Pixabay/Openverse providers, a real MongoDB server, the Docker image build, and handlers executing under
the real aiogram package. Run the tests (section 7) and a manual end-to-end check on your server (`/start`, `/find`, send a photo, `/sticker`, publish a pack) before going public.

MIT License.
