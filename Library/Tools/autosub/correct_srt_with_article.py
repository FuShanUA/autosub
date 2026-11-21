#!/usr/bin/env python3
"""Correct ASR subtitles by aligning them to an article transcript."""

import argparse
import difflib
import re
from pathlib import Path

import sys

sys.path.append(str(Path(__file__).resolve().parents[1] / "common"))
import srt_utils

from bs4 import BeautifulSoup


SPEAKER_RE = re.compile(r"^(?:SS|TM|BT):\s*", re.IGNORECASE)


def extract_article_text(html_path: Path) -> str:
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()
    node = soup.select_one(".entry-content") or soup.select_one("main") or soup
    return node.get_text(" ", strip=True)


def split_into_chunks(text: str, max_words: int = 20):
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks = []
    for sentence in sentences:
        words = sentence.split()
        if not words:
            continue
        start = 0
        while start < len(words):
            part = words[start : start + max_words]
            chunks.append(" ".join(part))
            start += max_words
    return chunks


def normalize(text: str) -> str:
    text = SPEAKER_RE.sub("", text)
    text = re.sub(r"[^a-z0-9\s]", "", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def correct_srt(srt_path: Path, article_html: Path, output_path: Path, threshold: float):
    blocks = srt_utils.parse_srt(str(srt_path))
    article_text = extract_article_text(article_html)
    article_chunks = split_into_chunks(article_text)
    normalized_chunks = [normalize(chunk) for chunk in article_chunks]
    chunk_map = dict(zip(normalized_chunks, article_chunks))

    corrected = 0
    for block in blocks:
        original = " ".join(block["lines"])
        norm = normalize(original)
        if not norm:
            continue
        matches = difflib.get_close_matches(norm, normalized_chunks, n=1, cutoff=threshold)
        if matches:
            best = matches[0]
            replacement = chunk_map[best]
            replacement = SPEAKER_RE.sub("", replacement).strip()
            if replacement and replacement != original:
                block["lines"] = [replacement]
                corrected += 1

    srt_utils.write_srt(blocks, str(output_path))
    print(f"Corrected {corrected}/{len(blocks)} blocks using article text.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--srt", required=True, type=Path)
    parser.add_argument("--article", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--threshold", type=float, default=0.58)
    args = parser.parse_args()
    correct_srt(args.srt, args.article, args.output, args.threshold)


if __name__ == "__main__":
    main()
