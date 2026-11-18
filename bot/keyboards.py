"""Inline keyboards. Callback data prefixes (all <= 64 bytes):

menu:*  main menu      qs:<cat>  quick search     sr:<n|d|c|a>:<sid>:<i>  search results
ai:s:<style>           sk:<q|c|g>:<id>   sticker actions   sc:<id>:<cat>  set category
ph:*  photo options    tx:*  text sticker          pk:*  packs           set:*  settings
adm:* admin
"""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton as B, InlineKeyboardMarkup as M

from .services.ai_generator import STYLES
from .services.category_detector import PICKER_ORDER, category_emoji, category_label
from .services.image_processor import PRESET_TEXTS


def _chunk(buttons: list, n: int) -> list:
    return [buttons[i:i + n] for i in range(0, len(buttons), n)]


def main_menu() -> M:
    rows = [
        [B(text="🔍 Search Stickers", callback_data="menu:search")],
        [B(text="🎨 AI Sticker Generator", callback_data="menu:ai")],
        [B(text="📸 Photo to Sticker", callback_data="menu:photo")],
        [B(text="🧸 Cute Stickers", callback_data="qs:cute")],
        [B(text="🍓 Food Stickers", callback_data="qs:food")],
        [B(text="💗 Lovely Stickers", callback_data="qs:lovely")],
        [B(text="✍️ Text Stickers", callback_data="menu:text")],
        [B(text="📦 My Sticker Packs", callback_data="menu:packs")],
        [B(text="⚙️ Settings", callback_data="menu:settings")],
    ]
    return M(inline_keyboard=rows)


def back_menu() -> M:
    return M(inline_keyboard=[[B(text="🏠 Main menu", callback_data="menu:main")]])


def search_categories() -> M:
    btns = [B(text=f"{category_emoji(k)} {category_label(k)}", callback_data=f"qs:{k}")
            for k in PICKER_ORDER if k != "general"]
    rows = _chunk(btns, 2) + [[B(text="🏠 Main menu", callback_data="menu:main")]]
    return M(inline_keyboard=rows)


def category_picker(sticker_id: str) -> M:
    btns = [B(text=f"{category_emoji(k)} {category_label(k)}", callback_data=f"sc:{sticker_id}:{k}")
            for k in PICKER_ORDER]
    return M(inline_keyboard=_chunk(btns, 2))


def style_picker(premium: bool, default_style: str) -> M:
    btns = []
    for key, (label, emoji, prem_only, _) in STYLES.items():
        mark = " 💎" if prem_only and not premium else ""
        btns.append(B(text=f"{emoji} {label}{mark}", callback_data=f"ai:s:{key}"))
    rows = _chunk(btns, 2)
    rows.append([B(text="❌ Cancel", callback_data="menu:main")])
    return M(inline_keyboard=rows)


def search_nav(sid: str, idx: int, total: int, page_url: str = "") -> M:
    prev_i, next_i = (idx - 1) % total, (idx + 1) % total
    rows = [
        [B(text="⬅️ Previous", callback_data=f"sr:n:{sid}:{prev_i}"),
         B(text=f"{idx + 1}/{total}", callback_data="noop"),
         B(text="Next ➡️", callback_data=f"sr:n:{sid}:{next_i}")],
        [B(text="⬇️ Download", callback_data=f"sr:d:{sid}:{idx}"),
         B(text="✨ Create sticker", callback_data=f"sr:c:{sid}:{idx}")],
        [B(text="📦 Add to pack", callback_data=f"sr:a:{sid}:{idx}")],
    ]
    if page_url.startswith(("http://", "https://")) and len(page_url) < 512:
        rows.append([B(text="🔗 Source", url=page_url)])
    rows.append([B(text="🏠 Main menu", callback_data="menu:main")])
    return M(inline_keyboard=rows)


def sticker_actions(sticker_id: str, category: str, queued: bool = False, admin: bool = False, pack_url: str = "") -> M:
    first = (B(text="➕ Add this pack to Telegram", url=pack_url) if pack_url else B(text="📦 Preview & publish pack", callback_data="pk:preview") if queued
             else B(text="➕ Add to pack", callback_data=f"sk:q:{sticker_id}"))
    rows = [[first], [B(text=f"🏷 Category: {category_label(category)}", callback_data=f"sk:c:{sticker_id}")]]
    if admin:
        rows.append([B(text="🌐 Add to category pack", callback_data=f"sk:g:{sticker_id}")])
    return M(inline_keyboard=rows)


def photo_options(opts: dict) -> M:
    def tick(flag: bool, label: str) -> str:
        return f"✅ {label}" if flag else label
    rows = [
        [B(text="🧸 Cute", callback_data="ph:style:cute"), B(text="🌸 Anime", callback_data="ph:style:anime")],
        [B(text="🧊 3D", callback_data="ph:style:3d"), B(text="🤣 Meme", callback_data="ph:style:meme")],
        [B(text=tick(opts.get("bg") != "keep", "Remove Background"), callback_data="ph:t:bg")],
        [B(text=tick(opts.get("outline") == "white", "Add White Border"), callback_data="ph:t:white"),
         B(text=tick(opts.get("outline") == "black", "Add Black Border"), callback_data="ph:t:black")],
        [B(text=tick(opts.get("shadow", False), "Add Shadow"), callback_data="ph:t:shadow"),
         B(text=tick(opts.get("crop", True), "Crop Subject"), callback_data="ph:t:crop")],
        [B(text=tick(bool(opts.get("text")), "Add Text"), callback_data="ph:text"),
         B(text=tick(bool(opts.get("emoji")), "Add Emoji"), callback_data="ph:emoji")],
        [B(text="✨ Create Sticker", callback_data="ph:go")],
        [B(text="🖼 Convert to WEBP", callback_data="ph:webp"), B(text="📦 Add to Pack", callback_data="ph:pack")],
        [B(text="❌ Cancel", callback_data="menu:main")],
    ]
    return M(inline_keyboard=rows)


def text_presets() -> M:
    btns = [B(text=t, callback_data=f"tx:p:{i}") for i, t in enumerate(PRESET_TEXTS)]
    rows = _chunk(btns, 2) + [[B(text="✏️ Type my own text", callback_data="tx:custom")],
                              [B(text="🏠 Main menu", callback_data="menu:main")]]
    return M(inline_keyboard=rows)


def text_options(font: str, color: str, outline: str, bg: str) -> M:
    return M(inline_keyboard=[
        [B(text=f"🔤 Font: {font}"[:40], callback_data="tx:font"), B(text=f"🎨 Colour: {color}", callback_data="tx:color")],
        [B(text=f"🖍 Outline: {outline}", callback_data="tx:outline"), B(text=f"🌈 Background: {bg}", callback_data="tx:bg")],
        [B(text="✨ Create sticker", callback_data="tx:go")],
        [B(text="🏠 Main menu", callback_data="menu:main")],
    ])


def packs_menu(has_queue: bool) -> M:
    rows = []
    if has_queue:
        rows.append([B(text="👀 Preview & publish", callback_data="pk:preview")])
        rows.append([B(text="🗑 Clear queue", callback_data="pk:clear")])
    rows.append([B(text="🏠 Main menu", callback_data="menu:main")])
    return M(inline_keyboard=rows)


def pack_preview() -> M:
    return M(inline_keyboard=[
        [B(text="✅ Publish pack", callback_data="pk:go")],
        [B(text="🗑 Clear queue", callback_data="pk:clear"), B(text="⬅️ Back", callback_data="menu:packs")],
    ])


def pack_links(links: list) -> M:
    rows = [[B(text=f"➕ Add {title}"[:60], url=url)] for title, url in links[:8]]
    rows.append([B(text="🏠 Main menu", callback_data="menu:main")])
    return M(inline_keyboard=rows)


def settings_menu(s: dict) -> M:
    bg_labels = {"auto": "Automatic", "keep": "Keep original", "transparent": "Transparent",
                 "white": "White", "custom": "Custom colour"}
    return M(inline_keyboard=[
        [B(text=f"🖼 Background: {bg_labels.get(s.get('bg_mode', 'auto'), 'Automatic')}", callback_data="set:bg")],
        [B(text=f"🔲 Border: {s.get('outline', 'none')}", callback_data="set:outline")],
        [B(text=f"🌫 Shadow: {'on' if s.get('shadow') else 'off'}", callback_data="set:shadow")],
        [B(text="🎨 Default AI style", callback_data="set:style")],
        [B(text="👤 My profile", callback_data="set:profile")],
        [B(text="🏠 Main menu", callback_data="menu:main")],
    ])


def default_style_picker() -> M:
    btns = [B(text=f"{e} {l}", callback_data=f"set:st:{k}") for k, (l, e, _, _) in STYLES.items()]
    return M(inline_keyboard=_chunk(btns, 2) + [[B(text="⬅️ Back", callback_data="menu:settings")]])


def admin_panel(maintenance: bool) -> M:
    return M(inline_keyboard=[
        [B(text="🔄 Refresh", callback_data="adm:refresh"), B(text="👥 Users", callback_data="adm:users")],
        [B(text=f"🛠 Maintenance: {'ON' if maintenance else 'off'}", callback_data="adm:maint")],
    ])


def confirm_broadcast(n: int) -> M:
    return M(inline_keyboard=[[B(text=f"✅ Send to {n} users", callback_data="adm:bc_yes"),
                               B(text="❌ Cancel", callback_data="adm:bc_no")]])
