"""OpenAI Images API integration (generation + photo stylisation) and optional vision tagging."""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import threading

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


def build_prompt(prompt: str, style: str, transparent: bool = True) -> str:
    style_text = STYLES.get(style, STYLES[DEFAULT_STYLE])[3]
    bg = "plain transparent background" if transparent else "plain solid white background"
    return (
        f"Create ONE standalone sticker illustration. Subject: {prompt.strip()}. "
        f"Visual style: {style_text}. "
        f"Centered subject with a clear readable silhouette, thick white sticker outline, "
        f"{bg}, no mockup, no sticker sheet, no frame, no watermark, "
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
                 vision_model: str = "gpt-4o-mini", timeout: float = 120.0, client=None,
                 providers: list | None = None, downloader=None, local_model: str = "stabilityai/sd-turbo"):
        self.downloader = downloader
        self.local_model = local_model
        self._pipe = None
        self._local_lock = threading.Lock()
        # Provider chain, tried in order: openai (key needed) -> pollinations (free hosted, no key) -> local (CPU)
        wanted = providers if providers is not None else ["openai", "pollinations"]
        self.providers = [p for p in wanted
                          if (p == "openai" and (bool(api_key) or client is not None))
                          or (p == "pollinations" and downloader is not None) or p == "local"]
        self.api_key = api_key
        self.model = model
        self.quality = quality
        self.vision_model = vision_model
        self.timeout = timeout
        self._client = client

    @property
    def enabled(self) -> bool:
        return bool(self.providers)

    @property
    def openai_enabled(self) -> bool:
        return bool(self.api_key) or self._client is not None

    @property
    def _is_gpt_image(self) -> bool:
        return self.model.startswith("gpt-image")

    def _get_client(self):
        if not self.openai_enabled:
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
        last: Exception | None = None
        for name in self.providers:
            try:
                if name == "openai":
                    return await self._gen_openai(prompt, style)
                if name == "pollinations":
                    return await self._gen_pollinations(prompt, style)
                if name == "local":
                    return await self._gen_local(prompt, style)
            except Exception as exc:  # noqa: BLE001 - fall through to the next provider
                last = exc
                log.warning("AI provider %s failed: %s: %s", name, type(exc).__name__, str(exc)[:160])
        if last is not None:
            raise last
        raise AINotConfigured("No AI provider is configured.")

    async def _gen_pollinations(self, prompt: str, style: str) -> bytes:
        """Free hosted image generation (no key). Result is opaque; background removal makes it a cut-out."""
        import random
        from urllib.parse import quote

        text = build_prompt(prompt[:300], style, transparent=False)[:900]
        url = (f"https://image.pollinations.ai/prompt/{quote(text, safe='')}"
               f"?width=768&height=768&nologo=true&seed={random.randint(1, 10**6)}")
        res = await self.downloader.fetch(url, accept=("image/",), max_bytes=10 * 1024 * 1024, total_timeout=100)
        return res.data

    def _local_sync(self, text: str) -> bytes:
        try:
            import torch  # noqa: F401
            from diffusers import AutoPipelineForText2Image
        except ImportError as exc:
            raise AIUnavailable("Local generator not installed (build with INSTALL_LOCAL_AI=1).") from exc
        with self._local_lock:
            if self._pipe is None:
                import torch
                pipe = AutoPipelineForText2Image.from_pretrained(self.local_model, torch_dtype=torch.float32)
                pipe.set_progress_bar_config(disable=True)
                self._pipe = pipe
            image = self._pipe(prompt=text, num_inference_steps=2, guidance_scale=0.0, width=512, height=512).images[0]
        buf = io.BytesIO()
        image.save(buf, "PNG")
        return buf.getvalue()

    async def _gen_local(self, prompt: str, style: str) -> bytes:
        """CPU image generation on this server (SD-Turbo via diffusers). Slow (≈20-90 s) but free."""
        return await asyncio.to_thread(self._local_sync, build_prompt(prompt[:300], style, transparent=False))

    async def _gen_openai(self, prompt: str, style: str) -> bytes:
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
