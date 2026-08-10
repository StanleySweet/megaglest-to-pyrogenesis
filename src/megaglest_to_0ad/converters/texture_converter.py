"""Texture conversion (MegaGlest TGA/BMP/JPG → 0 A.D. PNG)."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from ..core.errors import ConversionError

_TEXTURE_EXTS = (".tga", ".bmp", ".png", ".jpg", ".jpeg")


def texture_stem(name: str) -> str:
    """File stem with repeated texture extensions collapsed.

    Some packs double the extension (``texture_ashes_magic.tga.tga``); the
    on-disk file carries a single suffix, so the doubled one is stripped.
    """
    stem = name
    for _ in range(2):
        lower = stem.lower()
        matched = next((ext for ext in _TEXTURE_EXTS if lower.endswith(ext)), None)
        if matched is None:
            break
        stem = stem[: -len(matched)]
    return stem


def _next_pot(value: int) -> int:
    """Smallest power of two >= ``value`` (1, 2, 4, 8, ...)."""
    return 1 << (value - 1).bit_length()


class TextureConverter:
    """Convert image files to RGBA PNG with maximum lossless compression."""

    def convert_to_png(self, source: Path, output: Path) -> None:
        """Convert ``source`` (TGA/BMP/JPG/PNG) to an RGBA PNG at ``output``.

        Alpha is preserved; grayscale and palette images are promoted to RGBA
        so material binds stay consistent. Non-power-of-two images are
        upscaled to the next POT: 0 A.D.'s archive builder otherwise warns
        and rescales each one at archive time.
        """
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            with Image.open(source) as img:
                if img.mode not in ("RGBA", "LA"):
                    img = img.convert("RGBA")
                width, height = img.size
                if width != _next_pot(width) or height != _next_pot(height):
                    img = img.resize(
                        (_next_pot(width), _next_pot(height)), Image.Resampling.LANCZOS
                    )
                img.save(output, "PNG", compress_level=9)
        except OSError as exc:
            raise ConversionError(f"cannot convert texture {source}: {exc}") from exc
