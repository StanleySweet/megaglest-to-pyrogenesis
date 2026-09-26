"""Shared constants for pack scanning."""

from __future__ import annotations

MESH_SUFFIXES = frozenset({".g3d"})
TEXTURE_SUFFIXES = frozenset({".bmp", ".png", ".tga", ".jpg", ".jpeg"})
SOUND_SUFFIXES = frozenset({".wav", ".ogg"})
MUSIC_SUFFIXES = frozenset({".ogg", ".mp3"})
MAP_SUFFIXES = frozenset({".mgm", ".gbm"})

LOADING_SCREEN_GLOBS = (
    "loading_screen.png",
    "loading_screen.jpg",
    "loading_screen.jpeg",
    "loading_screen.bmp",
    "loading_screen.tga",
)
