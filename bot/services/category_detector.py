"""Keyword based category detection (+ optional vision fallback handled in ai_generator)."""
from __future__ import annotations

import re

# key -> (label, emoji, keywords). Order = tie-break priority (most specific first).
CATEGORIES: dict = {
    "baby_animals": ("Baby Animals", "🐣", ["baby animal", "baby animals", "puppy", "kitten", "chick", "bunny", "cub", "fawn", "duckling", "lamb"]),
    "cats": ("Cats", "🐱", ["cat", "kitten", "kitty", "meow", "feline"]),
    "dogs": ("Dogs", "🐶", ["dog", "puppy", "doggo", "corgi", "retriever", "husky"]),
    "coffee": ("Coffee", "☕", ["coffee", "latte", "espresso", "cappuccino", "mocha", "cafe"]),
    "desserts": ("Desserts", "🍰", ["dessert", "cake", "cupcake", "ice cream", "icecream", "donut", "doughnut", "cookie", "macaron", "pastry", "pudding", "chocolate"]),
    "fruits": ("Fruits", "🍓", ["fruit", "strawberry", "apple", "banana", "peach", "cherry", "watermelon", "mango", "lemon", "orange", "grape"]),
    "flowers": ("Flowers", "🌸", ["flower", "rose", "tulip", "blossom", "sakura", "daisy", "bouquet", "lily", "sunflower", "floral"]),
    "food": ("Food", "🍔", ["food", "pizza", "burger", "sushi", "ramen", "noodle", "pasta", "sandwich", "fries", "meal", "snack", "taco", "rice"]),
    "anime": ("Anime", "🌸", ["anime", "manga", "waifu", "otaku", "chibi"]),
    "kawaii": ("Kawaii", "🍡", ["kawaii"]),
    "memes": ("Memes", "🤣", ["meme", "memes", "reaction"]),
    "funny": ("Funny", "😂", ["funny", "lol", "hilarious", "joke", "silly"]),
    "couple": ("Couple", "💑", ["couple", "boyfriend", "girlfriend", "wedding", "romantic"]),
    "love": ("Love", "❤️", ["love", "heart", "valentine", "kiss", "hug"]),
    "lovely": ("Lovely", "💗", ["lovely", "adorable", "darling"]),
    "pink": ("Pink Aesthetic", "🌷", ["pink aesthetic", "pink", "rose pink", "blush"]),
    "pastel": ("Pastel Aesthetic", "🧁", ["pastel aesthetic", "pastel"]),
    "aesthetic": ("Aesthetic", "✨", ["aesthetic", "vibes", "vintage", "dreamy", "soft"]),
    "girly": ("Girly", "🎀", ["girly", "bow", "princess", "makeup", "ribbon", "barbie"]),
    "nature": ("Nature", "🌿", ["nature", "forest", "mountain", "ocean", "sunset", "tree", "leaf", "landscape", "beach"]),
    "cartoon": ("Cartoon", "🎨", ["cartoon", "doodle", "illustration", "comic", "mascot"]),
    "sweet": ("Sweet", "🍬", ["sweet", "candy", "sugar", "honey", "lollipop"]),
    "cute": ("Cute", "🧸", ["cute", "cutie", "teddy", "plush", "bear", "adorbs"]),
    "general": ("Stickers", "✨", []),
}
PICKER_ORDER = [k for k in CATEGORIES if k != "general"] + ["general"]
DEFAULT_CATEGORY = "general"
# Quick-search shortcuts shown on the main menu.
QUICK_QUERIES = {"cute": "cute", "food": "food", "lovely": "lovely"}


def category_label(key: str) -> str:
    return CATEGORIES.get(key, CATEGORIES[DEFAULT_CATEGORY])[0]


def category_emoji(key: str) -> str:
    return CATEGORIES.get(key, CATEGORIES[DEFAULT_CATEGORY])[1]


def valid_category(key: str) -> bool:
    return key in CATEGORIES


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def detect_category(*hints: str) -> str | None:
    """Return the best category key for free text (query, title, tags, caption) or None."""
    text = " ".join(h for h in hints if h).lower()
    if not text.strip():
        return None
    words = {_stem(w) for w in re.findall(r"[a-z0-9']+", text)}
    best_key, best_score = None, 0
    for key, (_, _, keywords) in CATEGORIES.items():
        score = 0
        for kw in keywords:
            if " " in kw:
                if kw in text:
                    score += 3
            elif _stem(kw) in words:
                score += 1
        if score > best_score:  # strict '>' keeps earlier (more specific) key on ties
            best_key, best_score = key, score
    return best_key


# Extra packs of the same category get a similar-but-different name, e.g. "Food Vibes : @Advik_prakashh".
PART_WORDS = ["Vibes", "Treats", "Club", "Mood", "Magic", "Dreams", "Party", "Cuties", "Love", "Joy"]


def pack_title(key: str, credit: str, part: int = 1) -> str:
    """Pack title format required by the project: 'Food : @Advik_prakashh'."""
    label = category_label(key)
    if part > 1:
        idx = part - 2
        word = PART_WORDS[idx % len(PART_WORDS)] + (f" {idx // len(PART_WORDS) + 1}" if idx >= len(PART_WORDS) else "")
        label = f"{label} {word}"
    credit = credit.strip()
    suffix = f" : {credit}"
    return (label[: max(1, 64 - len(suffix))] + suffix)[:64]
