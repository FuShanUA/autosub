#!/usr/bin/env python3
"""Build an SRT from an article transcript and audio duration."""

import argparse
import json
import re
import subprocess
from pathlib import Path

from bs4 import BeautifulSoup


def extract_article_text(html_path: Path) -> str:
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()
    node = soup.select_one(".entry-content") or soup.select_one("main") or soup
    return node.get_text(" ", strip=True)


def normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())


def extract_sections(html_path: Path):
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()
    node = soup.select_one(".entry-content") or soup.select_one("main") or soup
    sections = []
    current_title = "Intro"
    current_parts = []
    for child in node.children:
        if getattr(child, "name", None) in {"h2", "h3", "h4"}:
            title = child.get_text(" ", strip=True)
            if title.lower() == "share":
                break
            if current_parts:
                sections.append((current_title, " ".join(current_parts)))
            current_title = title
            current_parts = []
        else:
            text = child.get_text(" ", strip=True) if hasattr(child, "get_text") else str(child).strip()
            if text:
                current_parts.append(text)
    if current_parts:
        sections.append((current_title, " ".join(current_parts)))
    return sections


def get_chapters(audio_path: Path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_chapters",
            "-of",
            "json",
            str(audio_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    return payload.get("chapters", [])


def split_sentences(text: str):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def split_blocks(sentences, max_words=14):
    blocks = []
    current = []
    current_words = 0
    for sentence in sentences:
        words = sentence.split()
        if current and current_words + len(words) > max_words:
            blocks.append(" ".join(current))
            current = []
            current_words = 0
        if len(words) > max_words:
            for i in range(0, len(words), max_words):
                part = words[i : i + max_words]
                blocks.append(" ".join(part))
        else:
            current.extend(words)
            current_words += len(words)
    if current:
        blocks.append(" ".join(current))
    return blocks


def get_duration(audio_path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


def format_srt_time(seconds: float) -> str:
    milliseconds = int(round(seconds * 1000))
    hours = milliseconds // 3600000
    milliseconds %= 3600000
    minutes = milliseconds // 60000
    milliseconds %= 60000
    secs = milliseconds // 1000
    milliseconds %= 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def write_srt(blocks, duration, output_path: Path):
    total_words = sum(len(block.split()) for block in blocks)
    elapsed = 0.0
    with output_path.open("w", encoding="utf-8") as f:
        for index, block in enumerate(blocks, 1):
            words = len(block.split())
            start = elapsed
            elapsed += duration * words / total_words
            end = min(elapsed, duration)
            f.write(f"{index}\n")
            f.write(f"{format_srt_time(start)} --> {format_srt_time(end)}\n")
            f.write(f"{block}\n\n")


def write_section_srt(sections, chapters, output_path: Path):
    chapter_map = {normalize_title(chapter["tags"]["title"]): chapter for chapter in chapters}
    all_blocks = []
    for title, text in sections:
        text = re.sub(r"^Listen to this post:\s*", "", text)
        text = re.split(r"This Daily Update Interview is also available as a podcast\.", text)[0]
        text = re.split(r"This Daily Update is intended for a single recipient\.", text)[0]
        text = text.strip()
        if not text:
            continue
        chapter = chapter_map.get(normalize_title(title))
        if not chapter:
            continue
        start = float(chapter["start_time"])
        end = float(chapter["end_time"])
        sentences = split_sentences(text)
        blocks = split_blocks(sentences)
        total_words = sum(len(block.split()) for block in blocks)
        elapsed = start
        for block in blocks:
            words = len(block.split())
            block_start = elapsed
            elapsed += (end - start) * words / total_words
            block_end = min(elapsed, end)
            all_blocks.append((block_start, block_end, block))
    with output_path.open("w", encoding="utf-8") as f:
        for index, (start, end, block) in enumerate(all_blocks, 1):
            f.write(f"{index}\n")
            f.write(f"{format_srt_time(start)} --> {format_srt_time(end)}\n")
            f.write(f"{block}\n\n")
    print(f"Wrote {len(all_blocks)} subtitle blocks to {output_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--article", required=True, type=Path)
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-words", type=int, default=14)
    args = parser.parse_args()

    sections = extract_sections(args.article)
    chapters = get_chapters(args.audio)
    if chapters:
        write_section_srt(sections, chapters, args.output)
    else:
        text = extract_article_text(args.article)
        sentences = split_sentences(text)
        blocks = split_blocks(sentences, args.max_words)
        duration = get_duration(args.audio)
        write_srt(blocks, duration, args.output)
        print(f"Wrote {len(blocks)} subtitle blocks to {args.output}")


if __name__ == "__main__":
    main()
