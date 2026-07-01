"""
Daily Dump — Tech Edition
Fetches top tech news from NewsAPI + GNews + a set of top tech RSS feeds
(cross-referenced for importance), skips recently covered stories, writes a
~5-minute script with Gemini, converts to MP3 via Edge TTS, updates the
GitHub Pages RSS feed.
"""

import os
import re
import sys
import json
import hashlib
import datetime
import requests

# ── CONFIG ────────────────────────────────────────────────────────────────────
MIN_STORIES       = 3          # never fewer than this
MAX_STORIES       = 7          # never more than this
CANDIDATE_POOL    = 40         # how many headlines to gather before picking
STORY_MEMORY_DAYS = 3
OUTPUT_DIR        = "output"
FEED_DIR          = "docs"
MEMORY_FILE       = "output/story_memory.json"

# Top tech publications — the same kind of sources TLDR curates from.
# These give wide coverage + a cross-source importance signal (a story covered
# by several outlets is probably a bigger deal).
RSS_FEEDS = [
    "https://techcrunch.com/feed/",
    "https://www.theverge.com/rss/index.xml",
    "https://feeds.arstechnica.com/arstechnica/index",
    "https://www.wired.com/feed/rss",
    "https://hnrss.org/frontpage?points=150",   # Hacker News, 150+ points = high signal
    "https://www.engadget.com/rss.xml",
    "https://www.theregister.com/headlines.atom",
    "https://simonwillison.net/atom/everything/",  # top AI/LLM practitioner blog
    "https://feeds.feedburner.com/TheHackersNews",  # security
    "https://www.bleepingcomputer.com/feed/",        # security
]

PODCAST_TITLE       = "Daily Dump: Tech"
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


def fetch_rss() -> list:
    """
    Fetch recent headlines from the RSS_FEEDS list of top tech publications.
    Only keeps items from roughly the last 2 days. Returns [] on total failure
    but tolerates individual feeds failing.
    """
    try:
        import feedparser
    except ImportError:
        print("  RSS: feedparser not installed, skipping")
        return []

    headers = {"User-Agent": "Mozilla/5.0 (compatible; DailyDumpPodcast/1.0)"}
    cutoff  = datetime.datetime.utcnow() - datetime.timedelta(days=2)
    out     = []
    ok_feeds = 0

    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url, request_headers=headers)
            if not feed.entries:
                continue
            ok_feeds += 1
            source = feed.feed.get("title", url.split("/")[2])
            for entry in feed.entries[:8]:
                title = (entry.get("title") or "").strip()
                if not title:
                    continue
                # Recency filter when a date is available
                published = entry.get("published_parsed") or entry.get("updated_parsed")
                if published:
                    try:
                        pub_dt = datetime.datetime(*published[:6])
                        if pub_dt < cutoff:
                            continue
                    except Exception:
                        pass
                summary = entry.get("summary", entry.get("description", "")) or ""
                # Strip HTML tags from RSS summaries
                summary = re.sub(r"<[^>]+>", "", summary)[:300]
                out.append({
                    "title":   title,
                    "summary": summary,
                    "source":  source,
                })
        except Exception as e:
            print(f"  RSS feed failed ({url}): {e}")
            continue

    print(f"  RSS: {len(out)} stories from {ok_feeds}/{len(RSS_FEEDS)} feeds")
    return out


def _norm_title(title: str) -> str:
    """Normalise a title for cross-source matching."""
    t = title.lower()
    t = re.sub(r"[^a-z0-9 ]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def gather_candidates(memory: dict) -> list:
    """
    Combine NewsAPI + GNews + RSS feeds, dedupe, count how many sources cover
    each story (importance signal), filter recently-covered, and return the
    candidate pool sorted by cross-source coverage.
    """
    combined = fetch_newsapi() + fetch_gnews() + fetch_rss()

    if not combined:
        raise RuntimeError(
            "All news sources returned nothing. Check NEWSAPI_KEY, GNEWS_KEY, "
            "and network access to the RSS feeds."
        )

    # Group near-duplicate stories across sources. Different outlets word their
    # headlines very differently for the SAME event, so title-only matching misses
    # them. We match on significant words from the title AND summary combined, and
    # also treat stories that share a distinctive entity (a capitalized product or
    # company name) as the same story.
    STOPWORDS = {
        "the", "a", "an", "to", "of", "in", "on", "for", "with", "and", "or",
        "at", "by", "is", "are", "as", "new", "now", "its", "it", "this", "that",
        "from", "has", "have", "will", "after", "over", "into", "up", "out",
        "how", "why", "what", "when", "who", "your", "you", "here", "gets",
        "could", "would", "says", "said", "than", "but", "not", "all", "can",
    }

    def sig_words(text):
        return {
            w for w in _norm_title(text).split()
            if w not in STOPWORDS and len(w) > 2
        }

    def entities(raw_title):
        # Distinctive tokens: capitalized words in the ORIGINAL title (product /
        # company names), lowercased for comparison. These are strong same-story
        # signals ("OpenAI", "GPT", "Nvidia", "iPhone").
        found = set()
        for tok in re.findall(r"[A-Za-z0-9]+", raw_title):
            if len(tok) > 2 and (tok[0].isupper() or any(c.isupper() for c in tok[1:])):
                low = tok.lower()
                if low not in STOPWORDS:
                    found.add(low)
        return found

    groups = []  # {title, summary, source_count, _words, _ents}
    for a in combined:
        blob   = f"{a['title']} {a.get('summary', '')}"
        words  = sig_words(blob)
        ents   = entities(a["title"])
        if not words:
            continue
        matched = None
        for grp in groups:
            overlap = words & grp["_words"]
            smaller = min(len(words), len(grp["_words"]))
            word_sim = (len(overlap) / smaller) if smaller else 0
            # Shared distinctive entities (need 2+ shared, or 1 rare one)
            shared_ents = ents & grp["_ents"]
            # Same story if: strong word overlap, OR they share key entities
            # AND have at least modest word overlap (guards against false merges
            # like two unrelated "Apple" stories).
            if word_sim >= 0.55 or (len(shared_ents) >= 2 and word_sim >= 0.3):
                matched = grp
                break
        if matched:
            matched["source_count"] += 1
            matched["_words"] |= words
            matched["_ents"]  |= ents
            if len(a.get("summary", "")) > len(matched.get("summary", "")):
                matched["summary"] = a["summary"]
                matched["title"]   = a["title"]
        else:
            groups.append({
                "title":        a["title"],
                "summary":      a.get("summary", ""),
                "source_count": 1,
                "_words":       words,
                "_ents":        ents,
            })

    unique = [
        {k: v for k, v in g.items() if k not in ("_words", "_ents")}
        for g in groups
    ]

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

    # Sort by how many sources covered it (importance signal), highest first
    fresh.sort(key=lambda x: x["source_count"], reverse=True)

    # Report the strongest cross-source stories
    multi = [f for f in fresh if f["source_count"] > 1]
    if multi:
        print(f"  {len(multi)} stories covered by multiple sources (higher importance):")
        for f in multi[:5]:
            print(f"    [{f['source_count']}x] {f['title'][:60]}")

    # Backfill if too few fresh stories
    if len(fresh) < MIN_STORIES:
        need = MIN_STORIES - len(fresh)
        print(f"  Only {len(fresh)} fresh — backfilling {need} older ones")
        for a in unique:
            if a not in fresh:
                fresh.append(a)
                if len(fresh) >= MIN_STORIES:
                    break

    return fresh[:CANDIDATE_POOL]

# ─────────────────────────────────────────────────────────────────────────────


def write_script_gemini(candidates: list, max_stories: int) -> tuple:
    """
    Gemini judges how many stories are worth covering (between MIN_STORIES and
    max_stories) and writes the script. Returns (script, chosen_titles).
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not set")

    today = datetime.date.today().strftime("%B %d, %Y")

    candidate_block = "\n".join(
        f"{i+1}. [{c.get('source_count', 1)} source(s)] {c['title']}: {c['summary']}"
        for i, c in enumerate(candidates)
    )

    prompt = f"""Today is {today}.

You are writing the script for a daily tech news podcast called Daily Dump: Tech.
It's modeled on the TLDR tech newsletter: written BY an engineer FOR engineers.
Smart, factual, conversational, zero hype. The listener is a developer or founder
who knows the field. This is a PODCAST people listen to on a commute — not release
notes, not documentation.

Below are {len(candidates)} candidate stories.

STEP 0 — MERGE DUPLICATES FIRST:
Several candidates may be the SAME underlying event reported by different outlets
with different headlines (e.g. "OpenAI unveils GPT-5", "GPT-5 is here", "Sam Altman
announces new model" are ONE story). Before anything else, mentally group these.
Treat each real-world event as a SINGLE story — never cover the same event twice
just because it appears multiple times in the list. When you write it up, combine
the details from all the duplicate entries into one segment.

STEP 1 — SCORE EACH STORY FOR NOTEWORTHINESS (think like a sharp tech editor):
Before choosing anything, evaluate each candidate against these questions. This is
how you find quality signal instead of routine noise:

  1. NOVELTY: Is this genuinely new and recent, or a routine, expected, or already-
     old event? Prefer fresh, breaking developments over stories that have been
     circulating for days. Recurring maintenance — minor OS point-releases, routine
     security-patch roundups, "X app gets small update", weekly deal posts — is NOT
     noteworthy even when many outlets cover it. Those outlets cover it out of routine.
  2. CONSEQUENCE: Does it change something? A new capability, a shift in the market,
     a real vulnerability being exploited, a product that didn't exist yesterday, money
     actually moving. If nothing is different afterward, it's not a story.
  3. SURPRISE / INFORMATION: Would a knowledgeable engineer already assume this happened?
     "Apple shipped a bug-fix update" carries near-zero information — everyone knows Apple
     constantly ships those. "Apple shipped an emergency patch for a zero-day being actively
     exploited" carries real information. Same category, totally different noteworthiness.
  4. DEPTH AVAILABLE: Is there enough substance to actually talk about for a minute, or
     would you just be reading the headline back?

A story covered by many sources is only meaningful if it ALSO passes the questions above.
Wide coverage of a routine event (a normal iOS update) is just routine coverage — do not
mistake volume for importance. Weight your own editorial judgment above the source count.

STEP 2 — DECIDE HOW MANY TO COVER ({MIN_STORIES} to {max_stories}):
Cover only the stories that genuinely clear the bar above. A big news day might yield
{max_stories} real stories; a slow day might only yield {MIN_STORIES}. Never pad with
routine non-events to hit a number — a tight episode of {MIN_STORIES} strong stories beats
a padded one. Favor: real AI/ML developments, chips and hardware shifts, meaningful dev-tool
and open-source releases, actual security incidents, and funding/M&A with real figures.

CANDIDATE STORIES (with how many sources covered each — use as ONE input, not the decider):
{candidate_block}

Output in EXACTLY this format (list ONLY the titles you actually chose to cover,
separated by the pipe character — between {MIN_STORIES} and {max_stories} of them):

TITLES: <title 1> | <title 2> | <title 3> | ...

SCRIPT:
<the full script>

=== HOW TO WRITE IT (this is the important part) ===

JUDGE EACH STORY AND ALLOCATE TIME ACCORDINGLY:
Not every story deserves equal time. Before writing, rank the stories you chose by
how big a deal they actually are to a technical audience.
- The 1-2 BIGGEST stories (major AI model, huge acquisition, serious security event):
  give each a solid 5-6 sentences with real substance.
- Mid-tier stories: 3-4 sentences.
- Minor stories (incremental release, small update): 1-2 sentences. Just hit the
  headline fact and move on. It is completely fine for a minor story to be quick.
This variation in length is what makes it sound like a real host with judgment,
not a machine giving everything equal weight.

TALK LIKE A HOST, NOT LIKE DOCUMENTATION:
- Explain what changed and why an engineer would care — at the altitude a smart
  person wants while half-listening on a commute. NOT an exhaustive changelog.
- BAD (do not do this): listing every crate, function name, syscall, config flag,
  or percentage from a release. Nobody wants to hear "the gix-pack cache delta
  decode crate" read aloud.
- GOOD: "Git 2.55 is out, and the headline is Rust support is now on by default —
  part of the slow migration away from C for memory safety. There's also a fix for
  interactive rebase that was mangling merge commits, and some solid speedups for
  git status on Windows." Then move on.
- Give the ONE or TWO details that matter, not all ten.

DO NOT EXPLAIN THINGS THE AUDIENCE ALREADY KNOWS — THIS IS CRITICAL:
Never waste words explaining common concepts, obvious terms, or general background.
Your listener is technical. They already know what things mean.
- BAD: "The tool is called Nano Banana 2 Lite. The 'Lite' likely means it's a
  smaller, faster, more efficient version." — This is padding. Everyone knows what
  Lite means. It adds zero information.
- GOOD: "Google shipped Nano Banana 2 Lite — a faster, cheaper image model that
  runs at [whatever the actual news is: its real specs, price, benchmark, or use case]."
- Every sentence must carry NEW information pulled from the actual reporting: what
  shipped, the numbers, who's affected, what's different. If a sentence only restates
  the obvious or defines a word, DELETE it and replace it with a real detail from the
  sources. Filler explanations are the #1 thing that makes this sound like an AI. Use
  the details in the story summaries provided — that's your source material, draw the
  substance from there rather than inventing generic explanation.

NO SYMBOLS, CODE, OR PATHS — CRITICAL FOR AUDIO:
This is read aloud by text-to-speech. It must contain ZERO of the following:
- No backticks, no code snippets, no function names, no file paths, no crate names
- No special characters like \\ * ? / _ :: -> or bracket syntax
- No syscall notation like stat(2), no camelCase API names read as code
- Spell everything as spoken words. Say "version two point five five" not "2.55"
  only if it flows naturally — otherwise "Git two-point-five-five" is fine.
- If a detail can only be expressed in code or symbols, LEAVE IT OUT. It doesn't
  belong in audio.

VOICE:
- Real contractions, natural rhythm, varied sentence length.
- Lead with the concrete fact: company, number, what shipped.
- Do NOT add "why this matters" sermons or vague attributions ("reports say").
- Ban these words: exciting, fascinating, groundbreaking, revolutionary, game-changer,
  buckle up, let's dive in, stay tuned, it's worth noting, interestingly, notably.

TRANSITIONS — START EACH NEW STORY WITH A CLEAR CUE:
Every story after the first one must OPEN with a short spoken transition so the
listener clearly hears a new subject is starting. VARY it — do not reuse the same
one twice in an episode. Pick ones that fit the story's topic. Examples to draw from
(and invent similar ones):
  "Next," / "Moving on," / "Elsewhere," / "Meanwhile," / "Also today,"
  "In AI," / "On the AI side," / "In chips," / "In security," / "In Apple news,"
  "Over at [company]," / "Switching gears," / "Turning to [topic],"
The FINAL story must start with "Finally," so the listener knows it's the last one.
The opener line and the first story do NOT get a transition (the opener leads
straight into story one).

FORMAT:
- Spoken prose only. No markdown, bullets, asterisks, or headers.
- Don't name the news outlets. Don't start sentences with "today" or "here's".
- OPENER: start with exactly this line, filling in the real date:
  "It's [Month Day]. This is your Daily Dump of tech news."
- CLOSER: end with exactly this line: "And that's it for your Daily Dump. See you again, tomorrow."
- BETWEEN STORIES: put the marker [[PAUSE]] on its own line between each story
  (after you finish one story, before you start the next). This signals a beat of
  silence so the listener hears a clear break between subjects. Do NOT put a pause
  after the opener or before the closer — only between the story bodies.

LENGTH: The episode should always run about 5 minutes. At the podcast's speaking
pace that means roughly 790 words total, NO MATTER how many stories you cover.
This is important: if you only cover 3 stories, give each one MORE depth and context
so the episode still fills 5 minutes. If you cover 7, keep each tighter. Fewer
stories means richer coverage of each, not a shorter episode. Distribute words
UNEVENLY based on importance, but always land around 790 words total.

Begin now:"""

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={api_key}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature":      0.8,
            "maxOutputTokens":  5000,
            "thinkingConfig": {"thinkingBudget": 0},
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

    # Parse the "TITLES: ... \n SCRIPT: ..." format
    script, titles = full_text, []
    if "SCRIPT:" in full_text and "TITLES:" in full_text:
        titles_part, script_part = full_text.split("SCRIPT:", 1)
        titles_part = titles_part.replace("TITLES:", "").strip()
        titles = [t.strip() for t in titles_part.split("|") if t.strip()]
        script = script_part.strip()
    elif "TITLES:" in full_text:
        # Fallback: TITLES present but no SCRIPT label
        head, tail = full_text.rsplit("TITLES:", 1)
        script = head.strip()
        titles = [t.strip() for t in tail.strip().split("|") if t.strip()]

    # Fallback: if no titles parsed, use the top candidate titles we sent
    if not titles:
        titles = [c["title"] for c in candidates[:MIN_STORIES]]

    return script, titles


def expand_script(short_script: str, target_low: int = 750) -> str:
    """
    Ask Gemini to lengthen a too-short script while preserving the TLDR voice.
    Returns the expanded script (or the original if the call fails).
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return short_script

    current = len(short_script.split())
    prompt = f"""This tech news podcast script is too short. It's {current} words
but needs to be about {target_low} words for a full 5-minute episode.

Lengthen it by giving each story — especially the biggest one or two — more depth:
more context on what happened, the background an engineer would want, and the
implications. Do NOT add new stories. Do NOT add technical minutiae like function
names, file paths, crate names, syscalls, or code symbols — this is read aloud by
text-to-speech and symbols sound broken. Do NOT add hype or "why it matters" filler.
Keep the same conversational engineer-to-engineer voice. Spoken prose only, no markdown.

Return ONLY the expanded script, nothing else.

SCRIPT TO EXPAND:
{short_script}"""

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={api_key}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature":      0.8,
            "maxOutputTokens":  5000,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    try:
        resp = requests.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        parts = data["candidates"][0].get("content", {}).get("parts", [])
        expanded = "".join(p.get("text", "") for p in parts).strip()
        # Keep whichever is longer, just in case
        if len(expanded.split()) > len(short_script.split()):
            return expanded
    except Exception as e:
        print(f"  Expansion failed: {e}")
    return short_script


def clean_for_speech(script: str) -> str:
    """
    Safety net: strip characters and patterns that sound broken when read aloud
    by TTS, in case any slipped past the prompt. Preserves normal punctuation and
    leaves [[PAUSE]] markers intact (text_to_speech splits on them).
    """
    import re

    text = script

    # Remove code spans / backticks entirely (keep inner text but drop the ticks)
    text = text.replace("`", "")
    # Remove markdown emphasis characters
    text = text.replace("*", "").replace("_", "")
    # Remove bracketed/paren code-ish notation like stat(2) -> stat
    text = re.sub(r"\(\d+\)", "", text)
    # Collapse "::" and "->" and "/" path separators to spaces
    text = text.replace("::", " ").replace("->", " ").replace("\\", " ")
    # Remove standalone code-symbol characters, but PROTECT the [[PAUSE]] marker.
    text = text.replace("[[PAUSE]]", "\x00PAUSE\x00")
    text = re.sub(r"[<>|{}\[\]#~^]", "", text)
    text = text.replace("\x00PAUSE\x00", "[[PAUSE]]")
    # Fix leftover double spaces and space-before-punctuation
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)

    return text.strip()


def text_to_speech(script: str, output_path: str) -> bool:
    """
    Synthesize the script to MP3. Renders in SEGMENTS split on [[PAUSE]] markers
    and concatenates the MP3 bytes (same-format Edge TTS MP3s concatenate cleanly).
    The closing sign-off line is rendered slightly slower so it doesn't rush.
    """
    try:
        import edge_tts
        import asyncio
        import re

        VOICE       = "en-US-AndrewMultilingualNeural"
        RATE        = "+12%"   # normal pace for the body
        CLOSER_RATE = "+0%"    # slower pace just for the sign-off line

        cleaned = clean_for_speech(script)

        # Split into segments on pause markers (each ≈ one story = short stream)
        raw_segments = re.split(r"\[\[\s*PAUSE\s*\]\]", cleaned, flags=re.IGNORECASE)
        segments = [s.strip() for s in raw_segments if s.strip()]
        if not segments:
            segments = [cleaned]

        # Pull the closing sign-off out of the last segment so we can slow it down.
        # Matches "And that's it for your Daily Dump. See you again, tomorrow."
        closer = None
        closer_pat = re.compile(
            r"(and that'?s it for your daily dump\.?\s*see you again,?\s*tomorrow[.!]?)\s*$",
            re.IGNORECASE,
        )
        if segments:
            m = closer_pat.search(segments[-1])
            if m:
                closer = m.group(1).strip()
                segments[-1] = segments[-1][:m.start()].strip()
                if not segments[-1]:
                    segments.pop()

        async def _synth_segment(text: str, rate: str) -> bytes:
            buf = bytearray()
            communicate = edge_tts.Communicate(text, VOICE, rate=rate)
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    buf.extend(chunk["data"])
            return bytes(buf)

        async def _render(text: str, rate: str, label: str) -> bytes:
            if not text.rstrip().endswith((".", "!", "?")):
                text = text.rstrip() + "."
            audio = b""
            for attempt in range(3):
                audio = await _synth_segment(text, rate)
                if len(audio) > 500:
                    break
                print(f"    {label} retry {attempt+1}...")
            return audio

        async def _make_silence() -> bytes:
            # Generate a real silent gap in the SAME MP3 format as the segments so
            # it concatenates cleanly (no ffmpeg needed). Commas are read as pauses,
            # not spoken, so a short comma string yields near-silent audio. We cap
            # the size we'll accept: a genuine silent clip is small; if it comes
            # back large the voice spoke something, so we discard it. Falls back to
            # no gap rather than breaking the episode.
            try:
                clip = await _synth_segment("  ,  ,  ,  ,  ,  ", "+0%")
                # ~1-2s of 24kbps silence ≈ 3000-7000 bytes. Reject anything huge.
                if 200 < len(clip) < 12000:
                    return clip
                return b""
            except Exception:
                return b""

        async def _run() -> bytes:
            gap = await _make_silence()
            out = bytearray()
            for i, seg in enumerate(segments):
                out.extend(await _render(seg, RATE, f"segment {i+1}"))
                # Insert a clear pause between stories (not after the last segment)
                is_last_body = (i == len(segments) - 1)
                if not is_last_body and gap:
                    out.extend(gap)
            if closer:
                # A pause before the closer too, then the slower sign-off.
                if gap:
                    out.extend(gap)
                out.extend(await _render(closer, CLOSER_RATE, "closer"))
            return bytes(out)

        audio_bytes = asyncio.run(_run())
        if len(audio_bytes) < 1000:
            print("  TTS produced almost no audio — treating as failure")
            return False

        with open(output_path, "wb") as f:
            f.write(audio_bytes)
        n = len(segments) + (1 if closer else 0)
        print(f"    (rendered {n} segments{' + slow closer' if closer else ''})")
        return True
    except Exception as e:
        print(f"  TTS error: {e}")
        return False


def get_mp3_duration_seconds(mp3_path: str) -> int:
    """
    Get MP3 duration. Tries mutagen for the real value; falls back to a
    bitrate-based estimate. Edge TTS output is ~24 kbps mono → ~3000 bytes/sec.
    """
    # Try to read the true duration if mutagen is available
    try:
        from mutagen.mp3 import MP3
        audio = MP3(mp3_path)
        if audio.info and audio.info.length > 0:
            return int(audio.info.length)
    except Exception:
        pass
    # Fallback estimate tuned to Edge TTS bitrate (~24 kbps = ~3000 bytes/sec)
    try:
        return max(1, os.path.getsize(mp3_path) // 3000)
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

        # De-dupe: if an episode for this same MP3 (same day) already exists,
        # remove it so we replace rather than stack duplicates. Matches on the
        # enclosure URL, which contains the dated filename.
        import re
        pattern = re.compile(
            r"\s*<item>(?:(?!</item>).)*?"
            + re.escape(mp3_filename)
            + r"(?:(?!</item>).)*?</item>",
            re.DOTALL,
        )
        existing, n_removed = pattern.subn("", existing)
        if n_removed:
            print(f"  Replaced {n_removed} existing entry for {mp3_filename}")

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
    # --fresh (or --test): testing mode. Ignores story memory so you can
    # regenerate repeatedly on the same day and get different story picks, and
    # does NOT write to memory (so it won't affect real daily runs). Also
    # nudges candidate ordering so Gemini doesn't keep landing on the same set.
    FRESH = ("--fresh" in sys.argv) or ("--test" in sys.argv)

    today_str = datetime.date.today().strftime("%Y-%m-%d")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    mp3_filename = f"daily-dump-tech-{today_str}.mp3"
    mp3_path     = os.path.join(OUTPUT_DIR, mp3_filename)
    script_path  = os.path.join(OUTPUT_DIR, f"daily-dump-tech-{today_str}.txt")

    if FRESH:
        print("[TEST MODE] --fresh: ignoring memory, won't save memory, "
              "randomizing candidate order")

    print(f"[{today_str}] Loading story memory...")
    memory = load_memory()
    memory = purge_old_memory(memory, days=14)
    print(f"  {len(memory)} stories in memory")

    # In fresh/test mode, pretend memory is empty for filtering purposes so all
    # stories are eligible and we're not stuck with the same leftovers.
    filter_memory = {} if FRESH else memory

    print(f"[{today_str}] Fetching news from NewsAPI + GNews...")
    candidates = gather_candidates(filter_memory)
    print(f"  {len(candidates)} candidate stories after filtering")

    if FRESH:
        # Shuffle so Gemini sees a different ordering each run and doesn't keep
        # gravitating to the same top-of-list stories.
        import random
        random.shuffle(candidates)
        print("  [TEST MODE] candidate order randomized")

    # Dynamic ceiling: cap at MAX_STORIES, but lower it if the news pool is thin.
    # Roughly: allow 1 story per ~3 candidates, clamped to [MIN_STORIES, MAX_STORIES].
    ceiling = max(MIN_STORIES, min(MAX_STORIES, len(candidates) // 3))
    print(f"  Story ceiling for today: {ceiling} (Gemini picks {MIN_STORIES}-{ceiling})")

    print(f"[{today_str}] Writing script with Gemini...")
    script, titles = write_script_gemini(candidates, ceiling)

    def _wc(s):
        # Word count excluding pause markers
        import re
        return len(re.sub(r"\[\[\s*PAUSE\s*\]\]", " ", s, flags=re.IGNORECASE).split())

    word_count = _wc(script)
    n_stories = len(titles)
    print(f"  Stories chosen: {n_stories}")
    for t in titles:
        print(f"    - {t[:70]}")
    print(f"  Script: {word_count} words (~{word_count // 130} min)")

    # Always aim for ~5 minutes. At +12% speed that's ~790 words. Expand if short.
    if word_count < 740:
        print(f"  Under 5-min target — asking Gemini to expand...")
        script = expand_script(script, target_low=790)
        word_count = _wc(script)
        print(f"  After expansion: {word_count} words (~{word_count // 165} min)")

    # Absolute stub guard: below this, something is genuinely broken.
    if word_count < 450:
        raise RuntimeError(
            f"Script too short ({word_count} words) — aborting so we don't "
            "publish a broken episode. Check the Gemini response above."
        )

    # Save a clean transcript (pause markers removed) for the archive
    import re as _re
    clean_transcript = _re.sub(r"\[\[\s*PAUSE\s*\]\]", "", script, flags=_re.IGNORECASE)
    clean_transcript = _re.sub(r"\n{3,}", "\n\n", clean_transcript).strip()
    with open(script_path, "w") as f:
        f.write(clean_transcript)
    print(f"  Script saved → {script_path}")

    print(f"[{today_str}] Converting to audio...")
    if not text_to_speech(script, mp3_path):
        raise RuntimeError("TTS failed — check edge-tts installation")

    size_kb = os.path.getsize(mp3_path) // 1024
    print(f"  MP3 saved → {mp3_path} ({size_kb} KB)")

    update_rss_feed(
        mp3_filename,
        title       = f"Daily Dump: Tech — {today_str}",
        description = f"Five tech stories for {today_str}.",
        mp3_path    = mp3_path,
    )

    if FRESH:
        print("  [TEST MODE] skipping memory save (won't affect real runs)")
    else:
        memory = mark_covered(titles, memory)
        save_memory(memory)
        print(f"  Memory updated → {len(memory)} stories tracked")
    print("Done.")


if __name__ == "__main__":
    main()
