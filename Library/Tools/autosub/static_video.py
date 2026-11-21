#!/usr/bin/env python3
"""Create a static-background video from audio and optional subtitles."""

import argparse
import os
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


FONT_CANDIDATES = {
    "title": "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "body": "/System/Library/Fonts/Supplemental/Arial.ttf",
    "cjk": "/System/Library/Fonts/PingFang.ttc",
}


def load_font(kind: str, size: int):
    path = FONT_CANDIDATES[kind]
    if not os.path.exists(path):
        path = shutil.which("Arial") or "/System/Library/Fonts/Supplemental/Arial.ttf"
    return ImageFont.truetype(path, size)


def wrap_text(draw, text, font, max_width):
    lines = []
    current = ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def make_static_image(
    output_path: Path,
    title: str,
    subtitle: str,
    date: str,
    banner_path: Path | None,
):
    width, height = 1920, 1080
    image = Image.new("RGB", (width, height), "#f7f5f2")
    draw = ImageDraw.Draw(image)

    title_font = load_font("title", 74)
    subtitle_font = load_font("cjk", 42)
    meta_font = load_font("body", 30)

    max_width = 1480
    title_lines = wrap_text(draw, title, title_font, max_width)
    subtitle_lines = wrap_text(draw, subtitle, subtitle_font, max_width)

    y = 190
    for line in title_lines:
        draw.text((220, y), line, font=title_font, fill="#111111")
        y += 92

    y += 40
    for line in subtitle_lines:
        draw.text((220, y), line, font=subtitle_font, fill="#333333")
        y += 60

    draw.text((220, y + 48), date, font=meta_font, fill="#555555")
    draw.text((220, y + 92), "Stratechery Interview", font=meta_font, fill="#777777")

    if banner_path and banner_path.exists():
        banner = Image.open(banner_path).convert("RGBA")
        banner.thumbnail((900, 180), Image.Resampling.LANCZOS)
        image.paste(banner, (220, 860), banner)

    image.save(output_path, quality=95)


def run(cmd):
    subprocess.run(cmd, check=True)


def make_static_video(audio: Path, image: Path, output: Path):
    cmd = [
        "ffmpeg",
        "-y",
        "-loop",
        "1",
        "-framerate",
        "2",
        "-i",
        str(image),
        "-i",
        str(audio),
        "-c:v",
        "libx264",
        "-tune",
        "stillimage",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-shortest",
        "-movflags",
        "+faststart",
        str(output),
    ]
    run(cmd)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--title", required=True)
    parser.add_argument("--subtitle", default="")
    parser.add_argument("--date", default="")
    parser.add_argument("--banner", type=Path)
    parser.add_argument("--image", type=Path)
    parser.add_argument("--srt", type=Path)
    parser.add_argument("--ass", type=Path)
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    image_path = args.image or args.output.with_suffix(".static.png")
    make_static_image(
        image_path,
        args.title,
        args.subtitle,
        args.date,
        args.banner,
    )

    base_video = args.output.with_suffix(".base.mp4")
    make_static_video(args.audio, image_path, base_video)

    if args.srt:
        ass_path = args.ass or args.output.with_suffix(".ass")
        tools_dir = Path(__file__).resolve().parents[1]
        run([
            "python3",
            str(tools_dir / "hardsubber" / "srt_to_ass.py"),
            str(args.srt),
            str(ass_path),
            "--layout",
            "bilingual",
            "--overwrite",
        ])
        run([
            "python3",
            str(tools_dir / "hardsubber" / "burn_engine.py"),
            str(base_video),
            str(ass_path),
            str(args.output),
            "--headless",
        ])
        base_video.unlink(missing_ok=True)
    else:
        base_video.replace(args.output)


if __name__ == "__main__":
    main()
