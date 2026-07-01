"""
Daily Dump — Tech Edition
Pulls top tech news, skips recently covered stories (unless major update),
writes a 5-minute script with Gemini (free), converts to MP3 via Edge TTS,
then updates the GitHub Pages RSS feed.
"""

import os
import json
import hashlib
import datetime
import feedparser
import requests

# ── CONFIG ────────────────────────────────────────────────────────────────────
TECH_FEEDS = [
    "https://feeds.feedburner.com/TechCrunch",
    "https://www.theverge.com/rss/index.xml",
    "https://www.wired.com/feed/rss",
    "https://feeds.arstechnica.com/arstechnica/index",
    "https://hnrss.org/frontpage",
    "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
]

HEADLINES_COUNT  = 5          # stories per episode
STORY_MEMORY_DAYS = 3         # skip a story if covered within this many days
                               # (unless it's flagged as a major update)
OUTPUT_DIR        = "output"
FEED_DIR          = "docs"
MEMORY_FILE       = "output/story_memory.json"

PODCAST_TITLE       = "Daily Dump Tech"
PODCAST_DESCRIPTION = "Fast daily tech news. No fluff. Five minutes."
PODCAST_AUTHOR      = "Daily Dump Bot"
PODCAST_BASE_URL    = os.environ.get(
    "PODCAST_BASE_URL", "https://example.github.io/daily-dump-tech"
)
# ─────────────────────────────────────────────────────────────────────────────


# ── STORY MEMORY ─────────────────────────────────────────────────────────────

def story_key(title: str) -> str:
    """Stable short hash of a normalised title — used as the memory key."""
    normalised = title.lower().strip()
    return hashlib.md5(normalised.encode()).hexdigest()[:12]


def load_memory() -> dict:
    """
    Load the story memory JSON file.
    Schema: { "<story_key>": { "title": str, "date": "YYYY-MM-DD" } }
    """
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_memory(memory: dict):
    """Persist the story memory file."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(MEMORY_FILE, "w") as f:
        json.dump(memory, f, indent=2)


def purge_old_memory(memory: dict, days: int = 14) -> dict:
    """Remove entries older than `days` to keep the file lean."""
    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    return {
        k: v for k, v in memory.items()
        if datetime.date.fromisoformat(v["date"]) >= cutoff
    }


def was_recently_covered(title: str, memory: dict) -> bool:
    """Return True if this story key was covered within STORY_MEMORY_DAYS."""
    key = story_key(title)
    if key not in memory:
        return False
    covered_date = datetime.date.fromisoformat(memory[key]["date"])
    age = (datetime.date.today() - covered_date).days
    return age < STORY_MEMORY_DAYS


def mark_covered(titles: list[str], memory: dict) -> dict:
    """Add today's stories to the memory dict."""
    today = datetime.date.today().isoformat()
    for title in titles:
        memory[story_key(title)] = {"title": title, "date": today}
    return memory

# ─────────────────────────────────────────────────────────────────────────────


def fetch_headlines(feeds: list, count: int, memory: dict) -> list[dict]:
    """
    Pull headlines from RSS feeds, filter out recently covered stories,
    but allow them back in if the title contains update-signal words
    (suggesting a meaningful development on an ongoing story).
    """
    UPDATE_SIGNALS = {
        "update", "updated", "new", "latest", "breaks", "breaking",
        "launches", "announces", "confirms", "reveals", "raises",
        "acquires", "banned", "fined", "sues", "ruling", "recall",
        "breach", "hack", "layoffs", "fired", "resigns",
    }

    entries = []
    headers = {"User-Agent": "Mozilla/5.0 (compatible; DailyDumpPodcast/1.0)"}

    for url in feeds:
        try:
            feed = feedparser.parse(url, request_headers=headers)
            for entry in feed.entries[:4]:
                entries.append({
                    "title":     entry.get("title", "").strip(),
                    "summary":   entry.get("summary", entry.get("description", "")),
                    "link":      entry.get("link", ""),
                    "published": entry.get("published", ""),
                })
        except Exception as e:
            print(f"  Warning: could not fetch {url}: {e}")

    # Deduplicate by title
    seen_titles, unique = set(), []
    for e in entries:
        key = e["title"].lower()[:60]
        if key not in seen_titles:
            seen_titles.add(key)
            unique.append(e)

    # Filter: skip recently covered stories unless they carry update signals
    filtered = []
    skipped  = []
    for e in unique:
        title_words = set(e["title"].lower().split())
        has_update_signal = bool(title_words & UPDATE_SIGNALS)

        if was_recently_covered(e["title"], memory) and not has_update_signal:
            skipped.append(e["title"])
            continue
        filtered.append(e)

    if skipped:
        print(f"  Skipped {len(skipped)} recently covered stories")
        for t in skipped[:3]:
            print(f"    - {t[:70]}")

    # If we don't have enough fresh stories, backfill with the least-stale skipped ones
    if len(filtered) < count:
        print(f"  Not enough fresh stories — backfilling with {count - len(filtered)} older ones")
        filtered += [e for e in unique if e not in filtered][: count - len(filtered)]

    return filtered[:count]


def write_script_gemini(headlines: list) -> str:
    """Use Gemini 1.5 Flash (free tier) to write the podcast script."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise ValueError("GEMINI_API_KEY environment variable not set")

    today = datetime.date.today().strftime("%B %d, %Y")

    stories_block = "\n".join(
        f"- {h['title']}: {h['summary'][:300]}" for h in headlines
    )

    prompt = f"""You are writing a script for a daily audio tech news briefing called Daily Dump Tech.
Style: Fast, punchy, news anchor energy. No fluff. No filler phrases like "Let's dive in", "Stay tuned", or "Without further ado."
Each story gets 2-3 tight sentences max. Write for the ear, not the eye — short sentences, active voice, no jargon.
Do NOT use bullet points, markdown, asterisks, or headers anywhere in the output. Spoken word only.
Do NOT name sources. Do NOT start sentences with "today" or "here's".
Target length: about 5 minutes of spoken audio (roughly 650-750 words total).

Today is {today}.

Structure — follow this exactly:
1. One cold-open sentence: date + "five stories in tech"
2. Five stories, each 2-3 sentences. No intros like "story one" or "first up".
3. One sign-off sentence. Keep it dry and punchy — no "thanks for listening."

TECH STORIES:
{stories_block}

Write the full script now:"""

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={api_key}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature":     0.7,
            "maxOutputTokens": 1200,
        },
    }
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def text_to_speech(script: str, output_path: str) -> bool:
    """Convert script to MP3 using Edge TTS (free, no API key)."""
    try:
        import edge_tts
        import asyncio

        VOICE = "en-US-GuyNeural"  # authoritative male news anchor
        RATE  = "+15%"             # slightly faster for punchy feel

        async def _synth():
            communicate = edge_tts.Communicate(script, VOICE, rate=RATE)
            await communicate.save(output_path)

        asyncio.run(_synth())
        return True
    except Exception as e:
        print(f"  TTS error: {e}")
        return False


def get_mp3_duration_seconds(mp3_path: str) -> int:
    """Best-effort MP3 duration estimate."""
    try:
        size = os.path.getsize(mp3_path)
        return max(1, size // 16000)  # ~128kbps CBR
    except Exception:
        return 300  # fallback: 5 minutes


def update_rss_feed(mp3_filename: str, title: str, description: str, mp3_path: str):
    """Append a new episode to docs/feed.xml (served via GitHub Pages)."""
    os.makedirs(FEED_DIR, exist_ok=True)
    os.makedirs(os.path.join(FEED_DIR, "episodes"), exist_ok=True)
    feed_path = os.path.join(FEED_DIR, "feed.xml")

    mp3_url      = f"{PODCAST_BASE_URL}/episodes/{mp3_filename}"
    mp3_size     = os.path.getsize(mp3_path) if os.path.exists(mp3_path) else 0
    duration_sec = get_mp3_duration_seconds(mp3_path)
    pub_date     = datetime.datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")

    new_item = f"""
  <item>
    <title>{title}</title>
    <description>{description}</description>
    <pubDate>{pub_date}</pubDate>
    <enclosure url="{mp3_url}" length="{mp3_size}" type="audio/mpeg"/>
    <itunes:duration>{duration_sec}</itunes:duration>
    <guid isPermaLink="false">{mp3_url}</guid>
  </item>"""

    if os.path.exists(feed_path):
        with open(feed_path, "r", encoding="utf-8") as f:
            existing = f.read()
        updated = existing.replace(
            "  <!-- EPISODES -->", new_item + "\n  <!-- EPISODES -->"
        )
    else:
        updated = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
  xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
  xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>{PODCAST_TITLE}</title>
    <link>{PODCAST_BASE_URL}</link>
    <description>{PODCAST_DESCRIPTION}</description>
    <language>en-us</language>
    <itunes:author>{PODCAST_AUTHOR}</itunes:author>
    <itunes:category text="News"/>
    <itunes:explicit>false</itunes:explicit>
    <itunes:image href="{PODCAST_BASE_URL}/cover.jpg"/>
    <!-- EPISODES -->
{new_item}
  </channel>
</rss>"""

    with open(feed_path, "w", encoding="utf-8") as f:
        f.write(updated)

    print(f"  RSS feed updated → {feed_path}")
    print(f"  Episode URL: {mp3_url}")


def main():
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    mp3_filename = f"daily-dump-tech-{today_str}.mp3"
    mp3_path     = os.path.join(OUTPUT_DIR, mp3_filename)
    script_path  = os.path.join(OUTPUT_DIR, f"daily-dump-tech-{today_str}.txt")

    # ── Load + purge story memory ──
    print(f"[{today_str}] Loading story memory...")
    memory = load_memory()
    memory = purge_old_memory(memory, days=14)
    print(f"  {len(memory)} stories in memory")

    # ── Fetch fresh headlines ──
    print(f"[{today_str}] Fetching tech headlines...")
    headlines = fetch_headlines(TECH_FEEDS, HEADLINES_COUNT, memory)
    print(f"  Using {len(headlines)} stories")

    # ── Write script ──
    print(f"[{today_str}] Writing script with Gemini...")
    script = write_script_gemini(headlines)
    word_count = len(script.split())
    print(f"  Script: {word_count} words (~{word_count // 130} min)")

    with open(script_path, "w") as f:
        f.write(script)
    print(f"  Script saved → {script_path}")

    # ── Convert to audio ──
    print(f"[{today_str}] Converting to audio...")
    success = text_to_speech(script, mp3_path)

    if success:
        size_kb = os.path.getsize(mp3_path) // 1024
        print(f"  MP3 saved → {mp3_path} ({size_kb} KB)")

        # ── Update RSS feed ──
        update_rss_feed(
            mp3_filename,
            title       = f"Daily Dump Tech — {today_str}",
            description = f"Five tech stories for {today_str}.",
            mp3_path    = mp3_path,
        )

        # ── Save memory (only after successful episode) ──
        memory = mark_covered([h["title"] for h in headlines], memory)
        save_memory(memory)
        print(f"  Memory updated → {len(memory)} stories tracked")
    else:
        print("  TTS failed — check edge-tts installation")

    print("Done.")


if __name__ == "__main__":
    main()
