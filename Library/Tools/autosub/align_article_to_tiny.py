#!/usr/bin/env python3
"""Align article transcript text to a tiny Whisper timestamp track."""

import argparse
import json
import re
from pathlib import Path

import sys

sys.path.append(str(Path(__file__).resolve().parents[1] / "common"))
import srt_utils

from article_to_srt import extract_sections, get_chapters, get_duration, normalize_title, split_blocks, split_sentences


def normalize_word(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", word.lower())


def build_tiny_words(transcript_path: Path):
    segments = json.loads(transcript_path.read_text(encoding="utf-8"))
    words = []
    for segment in segments:
        text = segment["text"].strip()
        if not text:
            continue
        raw_words = text.split()
        if not raw_words:
            continue
        start = float(segment["start"])
        end = float(segment["end"])
        duration = max(end - start, 0.01)
        for index, raw_word in enumerate(raw_words):
            word_start = start + duration * index / len(raw_words)
            word_end = start + duration * (index + 1) / len(raw_words)
            words.append(
                {
                    "raw": raw_word,
                    "norm": normalize_word(raw_word),
                    "start": word_start,
                    "end": word_end,
                }
            )
    return words


def build_tiny_segments(transcript_path: Path):
    return json.loads(transcript_path.read_text(encoding="utf-8"))


def normalize_text(text: str) -> str:
    return " ".join(normalize_word(word) for word in text.split() if normalize_word(word))


def merge_intro_sections(sections):
    if len(sections) >= 2:
        first_title, first_text = sections[0]
        second_title, second_text = sections[1]
        merged_text = " ".join(part.strip() for part in [first_text, second_text] if part.strip())
        return [("Intro", merged_text)] + sections[2:]
    return sections


def align_section(article_text: str, tiny_words, chapter):
    article_words = article_text.split()
    article_norms = [normalize_word(word) for word in article_words]
    tiny_norms = [word["norm"] for word in tiny_words]

    from difflib import SequenceMatcher

    matcher = SequenceMatcher(None, article_norms, tiny_norms, autojunk=False)
    matches = matcher.get_matching_blocks()

    mapping = {}
    for match in matches:
        for offset in range(match.size):
            article_index = match.a + offset
            tiny_index = match.b + offset
            mapping[article_index] = tiny_index

    # Interpolate unmatched article words between matched neighbors.
    matched_indices = sorted(mapping)
    if not matched_indices:
        return []

    word_times = []
    previous_matched = None
    next_matched = None
    for index in range(len(article_words)):
        if index in mapping:
            previous_matched = index
            word_times.append((tiny_words[mapping[index]]["start"], tiny_words[mapping[index]]["end"]))
            continue

        if next_matched is None or next_matched <= index:
            next_candidates = [candidate for candidate in matched_indices if candidate > index]
            next_matched = next_candidates[0] if next_candidates else None

        if previous_matched is not None and next_matched is not None:
            prev_time = tiny_words[mapping[previous_matched]]["start"]
            next_time = tiny_words[mapping[next_matched]]["start"]
            ratio = (index - previous_matched) / (next_matched - previous_matched)
            time = prev_time + (next_time - prev_time) * ratio
            word_times.append((time, time))
        elif previous_matched is not None:
            word_times.append((tiny_words[mapping[previous_matched]]["end"], tiny_words[mapping[previous_matched]]["end"]))
        elif next_matched is not None:
            word_times.append((tiny_words[mapping[next_matched]]["start"], tiny_words[mapping[next_matched]]["start"]))
        else:
            word_times.append((float(chapter["start_time"]), float(chapter["start_time"])))

    return word_times


def align_blocks_to_segments(blocks, tiny_segments):
    from difflib import SequenceMatcher

    aligned = []
    cursor = 0
    for block in blocks:
        block_norm = normalize_text(block)
        best_ratio = -1.0
        best_window = None
        best_end = cursor
        for start in range(cursor, min(len(tiny_segments), cursor + 8)):
            for size in range(1, 4):
                end = start + size
                if end > len(tiny_segments):
                    break
                window_text = " ".join(segment["text"] for segment in tiny_segments[start:end])
                window_norm = normalize_text(window_text)
                ratio = SequenceMatcher(None, block_norm, window_norm, autojunk=False).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_window = (start, end)
                    best_end = end
        if best_window is None:
            if cursor < len(tiny_segments):
                segment = tiny_segments[cursor]
                aligned.append((float(segment["start"]), float(segment["end"])))
                cursor += 1
            else:
                aligned.append((None, None))
            continue

        start, end = best_window
        aligned.append((float(tiny_segments[start]["start"]), float(tiny_segments[end - 1]["end"])))
        cursor = min(len(tiny_segments), start + 1)
    return aligned


def format_srt_time(seconds: float) -> str:
    milliseconds = int(round(seconds * 1000))
    hours = milliseconds // 3600000
    milliseconds %= 3600000
    minutes = milliseconds // 60000
    milliseconds %= 60000
    secs = milliseconds // 1000
    milliseconds %= 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--article", required=True, type=Path)
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--transcript", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-words", type=int, default=14)
    args = parser.parse_args()

    sections = merge_intro_sections(extract_sections(args.article))
    chapters = get_chapters(args.audio)
    tiny_words = build_tiny_words(args.transcript)
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
        chapter_start = float(chapter["start_time"])
        chapter_end = float(chapter["end_time"])
        section_tiny_words = [word for word in tiny_words if word["start"] >= chapter_start and word["end"] <= chapter_end]
        word_times = align_section(text, section_tiny_words, chapter)
        blocks = split_blocks(split_sentences(text), max_words=args.max_words)

        word_offset = 0
        for block in blocks:
            words = block.split()
            if not words:
                continue
            start_index = word_offset
            end_index = word_offset + len(words) - 1
            word_offset += len(words)
            if start_index >= len(word_times) or end_index >= len(word_times):
                continue
            start = word_times[start_index][0]
            end = word_times[end_index][1]
            if end <= start:
                end = start + 0.8
            if end - start < 0.8:
                end = start + 0.8
            all_blocks.append(
                {
                    "time": f"{format_srt_time(start)} --> {format_srt_time(end)}",
                    "start": start,
                    "end": end,
                    "lines": [block],
                }
            )

    # Keep subtitle timing monotonic.
    audio_duration = get_duration(args.audio)
    if all_blocks:
        last_end = all_blocks[-1]["end"]
        if audio_duration - last_end > 5:
            tail_segments = [
                segment
                for segment in build_tiny_segments(args.transcript)
                if float(segment["start"]) >= last_end and float(segment["end"]) <= audio_duration
            ]
            for segment in tail_segments:
                text = segment["text"].strip()
                if not text:
                    continue
                start = float(segment["start"])
                end = float(segment["end"])
                if end <= start:
                    end = start + 0.8
                all_blocks.append(
                    {
                        "time": f"{format_srt_time(start)} --> {format_srt_time(end)}",
                        "start": start,
                        "end": end,
                        "lines": [text],
                    }
                )

    for index in range(1, len(all_blocks)):
        previous = all_blocks[index - 1]
        current = all_blocks[index]
        if current["start"] < previous["end"]:
            current["start"] = previous["end"]
        if current["end"] <= current["start"]:
            current["end"] = current["start"] + 0.8
        current["time"] = f"{format_srt_time(current['start'])} --> {format_srt_time(current['end'])}"

    srt_utils.write_srt(all_blocks, str(args.output))
    print(f"Aligned {len(all_blocks)} blocks to {args.output}")


if __name__ == "__main__":
    main()
