"""OpenAI Images API integration (generation + photo stylisation) and optional vision tagging."""
from __future__ import annotations

import base64
import io
import logging

from .category_detector import PICKER_ORDER

log = logging.getLogger(__name__)

# key -> (label, emoji, premium_only, style prompt)
STYLES: dict = {
    "anime": ("Anime", "🌸", False, "anime illustration, large expressive eyes, clean cel shading, bold clean outlines"),
    "kawaii": ("Kawaii", "🍡", False, "kawaii character, tiny body, blushing cheeks, pastel palette, adorable"),
    "cute": ("Cute", "🧸", False, "super cute cartoon character, rounded shapes, big shiny eyes, soft colours"),
    "3d": ("3D", "🧊", True, "glossy 3D rendered character, soft studio lighting, smooth plastic material"),
    "realistic": ("Realistic", "📷", True, "highly detailed realistic render with natural lighting, still sticker-friendly"),
    "cartoon": ("Cartoon", "🎨", False, "classic cartoon style, thick outlines, flat bright colours"),
    "chibi": ("Chibi", "🐣", True, "chibi proportions, oversized head, tiny body, expressive face"),
    "pixel": ("Pixel art", "👾", True, "retro pixel art, limited palette, crisp pixels"),
    "minimal": ("Minimalist", "⚪", False, "minimalist flat vector design, simple shapes, 3 to 4 colours"),
    "meme": ("Meme", "🤣", False, "bold funny meme reaction sticker, exaggerated expression, crisp cartoon style"),
    "food": ("Food illustration", "🍓", False, "delicious food illustration, appetising colours, glossy highlights, cute face optional"),
    "pastel": ("Pastel aesthetic", "🧁", False, "soft pastel aesthetic, dreamy gradients, gentle shading"),
    "handdrawn": ("Hand-drawn", "✏️", True, "hand-drawn sketch look, ink linework with watercolour fill, visible pencil texture"),
}
DEFAULT_STYLE = "cute"


class AINotConfigured(RuntimeError):
    pass


class AIUnavailable(RuntimeError):
    """Feature not supported by the configured model."""


def style_label(key: str) -> str:
    return STYLES.get(key, STYLES[DEFAULT_STYLE])[0]


def is_premium_style(key: str) -> bool:
    return STYLES.get(key, ("", "", False, ""))[2]


def build_prompt(prompt: str, style: str) -> str:
    style_text = STYLES.get(style, STYLES[DEFAULT_STYLE])[3]
    return (
        f"Create ONE standalone sticker illustration. Subject: {prompt.strip()}. "
        f"Visual style: {style_text}. "
        "Centered subject with a clear readable silhouette, thick white sticker outline, "
        "plain transparent background, no mockup, no sticker sheet, no frame, no watermark, "
        "no extra objects. Text only if the subject explicitly asks for it. Square composition."
    )


def friendly_error(exc: BaseException) -> str:
    """Map provider exceptions to short user-facing text (never leaks keys or raw responses)."""
    name = type(exc).__name__
    text = str(exc).lower()
    if isinstance(exc, AINotConfigured):
        return "AI features are not configured on this bot yet."
    if isinstance(exc, AIUnavailable):
        return str(exc)
    if "moderation" in text or "content_policy" in text or "safety" in text:
        return "That prompt was declined by the image safety system. Try a different idea 💗"
    if name == "RateLimitError" or "rate limit" in text:
        return "The AI service is busy right now. Please try again in a minute."
    if name in ("APITimeoutError", "TimeoutError") or "timed out" in text:
        return "The AI service took too long. Please try again."
    if name in ("AuthenticationError", "PermissionDeniedError"):
        return "The AI service rejected the bot's credentials. The owner has been notified."
    if name in ("APIConnectionError",):
        return "I couldn't reach the AI service. Please try again shortly."
    return "Something went wrong while creating the image. Please try again."


class AIGenerator:
    def __init__(self, api_key: str, model: str = "gpt-image-1", quality: str = "medium",
                 vision_model: str = "gpt-4o-mini", timeout: float = 120.0, client=None):
        self.api_key = api_key
        self.model = model
        self.quality = quality
        self.vision_model = vision_model
        self.timeout = timeout
        self._client = client

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) or self._client is not None

    @property
    def _is_gpt_image(self) -> bool:
        return self.model.startswith("gpt-image")

    def _get_client(self):
        if not self.enabled:
            raise AINotConfigured("OPENAI_API_KEY is not set.")
        if self._client is None:
            from openai import AsyncOpenAI  # lazy: bot works without the key

            self._client = AsyncOpenAI(api_key=self.api_key, timeout=self.timeout, max_retries=2)
        return self._client

    @staticmethod
    def _decode(response) -> bytes:
        item = response.data[0]
        b64 = getattr(item, "b64_json", None)
        if not b64:
            raise RuntimeError("Image API returned no image data.")
        return base64.b64decode(b64)

    async def generate(self, prompt: str, style: str) -> bytes:
        client = self._get_client()
        kwargs: dict = dict(model=self.model, prompt=build_prompt(prompt[:700], style), size="1024x1024", n=1)
        if self._is_gpt_image:
            # Passed via extra_body so it works across openai-python 1.x releases.
            kwargs["extra_body"] = {"background": "transparent", "output_format": "png", "quality": self.quality}
        else:
            kwargs["response_format"] = "b64_json"
        return self._decode(await client.images.generate(**kwargs))

    async def stylize_photo(self, png_bytes: bytes, style: str) -> bytes:
        if not self._is_gpt_image:
            raise AIUnavailable("Photo styles need the gpt-image-1 model (set IMAGE_MODEL=gpt-image-1).")
        client = self._get_client()
        style_text = STYLES.get(style, STYLES[DEFAULT_STYLE])[3]
        prompt = (
            f"Turn this photo into a single sticker illustration in this style: {style_text}. "
            "Keep the subject recognisable, thick white outline, transparent background, no text."
        )
        response = await client.images.edit(
            model=self.model, image=("photo.png", io.BytesIO(png_bytes), "image/png"),
            prompt=prompt, size="1024x1024", n=1,
            extra_body={"background": "transparent", "output_format": "png", "quality": self.quality},
        )
        return self._decode(response)

    async def classify(self, jpeg_bytes: bytes) -> str | None:
        """Optional: ask a vision model for a category key. Returns None if unsure."""
        client = self._get_client()
        keys = [k for k in PICKER_ORDER if k != "general"]
        b64 = base64.b64encode(jpeg_bytes).decode()
        response = await client.chat.completions.create(
            model=self.vision_model, max_tokens=8,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": "Pick the single best category for this image from: "
                 + ", ".join(keys) + ". Reply with only the category word."},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]}],
        )
        answer = (response.choices[0].message.content or "").strip().lower().replace(" ", "_")
        return answer if answer in keys else None
