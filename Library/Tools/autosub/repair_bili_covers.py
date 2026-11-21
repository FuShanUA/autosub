#!/usr/bin/env python3
"""Regenerate and optionally republish Bilibili covers for local projects."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Optional

import requests

REPO_ROOT = Path(__file__).resolve().parents[3]
COMMON_DIR = REPO_ROOT / "Library" / "Tools" / "common"
if COMMON_DIR.is_dir() and str(COMMON_DIR) not in sys.path:
    sys.path.insert(0, str(COMMON_DIR))

from cover_utils import generate_platform_covers  # noqa: E402

DEFAULT_ROOT = Path("/Users/shanfu/cc/Projects/YY")
DEFAULT_COOKIES = Path(
    "/Users/shanfu/cc/.agents/skills/bilibili-uploader/assets/cookies.json"
)
MEMBER = "https://member.bilibili.com"
UPLOAD_REFERER = f"{MEMBER}/platform/upload/video/frame"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


def _normalize_title(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip())


def _find_archive(
    metadata: dict[str, Any],
    archives: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    title = _normalize_title(metadata.get("title", ""))
    description = _normalize_title(metadata.get("description", ""))
    for archive in archives:
        if _normalize_title(archive.get("title", "")) == title:
            return archive
    for archive in archives:
        if description and _normalize_title(archive.get("desc", "")) == description:
            return archive
    for archive in archives:
        online_title = _normalize_title(archive.get("title", ""))
        if title and online_title and (title in online_title or online_title in title):
            return archive
    if title:
        matches = [
            (
                SequenceMatcher(None, title, _normalize_title(archive.get("title", ""))).ratio(),
                archive,
            )
            for archive in archives
        ]
        ratio, archive = max(matches, key=lambda item: item[0])
        if ratio >= 0.58:
            return archive
    return None


def _find_projects(root: Path) -> list[Path]:
    return sorted(
        path.parent
        for path in root.rglob("upload_metadata.json")
        if "clips" not in path.parts and ".clipcut" not in path.parts
    )


def _find_video(project: Path) -> Optional[Path]:
    candidates = [
        path
        for path in project.glob("*.mp4")
        if ".clipcut" not in path.parts and "clips" not in path.parts
    ]
    hardsubs = [path for path in candidates if path.stem.endswith("_hardsub")]
    pool = hardsubs or candidates
    return max(pool, key=lambda path: path.stat().st_size) if pool else None


def _find_clean_source(project: Path, video: Optional[Path]) -> Optional[Path]:
    if video:
        base = video.with_suffix("")
        original_base = base.with_name(base.stem[:-8]) if base.stem.endswith("_hardsub") else base
        for path in (base.with_suffix(".jpg"), original_base.with_suffix(".jpg"), original_base.with_suffix(".poster.jpg")):
            if path.is_file():
                return path
    images = [
        path
        for path in project.glob("*.jpg")
        if ".cover." not in path.name and "clips" not in path.parts
    ]
    return max(images, key=lambda path: path.stat().st_size) if images else None


def _load_metadata(project: Path) -> dict[str, Any]:
    with project.joinpath("upload_metadata.json").open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _session(cookies_path: Path) -> requests.Session:
    with cookies_path.open("r", encoding="utf-8") as handle:
        cookies = json.load(handle)
    session = requests.Session()
    session.cookies.update(
        {
            key: cookies[key]
            for key in ("SESSDATA", "bili_jct", "buvid3", "buvid4", "DedeUserID")
            if cookies.get(key)
        }
    )
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Referer": UPLOAD_REFERER,
            "Origin": MEMBER,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
    )
    return session


def _list_archives(session: requests.Session) -> list[dict[str, Any]]:
    archives: list[dict[str, Any]] = []
    page = 1
    while True:
        response = None
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                response = session.get(
                    f"{MEMBER}/x2/creative/web/archives/sp",
                    params={"pn": page, "ps": 50},
                    timeout=(10, 60),
                )
                break
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(2 * (attempt + 1))
        if response is None:
            raise RuntimeError(f"Bilibili archive list request failed: {last_error}")
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"Bilibili archive list failed: {payload.get('message')}")
        data = payload.get("data") or {}
        entries = data.get("arc_audits") or []
        archives.extend(entry.get("Archive") or {} for entry in entries)
        pagination = data.get("page") or {}
        count = int(pagination.get("count") or 0)
        if not entries or len(archives) >= count:
            break
        page += 1
        time.sleep(0.2)
    return archives


def _archive_view(session: requests.Session, aid: int) -> dict[str, Any]:
    response = session.get(
        f"{MEMBER}/x/vupre/web/archive/view",
        params={"topic_grey": 1, "aid": aid},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        raise RuntimeError(f"Bilibili archive view failed: {payload.get('message')}")
    return payload.get("data") or {}


def _upload_cover(session: requests.Session, path: Path, csrf: str) -> str:
    data = path.read_bytes()
    encoded = base64.b64encode(data).decode("ascii")
    response = session.post(
        f"{MEMBER}/x/vu/web/cover/up",
        data={"cover": f"data:image/jpeg;base64,{encoded}", "csrf": csrf},
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        raise RuntimeError(f"Bilibili cover upload failed: {payload.get('message')}")
    url = payload.get("data", {}).get("url")
    if not url:
        raise RuntimeError("Bilibili cover upload returned no URL")
    return url


def _edit_body(view: dict[str, Any], cover: str, cover43: str, csrf: str) -> dict[str, Any]:
    archive = view["archive"]
    videos = view.get("videos") or []
    return {
        "aid": archive["aid"],
        "copyright": archive.get("copyright", 1),
        "source": archive.get("source", ""),
        "cover": cover,
        "cover43": cover43,
        "title": archive["title"],
        "tid": archive["tid"],
        "tag": archive.get("tag", ""),
        "desc": archive.get("desc", ""),
        "desc_format_id": archive.get("desc_format_id", 0),
        "dynamic": archive.get("dynamic", ""),
        "recreate": -1,
        "interactive": archive.get("interactive", 0),
        "act_reserve_create": 0,
        "no_reprint": archive.get("no_reprint", 1),
        "subtitle": {"open": 0, "lan": ""},
        "dtime": archive.get("dtime", 0),
        "videos": [
            {
                "filename": video["filename"],
                "title": video.get("title") or archive["title"],
                "desc": video.get("desc", ""),
                "cid": video.get("cid"),
            }
            for video in videos
        ],
        "is_only_self": archive.get("is_only_self", 0),
        "web_os": 1,
        "csrf": csrf,
    }


def _submit_edit(
    session: requests.Session,
    view: dict[str, Any],
    cover: str,
    cover43: str,
    csrf: str,
) -> None:
    response = session.post(
        f"{MEMBER}/x/vu/web/edit",
        params={"t": int(time.time() * 1000), "csrf": csrf},
        json=_edit_body(view, cover, cover43, csrf),
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        raise RuntimeError(f"Bilibili edit failed: {payload.get('message')}")


def repair(
    roots: Iterable[Path],
    report_path: Path,
    apply_online: bool,
    cookies_path: Path,
    dry_run: bool,
    use_existing: bool,
    fixed_aid: Optional[int] = None,
    fixed_bvid: str = "",
) -> int:
    projects = [project for root in roots for project in _find_projects(root)]
    session = _session(cookies_path) if apply_online else None
    archives: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    failures = 0

    for project in projects:
        result: dict[str, Any] = {"project": str(project)}
        results.append(result)
        try:
            metadata = _load_metadata(project)
            title = metadata.get("cover_title") or metadata.get("title")
            if not title:
                raise RuntimeError("upload_metadata.json has no cover_title/title")
            video = _find_video(project)
            source = _find_clean_source(project, video)
            if not video and not source:
                raise RuntimeError("no local hardsub video or clean source frame")
            result["title"] = title
            result["video"] = str(video) if video else None
            result["source"] = str(source) if source else None

            if dry_run:
                result["status"] = "dry-run"
                continue

            wide = Path(str(video or source)).with_suffix(".cover.bili.jpg")
            four_by_three = Path(str(video or source)).with_suffix(".cover.bili.4x3.jpg")
            if use_existing:
                if not wide.is_file() or not four_by_three.is_file():
                    raise RuntimeError(f"existing Bilibili covers missing: {wide}, {four_by_three}")
            else:
                wide, four_by_three = generate_platform_covers(
                    str(video or source),
                    "bilibili",
                    title,
                    source_path=str(source) if source else None,
                    overwrite=True,
                )
                wide, four_by_three = Path(wide), Path(four_by_three)
            result["wide_cover"] = str(wide)
            result["four_by_three_cover"] = str(four_by_three)

            if apply_online:
                if session is None:
                    raise RuntimeError("internal session missing")
                if metadata.get("aid"):
                    archive = {
                        "aid": int(metadata["aid"]),
                        "bvid": metadata.get("bvid", ""),
                    }
                elif fixed_aid:
                    archive = {"aid": fixed_aid, "bvid": fixed_bvid}
                else:
                    if not archives:
                        archives = _list_archives(session)
                    archive = _find_archive(metadata, archives)
                if not archive:
                    raise RuntimeError("Bilibili archive not found by form title")
                aid = int(archive["aid"])
                view = _archive_view(session, aid)
                csrf = session.cookies.get("bili_jct")
                if not csrf:
                    raise RuntimeError("cookies.json has no bili_jct")
                cover_url = _upload_cover(session, Path(wide), csrf)
                cover43_url = _upload_cover(session, Path(four_by_three), csrf)
                _submit_edit(session, view, cover_url, cover43_url, csrf)
                result["bvid"] = archive.get("bvid")
                result["online_cover"] = cover_url
                result["online_cover43"] = cover43_url
                result["status"] = "updated"
                time.sleep(0.5)
            else:
                result["status"] = "generated"
        except Exception as exc:
            failures += 1
            result["status"] = "failed"
            result["error"] = str(exc)
            print(f"❌ {project}: {exc}", flush=True)

    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8") as handle:
        json.dump(results, handle, ensure_ascii=False, indent=2)
    print(f"Report: {report_path}")
    print(f"Projects: {len(results)}, failures: {failures}")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "roots",
        nargs="*",
        type=Path,
        default=[DEFAULT_ROOT],
        help="Project parent folders containing upload_metadata.json",
    )
    parser.add_argument("--report", type=Path, default=Path("cover_repair_report.json"))
    parser.add_argument("--cookies", type=Path, default=DEFAULT_COOKIES)
    parser.add_argument("--apply-online", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--use-existing", action="store_true")
    parser.add_argument("--aid", type=int)
    parser.add_argument("--bvid", default="")
    args = parser.parse_args()
    roots = args.roots or [DEFAULT_ROOT]
    return repair(
        roots,
        args.report,
        args.apply_online,
        args.cookies,
        args.dry_run,
        args.use_existing,
        args.aid,
        args.bvid,
    )


if __name__ == "__main__":
    raise SystemExit(main())
