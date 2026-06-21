#!/usr/bin/env python3
"""Bitwarden-style passphrase generator: Word4-Word-Word.

Reads /usr/share/dict/words on macOS/Linux. Falls back to a small
embedded list if the system wordlist is unavailable.
"""
from __future__ import annotations
import secrets
import sys
from pathlib import Path

FALLBACK = """
Anchor Bridge Cabin Dragon Ember Forest Garden Harbor Island Jungle
Kettle Lantern Meadow Nimble Opal Petal Quartz Ribbon Saddle Tundra
Umber Valley Whisper Yonder Zephyr Cascade Borough Cipher Drifting
Elegant Fortune Gallant Helmet Inkwell Justice Kindred Lattice
Mosaic Notation Outpost Pioneer Quotient Roaming Solstice Tempest
Upright Voyage Wander Yielding Zealous Atlas Beacon Caravan
""".split()

MIN_LEN = 4
MAX_LEN = 9


def load_words() -> list[str]:
    sys_path = Path("/usr/share/dict/words")
    if sys_path.is_file():
        words = []
        for line in sys_path.read_text().splitlines():
            w = line.strip()
            if MIN_LEN <= len(w) <= MAX_LEN and w.isalpha():
                words.append(w.capitalize())
        if len(words) >= 50:
            return words
    return [w.capitalize() for w in FALLBACK]


def main() -> int:
    # Five words ≥ 32 chars. Mattermost AtRestEncryptKey requires 32 chars
    # min; many other apps cap usefulness around 64. Five words at ~6 chars
    # avg + dashes + digit lands ~33–45 chars: comfortably above the floor,
    # below typical caps, and still memorable.
    words = load_words()
    parts = [secrets.choice(words) for _ in range(5)]
    digit = secrets.randbelow(10)
    print(f"{parts[0]}{digit}-{parts[1]}-{parts[2]}-{parts[3]}-{parts[4]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
