#!/usr/bin/env python3
"""Reverse image search across trace.moe (keyless) and SauceNAO (optional key).

Standard library only, so the skill has nothing to install.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

TRACE_SEARCH = "https://api.trace.moe/search"
TRACE_ME = "https://api.trace.moe/me"
SAUCE_SEARCH = "https://saucenao.com/search.php"
UA = "picsearch-skill/1.0 (+claude-skill)"
TIMEOUT = 90

# trace.moe's own guidance: below roughly this, the match is usually noise.
TRACE_CONFIDENT = 0.87


class SearchError(RuntimeError):
    """Something went wrong that the user needs to hear about verbatim."""


# ---------------------------------------------------------------- http helpers
def _open(req: urllib.request.Request) -> bytes:
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()[:400].decode("utf-8", "replace")
        if exc.code == 429:
            raise SearchError(
                "Rate limited (HTTP 429). trace.moe allows ~100 searches/day per IP "
                "and one at a time. Wait a minute and retry."
            ) from exc
        if exc.code in (402, 403) and "saucenao" in req.full_url:
            raise SearchError(
                "SauceNAO rejected the request (HTTP %d). Either the daily quota is "
                "spent or the API key is wrong. Details: %s" % (exc.code, body)
            ) from exc
        raise SearchError("HTTP %d from %s: %s" % (exc.code, req.full_url, body)) from exc
    except urllib.error.URLError as exc:
        raise SearchError(
            "Could not reach %s (%s). This skill needs outbound internet access."
            % (urllib.parse.urlsplit(req.full_url).netloc, exc.reason)
        ) from exc


def _get_json(url: str) -> dict:
    raw = _open(urllib.request.Request(url, headers={"User-Agent": UA}))
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        head = raw[:200].decode("utf-8", "replace")
        # SauceNAO sits behind Cloudflare and answers challenges in HTML.
        if b"<!DOCTYPE" in raw[:64] or b"<html" in raw[:64]:
            raise SearchError(
                "Expected JSON but got an HTML page — usually a Cloudflare challenge, "
                "which means this network cannot reach the service directly. First bytes: %s"
                % head
            ) from exc
        raise SearchError("Malformed response: %s" % head) from exc


def _multipart(url: str, field: str, path: Path, extra: dict[str, str] | None = None) -> dict:
    """POST one file as multipart/form-data without pulling in requests."""
    boundary = "----picsearch" + uuid.uuid4().hex
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    body = bytearray()
    for key, value in (extra or {}).items():
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
        ).encode()
    body += (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{path.name}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n"
    ).encode()
    body += path.read_bytes()
    body += f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        url,
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": UA},
    )
    raw = _open(req)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SearchError("Malformed response: %s" % raw[:200].decode("utf-8", "replace")) from exc


def _is_url(target: str) -> bool:
    return target.startswith(("http://", "https://"))


def _check_local(target: str) -> Path:
    path = Path(target).expanduser()
    if not path.exists():
        raise SearchError(f"No such image: {path}")
    size_mb = path.stat().st_size / 1e6
    if size_mb > 25:
        raise SearchError(
            f"{path.name} is {size_mb:.1f}MB; trace.moe caps uploads at 25MB. "
            "Downscale it first."
        )
    return path


# ---------------------------------------------------------------- trace.moe
def trace_quota() -> dict:
    return _get_json(TRACE_ME)


def search_trace(target: str, cut_borders: bool = True) -> list[dict]:
    """Anime screenshot -> which show, which episode, which second."""
    params = ["anilistInfo"]
    if cut_borders:
        params.append("cutBorders")
    query = "&".join(params)

    if _is_url(target):
        url = f"{TRACE_SEARCH}?{query}&url={urllib.parse.quote(target, safe='')}"
        data = _get_json(url)
    else:
        data = _multipart(f"{TRACE_SEARCH}?{query}", "image", _check_local(target))

    if data.get("error"):
        raise SearchError(f"trace.moe: {data['error']}")

    out = []
    for item in data.get("result") or []:
        anilist = item.get("anilist") or {}
        titles = anilist.get("title") or {} if isinstance(anilist, dict) else {}
        name = (
            titles.get("romaji")
            or titles.get("english")
            or titles.get("native")
            or item.get("filename")
            or "Unknown"
        )
        out.append(
            {
                "engine": "trace.moe",
                "title": name,
                "english": titles.get("english"),
                "native": titles.get("native"),
                "episode": item.get("episode"),
                "from": item.get("from"),
                "to": item.get("to"),
                "similarity": item.get("similarity") or 0.0,
                "adult": bool(anilist.get("isAdult")) if isinstance(anilist, dict) else False,
                "anilist_id": anilist.get("id") if isinstance(anilist, dict) else None,
                "preview_video": item.get("video"),
                "preview_image": item.get("image"),
            }
        )
    return out


# ---------------------------------------------------------------- SauceNAO
def search_sauce(target: str, api_key: str, numres: int = 8) -> list[dict]:
    """Illustrations and manga: Pixiv, Danbooru, Gelbooru, doujin, and more."""
    qs = urllib.parse.urlencode(
        {"output_type": 2, "api_key": api_key, "db": 999, "numres": numres}
    )
    if _is_url(target):
        data = _get_json(f"{SAUCE_SEARCH}?{qs}&url={urllib.parse.quote(target, safe='')}")
    else:
        data = _multipart(f"{SAUCE_SEARCH}?{qs}", "file", _check_local(target))

    header = data.get("header") or {}
    if header.get("status", 0) < 0:
        raise SearchError("SauceNAO: %s" % header.get("message", "request rejected"))

    out = []
    for item in data.get("results") or []:
        head = item.get("header") or {}
        body = item.get("data") or {}
        urls = body.get("ext_urls") or []
        creator = body.get("member_name") or body.get("creator") or body.get("author_name")
        if isinstance(creator, list):
            creator = ", ".join(str(c) for c in creator)
        out.append(
            {
                "engine": "saucenao",
                "title": body.get("title")
                or body.get("source")
                or body.get("eng_name")
                or body.get("jp_name")
                or "Untitled",
                "artist": creator,
                "index": head.get("index_name"),
                "similarity": float(head.get("similarity", 0)) / 100.0,
                "url": urls[0] if urls else None,
                "all_urls": urls,
                "thumbnail": head.get("thumbnail"),
            }
        )
    return out


# ---------------------------------------------------------------- rendering
def _fmt_time(seconds) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def render(results: list[dict], min_sim: float, quota: dict | None) -> str:
    lines: list[str] = []
    if quota:
        used, total = quota.get("quotaUsed", "?"), quota.get("quota", "?")
        lines.append(f"trace.moe quota: {used}/{total} used today")
        lines.append("")

    kept = [r for r in results if r["similarity"] >= min_sim]
    if not kept:
        best = max((r["similarity"] for r in results), default=0.0)
        lines.append(
            f"No match at or above {min_sim:.0%} similarity "
            f"(best was {best:.1%} of {len(results)} candidates)."
        )
        lines.append("")
        lines.append(
            "trace.moe only indexes anime *video frames*. Cover art, promo images, "
            "manga pages and original illustrations will not match — for those, set "
            "SAUCENAO_API_KEY and rerun with --engine all."
        )
        return "\n".join(lines)

    for i, r in enumerate(kept, 1):
        conf = "strong" if r["similarity"] >= TRACE_CONFIDENT else "weak — treat with suspicion"
        if r["engine"] == "trace.moe":
            lines.append(f"{i}. {r['title']}   [{r['similarity']:.1%} · {conf}]")
            if r.get("english") and r["english"] != r["title"]:
                lines.append(f"   English : {r['english']}")
            if r.get("native"):
                lines.append(f"   Native  : {r['native']}")
            ep = r.get("episode")
            lines.append(
                f"   Episode : {ep if ep not in (None, '') else '—'}"
                f"   at {_fmt_time(r.get('from'))}–{_fmt_time(r.get('to'))}"
            )
            if r.get("adult"):
                lines.append("   Flagged adult by AniList")
            if r.get("anilist_id"):
                lines.append(f"   AniList : https://anilist.co/anime/{r['anilist_id']}")
            if r.get("preview_video"):
                lines.append(f"   Preview : {r['preview_video']}")
        else:
            lines.append(f"{i}. {r['title']}   [{r['similarity']:.1%} · {r.get('index') or ''}]")
            if r.get("artist"):
                lines.append(f"   Artist  : {r['artist']}")
            for u in (r.get("all_urls") or [])[:3]:
                lines.append(f"   Link    : {u}")
        lines.append("")

    weak = [r for r in kept if r["similarity"] < TRACE_CONFIDENT]
    if weak and all(r["engine"] == "trace.moe" for r in weak):
        lines.append(
            f"Note: {len(weak)} of {len(kept)} results are below trace.moe's "
            f"{TRACE_CONFIDENT:.0%} confidence line. Below that, matches are frequently wrong — "
            "say so rather than presenting them as the answer."
        )
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------- cli
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Reverse image search: identify an anime screenshot or artwork."
    )
    ap.add_argument("image", nargs="?", help="local image path or http(s) URL")
    ap.add_argument(
        "--engine",
        choices=["trace", "sauce", "all"],
        default="trace",
        help="trace = anime screenshots (no key). sauce needs SAUCENAO_API_KEY. Default: trace",
    )
    ap.add_argument("--min", type=float, default=0.0, metavar="F",
                    help="hide results below this similarity, 0-1 (default 0)")
    ap.add_argument("--limit", type=int, default=5, help="max results to show (default 5)")
    ap.add_argument("--json", action="store_true", help="emit raw JSON instead of a report")
    ap.add_argument("--quota", action="store_true", help="just report trace.moe quota and exit")
    ap.add_argument("--no-cut-borders", action="store_true",
                    help="keep letterbox bars (trace.moe strips them by default)")
    args = ap.parse_args()

    try:
        if args.quota:
            print(json.dumps(trace_quota(), indent=2))
            return 0
        if not args.image:
            ap.error("an image path or URL is required")

        results: list[dict] = []
        quota = None
        key = os.environ.get("SAUCENAO_API_KEY", "").strip()

        if args.engine in ("trace", "all"):
            results += search_trace(args.image, cut_borders=not args.no_cut_borders)
            try:
                quota = trace_quota()
            except SearchError:
                quota = None

        if args.engine in ("sauce", "all"):
            if not key:
                msg = (
                    "SauceNAO needs an API key. Get a free one at "
                    "https://saucenao.com/user.php and export SAUCENAO_API_KEY."
                )
                if args.engine == "sauce":
                    raise SearchError(msg)
                print(f"[skipping SauceNAO] {msg}", file=sys.stderr)
            else:
                results += search_sauce(args.image, key)

        results.sort(key=lambda r: r["similarity"], reverse=True)
        results = results[: args.limit]

        if args.json:
            print(json.dumps({"quota": quota, "results": results}, indent=2, ensure_ascii=False))
        else:
            print(render(results, args.min, quota))
        return 0

    except SearchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
