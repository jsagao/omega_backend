# app/rss_finance_home.py
from fastapi import APIRouter, HTTPException
import asyncio
import httpx
import feedparser
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

router = APIRouter()

# --- Feeds: Manila Times, Bloomberg (Markets), AP (Financial) ---
FEEDS = [
    {"name": "Manila Times", "url": "https://www.manilatimes.net/news/national/feed/"},
    {"name": "Manila Times", "url": "https://www.manilatimes.net/business/feed/"},
    {"name": "Bloomberg",    "url": "https://feeds.bloomberg.com/markets/news.rss"},
    {"name": "AP",           "url": "https://apnews.com/hub/apf-financial-news?utm_source=rss"},
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; RSSFetcher/1.0; +https://example.com)"
}

def human_age(dt: Optional[datetime]) -> str:
    try:
        if not dt:
            return ""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        diff = datetime.now(timezone.utc) - dt
        mins = int(diff.total_seconds() // 60)
        if mins < 1:
            return "Just now"
        if mins < 60:
            return f"{mins} minute{'s' if mins != 1 else ''} ago"
        hours = mins // 60
        if hours < 24:
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        days = hours // 24
        return f"{days} day{'s' if days != 1 else ''} ago"
    except Exception:
        return ""

def first_img_from_html(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    img = soup.find("img")
    return img["src"] if img and img.has_attr("src") else ""

def extract_image(entry) -> str:
    media = entry.get("media_content") or []
    if isinstance(media, list) and media:
        url = media[0].get("url")
        if url:
            return url

    thumb = entry.get("media_thumbnail") or []
    if isinstance(thumb, list) and thumb:
        url = thumb[0].get("url")
        if url:
            return url

    encl = entry.get("enclosures") or []
    if isinstance(encl, list) and encl:
        url = encl[0].get("href")
        if url:
            return url

    html_src = entry.get("summary") or ""
    if not html_src:
        contents = entry.get("content") or []
        if contents and isinstance(contents, list):
            html_src = contents[0].get("value", "")
    return first_img_from_html(html_src)

def parse_dt(entry) -> Optional[datetime]:
    try:
        if entry.get("published_parsed"):
            return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        if entry.get("updated_parsed"):
            return datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
    except Exception:
        pass
    return None

def norm_item(entry, source_name: str) -> Dict[str, Any]:
    title = (entry.get("title") or "Untitled").strip()
    link = entry.get("link") or entry.get("id") or "#"
    image = extract_image(entry)
    dt = parse_dt(entry)
    age = human_age(dt)

    return {
        "id": link,
        "url": link,
        "image": image,
        "title": title,
        "source": source_name,
        "age": age,
        "type": "story",
        "kicker": "",
        "tags": [],
        "_dt": dt or datetime(1970, 1, 1, tzinfo=timezone.utc),
    }

async def fetch_feed(url: str, name: str) -> List[Dict[str, Any]]:
    async with httpx.AsyncClient(follow_redirects=True, headers=HEADERS, timeout=20.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        parsed = feedparser.parse(r.content)
        entries = parsed.entries or []
        return [norm_item(e, name) for e in entries]

@router.get("/rss/finance-home")
async def finance_home():
    try:
        results = await asyncio.gather(
            *[fetch_feed(f["url"], f["name"]) for f in FEEDS],
            return_exceptions=True,
        )

        items: List[Dict[str, Any]] = []
        for res in results:
            if isinstance(res, Exception):
                continue
            items.extend(res)

        if not items:
            return {"hero": None, "topRight": [], "subCards": [], "latest": []}

        dedup_map: Dict[str, Dict[str, Any]] = {}
        for it in items:
            key = it["id"]
            prev = dedup_map.get(key)
            if not prev or it["_dt"] > prev["_dt"]:
                dedup_map[key] = it

        dedup = list(dedup_map.values())
        dedup.sort(key=lambda x: x["_dt"], reverse=True)

        pool = dedup[:60]

        hero = {**pool[0], "type": "hero", "kicker": pool[0]["source"]}
        hero.pop("_dt", None)

        topRight = []
        for it in pool[1:4]:
            it = dict(it)
            it.pop("_dt", None)
            topRight.append(it)

        subCards = []
        for it in pool[4:6]:
            it = dict(it)
            it.pop("_dt", None)
            subCards.append(it)

        latest = []
        for it in pool[6:18]:
            latest.append({
                "id": it["id"],
                "image": "",
                "title": it["title"],
                "source": it["source"],
                "age": it["age"],
                "url": it["url"],
            })

        return {"hero": hero, "topRight": topRight, "subCards": subCards, "latest": latest}

    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"RSS upstream error: {e}") from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"RSS parse error: {e}") from e