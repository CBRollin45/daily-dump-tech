"""
Daily Dump — Tech Edition
Fetches top tech news from NewsAPI + GNews (dual source for reliability),
skips recently covered stories (unless major update), writes a punchy
5-minute script with Gemini, converts to MP3 via Edge TTS, updates the
GitHub Pages RSS feed.
"""

import os
import json
import hashlib
import datetime
import requests

# ── CONFIG ────────────────────────────────────────────────────────────────────
HEADLINES_COUNT   = 5          # stories used per episode
CANDIDATE_POOL    = 25         # how many headlines to gather before picking
STORY_MEMORY_DAYS = 3
OUTPUT_DIR        = "output"
FEED_DIR          = "docs"
MEMORY_FILE       = "output/story_memory.json"

PODCAST_TITLE       = "Daily Dump Tech"
PODCAST_DESCRIPTION = "Fast daily tech news. No fluff. Five minutes."
PODCAST_AUTHOR      = "Daily Dump Bot"
PODCAST_BASE_URL    = os.environ.get(
    "PODCAST_BASE_URL", "https://example.github.io/daily-dump-tech"
)

# Update-signal words: let a recently-covered story back in if its headline
# suggests a genuinely new development.
UPDATE_SIGNALS = {
    "update", "updated", "breaking", "breaks", "launches", "launched",
    "announces", "announced", "confirms", "confirmed", "reveals",
    "raises", "acquires", "acquired", "banned", "bans", "fined",
    "sues", "sued", "ruling", "recall", "recalls", "breach", "hacked",
    "hack", "layoffs", "fired", "resigns", "shuts", "settles",
}
# ─────────────────────────────────────────────────────────────────────────────


# ── STORY MEMORY ─────────────────────────────────────────────────────────────

def story_key(title: str) -> str:
    return hashlib.md5(title.lower().strip().encode()).hexdigest()[:12]


def load_memory() -> dict:
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_memory(memory: dict):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(MEMORY_FILE, "w") as f:
        json.dump(memory, f, indent=2)


def purge_old_memory(memory: dict, days: int = 14) -> dict:
    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    keep = {}
    for k, v in memory.items():
        try:
            if datetime.date.fromisoformat(v["date"]) >= cutoff:
                keep[k] = v
        except Exception:
            continue
    return keep


def was_recently_covered(title: str, memory: dict) -> bool:
    key = story_key(title)
    if key not in memory:
        return False
    try:
        covered = datetime.date.fromisoformat(memory[key]["date"])
    except Exception:
        return False
    return (datetime.date.today() - covered).days < STORY_MEMORY_DAYS


def mark_covered(titles: list, memory: dict) -> dict:
    today = datetime.date.today().isoformat()
    for title in titles:
        memory[story_key(title)] = {"title": title, "date": today}
    return memory

# ─────────────────────────────────────────────────────────────────────────────


# ── NEWS FETCHING (dual source with fallback) ────────────────────────────────

def fetch_newsapi() -> list:
    """Fetch top US tech headlines from NewsAPI.org. Returns [] on any failure."""
    key = os.environ.get("NEWSAPI_KEY", "")
    if not key:
        print("  NewsAPI: no key set, skipping")
        return []
    try:
        resp = requests.get(
            "https://newsapi.org/v2/top-headlines",
            params={
                "category": "technology",
                "country":  "us",
                "pageSize": 20,
                "apiKey":   key,
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        articles = data.get("articles", [])
        out = []
        for a in articles:
            title = (a.get("title") or "").strip()
            if title and title != "[Removed]":
                out.append({
                    "title":   title,
                    "summary": (a.get("description") or "")[:300],
                })
        print(f"  NewsAPI: {len(out)} stories")
        return out
    except Exception as e:
        print(f"  NewsAPI failed: {e}")
        return []


def fetch_gnews() -> list:
    """Fetch top tech headlines from GNews.io. Returns [] on any failure."""
    key = os.environ.get("GNEWS_KEY", "")
    if not key:
        print("  GNews: no key set, skipping")
        return []
    try:
        resp = requests.get(
            "https://gnews.io/api/v4/top-headlines",
            params={
                "category": "technology",
                "lang":     "en",
                "country":  "us",
                "max":      10,
                "apikey":   key,
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        articles = data.get("articles", [])
        out = []
        for a in articles:
            title = (a.get("title") or "").strip()
            if title:
                out.append({
                    "title":   title,
                    "summary": (a.get("description") or "")[:300],
                })
        print(f"  GNews: {len(out)} stories")
        return out
    except Exception as e:
        print(f"  GNews failed: {e}")
        return []


def gather_candidates(memory: dict) -> list:
    """
    Combine both sources, dedupe, and filter out recently-covered stories
    (unless they carry an update signal). Returns the candidate pool.
    """
    combined = fetch_newsapi() + fetch_gnews()

    if not combined:
        raise RuntimeError(
            "Both news sources returned nothing. Check NEWSAPI_KEY and GNEWS_KEY."
        )

    # Dedupe by normalised title prefix
    seen, unique = set(), []
    for a in combined:
        k = a["title"].lower()[:55]
        if k not in seen:
            seen.add(k)
            unique.append(a)

    # Filter recently-covered unless update signal present
    fresh, skipped = [], []
    for a in unique:
        words = set(a["title"].lower().replace(",", " ").replace(".", " ").split())
        has_signal = bool(words & UPDATE_SIGNALS)
        if was_recently_covered(a["title"], memory) and not has_signal:
            skipped.append(a["title"])
        else:
            fresh.append(a)

    if skipped:
        print(f"  Skipped {len(skipped)} recently covered stories")

    # Backfill if too few fresh stories
    if len(fresh) < HEADLINES_COUNT:
        need = HEADLINES_COUNT - len(fresh)
        print(f"  Only {len(fresh)} fresh — backfilling {need} older ones")
        for a in unique:
            if a not in fresh:
                fresh.append(a)
                if len(fresh) >= HEADLINES_COUNT:
                    break

    return fresh[:CANDIDATE_POOL]

# ─────────────────────────────────────────────────────────────────────────────


def write_script_gemini(candidates: list) -> tuple:
    """
    Gemini picks the best HEADLINES_COUNT stories from the candidate pool
    and writes the script. Returns (script, chosen_titles).
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not set")

    today = datetime.date.today().strftime("%B %d, %Y")

    candidate_block = "\n".join(
        f"{i+1}. {c['title']}: {c['summary']}" for i, c in enumerate(candidates)
    )

    prompt = f"""Today is {today}.

You are writing a script for a daily audio tech news briefing called Daily Dump Tech.

Below are {len(candidates)} candidate tech news stories from today. Pick the {HEADLINES_COUNT}
MOST important and interesting ones (prioritize: major AI developments, big tech company
news, significant product launches, cybersecurity, notable startups/funding, science/space tech).
Skip anything that reads like an ad, listicle, or evergreen how-to.

CANDIDATE STORIES:
{candidate_block}

Now write the full podcast script following these rules EXACTLY:

STYLE RULES:
- Fast, punchy news anchor energy. No fluff.
- No filler phrases: no "Let's dive in", "Stay tuned", "Without further ado"
- Each story: 2-3 tight sentences max
- Write for the ear — short sentences, active voice
- NO bullet points, NO markdown, NO asterisks, NO headers
- Do NOT name news sources
- Do NOT start sentences with "today" or "here's"
- Target length: 650-750 words (about 5 minutes of audio)

STRUCTURE:
1. One cold-open sentence: mention the date and that it's five stories in tech
2. The five stories back to back, 2-3 sentences each (no "story one" labels)
3. One dry, punchy sign-off sentence

After the script, on a brand new line, output exactly:
TITLES: <chosen title 1> | <chosen title 2> | <chosen title 3> | <chosen title 4> | <chosen title 5>

Write it now:"""

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={api_key}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature":     0.7,
            "maxOutputTokens": 2000,
        },
    }

    resp = requests.post(url, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    # Robust extraction — handle multi-part responses
    candidate = data["candidates"][0]
    parts = candidate.get("content", {}).get("parts", [])
    full_text = "".join(p.get("text", "") for p in parts).strip()

    if not full_text:
        raise RuntimeError(f"Gemini returned empty text. Raw: {json.dumps(data)[:500]}")

    # Separate script from the TITLES line
    script, titles = full_text, []
    if "TITLES:" in full_text:
        head, tail = full_text.rsplit("TITLES:", 1)
        script = head.strip()
        titles = [t.strip() for t in tail.strip().split("|") if t.strip()]

    # Fallback: if no titles parsed, use the candidate titles we sent
    if not titles:
        titles = [c["title"] for c in candidates[:HEADLINES_COUNT]]

    return script, titles


def text_to_speech(script: str, output_path: str) -> bool:
    try:
        import edge_tts
        import asyncio

        VOICE = "en-US-GuyNeural"
        RATE  = "+15%"

        async def _synth():
            communicate = edge_tts.Communicate(script, VOICE, rate=RATE)
            await communicate.save(output_path)

        asyncio.run(_synth())
        return True
    except Exception as e:
        print(f"  TTS error: {e}")
        return False


def get_mp3_duration_seconds(mp3_path: str) -> int:
    try:
        return max(1, os.path.getsize(mp3_path) // 16000)
    except Exception:
        return 300


def update_rss_feed(mp3_filename: str, title: str, description: str, mp3_path: str):
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

    print(f"[{today_str}] Loading story memory...")
    memory = load_memory()
    memory = purge_old_memory(memory, days=14)
    print(f"  {len(memory)} stories in memory")

    print(f"[{today_str}] Fetching news from NewsAPI + GNews...")
    candidates = gather_candidates(memory)
    print(f"  {len(candidates)} candidate stories after filtering")

    print(f"[{today_str}] Writing script with Gemini...")
    script, titles = write_script_gemini(candidates)
    word_count = len(script.split())
    print(f"  Script: {word_count} words (~{word_count // 130} min)")
    print(f"  Stories chosen: {len(titles)}")
    for t in titles:
        print(f"    - {t[:70]}")

    # Safety guard: never publish a stub
    if word_count < 300:
        raise RuntimeError(
            f"Script too short ({word_count} words) — aborting so we don't "
            "publish a broken episode. Check the Gemini response above."
        )

    with open(script_path, "w") as f:
        f.write(script)
    print(f"  Script saved → {script_path}")

    print(f"[{today_str}] Converting to audio...")
    if not text_to_speech(script, mp3_path):
        raise RuntimeError("TTS failed — check edge-tts installation")

    size_kb = os.path.getsize(mp3_path) // 1024
    print(f"  MP3 saved → {mp3_path} ({size_kb} KB)")

    update_rss_feed(
        mp3_filename,
        title       = f"Daily Dump Tech — {today_str}",
        description = f"Five tech stories for {today_str}.",
        mp3_path    = mp3_path,
    )

    memory = mark_covered(titles, memory)
    save_memory(memory)
    print(f"  Memory updated → {len(memory)} stories tracked")
    print("Done.")


if __name__ == "__main__":
    main()
