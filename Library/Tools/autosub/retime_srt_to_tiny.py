#!/usr/bin/env python3
"""Retime an existing SRT using a tiny Whisper timestamp track."""

import argparse
import json
from pathlib import Path

import sys

sys.path.append(str(Path(__file__).resolve().parents[1] / "common"))
import srt_utils

from align_article_to_tiny import build_tiny_words, format_srt_time, normalize_word


def build_word_times(article_words, tiny_words):
    from difflib import SequenceMatcher

    article_norms = [normalize_word(word) for word in article_words]
    tiny_norms = [word["norm"] for word in tiny_words]
    matcher = SequenceMatcher(None, article_norms, tiny_norms, autojunk=False)
    matches = matcher.get_matching_blocks()

    mapping = {}
    for match in matches:
        for offset in range(match.size):
            mapping[match.a + offset] = match.b + offset

    matched_indices = sorted(mapping)
    word_times = []
    previous_matched = None
    next_matched = None
    for index in range(len(article_words)):
        if index in mapping:
            previous_matched = index
            word_times.append((tiny_words[mapping[index]]["start"], tiny_words[mapping[index]]["end"]))
            continue

        if next_matched is None or next_matched <= index:
            candidates = [candidate for candidate in matched_indices if candidate > index]
            next_matched = candidates[0] if candidates else None

        if previous_matched is not None and next_matched is not None:
            previous_time = tiny_words[mapping[previous_matched]]["start"]
            next_time = tiny_words[mapping[next_matched]]["start"]
            ratio = (index - previous_matched) / (next_matched - previous_matched)
            time = previous_time + (next_time - previous_time) * ratio
            word_times.append((time, time))
        elif previous_matched is not None:
            word_times.append((tiny_words[mapping[previous_matched]]["end"], tiny_words[mapping[previous_matched]]["end"]))
        elif next_matched is not None:
            word_times.append((tiny_words[mapping[next_matched]]["start"], tiny_words[mapping[next_matched]]["start"]))
        else:
            word_times.append((0.0, 0.0))

    return word_times


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--srt", required=True, type=Path)
    parser.add_argument("--transcript", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    blocks = srt_utils.parse_srt(str(args.srt))
    tiny_words = build_tiny_words(args.transcript)
    article_words = []
    block_ranges = []
    for block in blocks:
        words = " ".join(block["lines"]).split()
        start_index = len(article_words)
        article_words.extend(words)
        block_ranges.append((start_index, start_index + len(words) - 1))

    word_times = build_word_times(article_words, tiny_words)
    retimed = []
    for block, (start_index, end_index) in zip(blocks, block_ranges):
        if start_index >= len(word_times) or end_index >= len(word_times):
            continue
        start = word_times[start_index][0]
        end = word_times[end_index][1]
        if end <= start:
            end = start + 0.8
        if end - start < 0.8:
            end = start + 0.8
        retimed.append(
            {
                "time": f"{format_srt_time(start)} --> {format_srt_time(end)}",
                "start": start,
                "end": end,
                "lines": block["lines"],
            }
        )

    for index in range(1, len(retimed)):
        previous = retimed[index - 1]
        current = retimed[index]
        if current["start"] < previous["end"]:
            current["start"] = previous["end"]
        if current["end"] <= current["start"]:
            current["end"] = current["start"] + 0.8
        current["time"] = f"{format_srt_time(current['start'])} --> {format_srt_time(current['end'])}"

    srt_utils.write_srt(retimed, str(args.output))
    print(f"Retimed {len(retimed)} blocks to {args.output}")


if __name__ == "__main__":
    main()
