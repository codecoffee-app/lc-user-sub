#!/usr/bin/env python3
"""Create missing problem folders with encoded config.txt and 1.txt."""

from __future__ import annotations

import json
import random
from pathlib import Path


def encode(s: str) -> str:
    if not isinstance(s, str):
        raise TypeError("Input for encode must be a string.")
    if len(s) == 0:
        return ""

    shift_offset = random.randint(1, 25)
    encoded_chars = []
    for i, ch in enumerate(s):
        shifted = ord(ch) + (i % shift_offset) + shift_offset
        encoded_chars.append(shifted)

    segment_lengths = list(range(len(encoded_chars)))
    for i in range(len(segment_lengths) - 1, 0, -1):
        j = random.randint(0, i)
        segment_lengths[i], segment_lengths[j] = (
            segment_lengths[j],
            segment_lengths[i],
        )

    encoded_chars_with_lengths = [encoded_chars[el] for el in segment_lengths]
    encoded_sequence_string = "".join(f"{num:07d}" for num in segment_lengths)
    encoded_chars_string = "".join(
        f"{num:05d}" for num in encoded_chars_with_lengths
    )
    return f"{shift_offset}_{encoded_sequence_string}_{encoded_chars_string}"


def main() -> None:
    root = Path(__file__).resolve().parent
    input_path = root / "input.json"
    problems_dir = root / "problems"

    with input_path.open(encoding="utf-8") as f:
        items = json.load(f)

    config_raw = json.dumps({"current": 1, "limit": 100}, separators=(",", ":"))
    one_raw = "[]"

    created = 0
    skipped = 0

    problems_dir.mkdir(exist_ok=True)

    for item in items:
        slug = item.get("titleSlug")
        if not slug:
            continue

        slug_dir = problems_dir / slug
        if slug_dir.is_dir():
            skipped += 1
            continue

        slug_dir.mkdir(parents=True, exist_ok=True)
        (slug_dir / "config.txt").write_text(encode(config_raw), encoding="utf-8")
        (slug_dir / "1.txt").write_text(encode(one_raw), encoding="utf-8")
        created += 1

    print(f"Created: {created}")
    print(f"Skipped (already exists): {skipped}")
    print(f"Total items: {len(items)}")


if __name__ == "__main__":
    main()
