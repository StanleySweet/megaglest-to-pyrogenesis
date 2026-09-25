"""Audio conversion (WAV/MP3 → OGG Vorbis) and 0 A.D. sound-group XML."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from lxml import etree

from ..core.errors import ConversionError

_DEFAULT_FFMPEG = Path("/opt/homebrew/bin/ffmpeg")


class AudioConverter:
    """Convert audio to OGG Vorbis via ffmpeg; emit sound-group XML.

    pydub is avoided: it is uninstallable on Python 3.13+ (its ``audioop``
    dependency was removed from the stdlib), so ffmpeg is driven directly.
    """

    def __init__(self, ffmpeg: Path | None = None) -> None:
        self._ffmpeg = str(ffmpeg) if ffmpeg is not None else str(_DEFAULT_FFMPEG)
        if not Path(self._ffmpeg).exists():
            self._ffmpeg = "ffmpeg"

    def convert_wav_to_ogg(self, source: Path, output: Path) -> None:
        """Convert a WAV file to OGG Vorbis (~q:a 6, 192 kbps VBR).

        Sample rate and channel count are preserved from the source.
        """
        self._export(source, output)

    def convert_to_ogg(self, source: Path, output: Path) -> None:
        """Convert any supported input (WAV/MP3) to OGG; OGG is copied."""
        if source.suffix.lower() == ".ogg":
            self.copy_ogg(source, output)
            return
        self._export(source, output)

    def _export(self, source: Path, output: Path) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        command = [
            self._ffmpeg,
            "-y",
            "-i",
            str(source),
            "-c:a",
            "libvorbis",
            "-q:a",
            "6",
            str(output),
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=False)
        except OSError as exc:
            raise ConversionError(f"cannot run ffmpeg: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise ConversionError(
                f"ffmpeg failed on {source.name}: {detail[-1] if detail else 'unknown error'}"
            )

    def copy_ogg(self, source: Path, output: Path) -> None:
        """Copy an already-OGG sound verbatim (0 A.D. plays OGG natively)."""
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(source, output)
        except OSError as exc:
            raise ConversionError(f"cannot copy audio {source}: {exc}") from exc

    def write_sound_group(
        self,
        group_path: Path,
        sounds_dir: str,
        sound_names: list[str],
    ) -> None:
        """Write one 0 A.D. sound-group XML (random selection within a set).

        ``sounds_dir`` is the mod-relative directory of the sounds (e.g.
        ``audio/sfx/demo/``); ``sound_names`` are basenames inside it.
        """
        root = etree.Element("SoundGroup")
        for tag, value in (
            ("Gain", "1"),
            ("Priority", "100"),
            ("ConeGain", "1"),
            ("Looping", "0"),
            ("RandOrder", "1"),
            ("RandGain", "0"),
            ("GainLower", "0"),
            ("RandPitch", "1"),
            ("PitchUpper", "1.03"),
            ("Threshold", "0"),
        ):
            etree.SubElement(root, tag).text = value
        etree.SubElement(root, "Path").text = sounds_dir
        for name in sound_names:
            etree.SubElement(root, "Sound").text = name
        data = etree.tostring(
            root,
            xml_declaration=True,
            encoding="UTF-8",
            pretty_print=True,
        )
        group_path.parent.mkdir(parents=True, exist_ok=True)
        group_path.write_bytes(data)
