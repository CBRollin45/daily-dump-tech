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
MIN_STORIES       = 4          # never fewer than this
MAX_STORIES       = 10         # denser format fits more stories in 5 min
CANDIDATE_POOL    = 40         # how many headlines to gather before picking
STORY_MEMORY_DAYS = 30         # don't repeat a story within this window unless it
                               # has a genuinely significant new development (judged
                               # by the AI, not keyword matching)
OUTPUT_DIR        = "output"
FEED_DIR          = "docs"
MEMORY_FILE       = "output/story_memory.json"

# Top tech publications — the same kind of sources TLDR curates from.
# These give wide coverage + a cross-source importance signal (a story covered
# by several outlets is probably a bigger deal).
# Source diet aimed at a tech-savvy (not necessarily engineer) audience: strong
# general tech outlets first, a lighter touch of developer/security sources so the
# pool isn't flooded with in-the-weeds tooling or routine security noise.
RSS_FEEDS = [
    "https://techcrunch.com/feed/",
    "https://www.theverge.com/rss/index.xml",
    "https://feeds.arstechnica.com/arstechnica/index",
    "https://www.wired.com/feed/rss",
    "https://www.engadget.com/rss.xml",
    "https://hnrss.org/frontpage?points=300",  # Hacker News, 300+ pts = only the biggest
    "https://www.bleepingcomputer.com/feed/",   # security (kept to one, for major breaches)
]

PODCAST_TITLE       = "Daily Dump: Tech"
PODCAST_DESCRIPTION = "Fast daily tech news. No fluff. Five minutes."
PODCAST_AUTHOR      = "Daily Dump Bot"
PODCAST_BASE_URL    = os.environ.get(
    "PODCAST_BASE_URL", "https://example.github.io/daily-dump-tech"
)

# Gemini model. 3.1 Flash-Lite: free tier, 15 RPM (vs 10 for 3 Flash), quality
# on par with 2.5 Flash, better instruction following. One-line swap if needed.
GEMINI_MODEL = "gemini-3.1-flash-lite"
# ─────────────────────────────────────────────────────────────────────────────


def call_gemini(prompt: str, temperature: float = 0.8,
                max_tokens: int = 5000) -> str:
    """
    Single shared Gemini caller. Uses the Gemini 3.x thinkingLevel control
    (minimal = fastest, cheapest, full output budget for the script). If the
    model rejects that config (older/newer API variations), retries once
    without any thinkingConfig rather than failing the whole run.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not set")

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={api_key}"
    )

    def _payload(with_thinking: bool):
        cfg = {"temperature": temperature, "maxOutputTokens": max_tokens}
        if with_thinking:
            cfg["thinkingConfig"] = {"thinkingLevel": "minimal"}
        return {"contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": cfg}

    last_err = None
    for with_thinking in (True, False):
        try:
            resp = requests.post(url, json=_payload(with_thinking), timeout=90)
            if resp.status_code == 400 and with_thinking:
                # thinkingConfig shape rejected — retry without it
                print("  (thinkingConfig rejected, retrying without it)")
                continue
            resp.raise_for_status()
            data = resp.json()
            parts = data["candidates"][0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts).strip()
            if text:
                return text
            last_err = RuntimeError(
                f"Gemini returned empty text. Raw: {json.dumps(data)[:500]}"
            )
        except Exception as e:
            last_err = e
    raise last_err


# ── STORY MEMORY ─────────────────────────────────────────────────────────────

def story_key(title: str) -> str:
    return hashlib.md5(title.lower().strip().encode()).hexdigest()[:12]


_MEMORY_STOPWORDS = {
    "the", "a", "an", "to", "of", "in", "on", "for", "with", "and", "or",
    "at", "by", "is", "are", "as", "new", "now", "its", "it", "this", "that",
    "from", "has", "have", "will", "after", "over", "into", "up", "out",
    "how", "why", "what", "when", "who", "your", "you", "here", "gets",
    "could", "would", "says", "said", "than", "but", "not", "all", "can",
    # Common news verbs — shared verbs don't mean shared story
    "launches", "launched", "announces", "announced", "releases", "released",
    "ships", "shipped", "unveils", "unveiled", "introduces", "introduced",
    "reveals", "revealed", "expands", "expanded", "rolls", "rolling", "hits",
    "faces", "facing", "gets", "getting", "makes", "making", "takes", "using",
}


def _title_sig_words(title: str) -> set:
    t = re.sub(r"[^a-z0-9 ]", "", title.lower())
    return {w for w in t.split() if w not in _MEMORY_STOPWORDS and len(w) > 2}


def was_recently_covered(title: str, memory: dict):
    """
    Fuzzy check: is this headline the same STORY as one covered recently?
    Exact-hash matching fails across days because outlets re-headline the same
    event ("OpenAI launches GPT-5" -> "GPT-5 rollout expands").
    Matches if any of:
      - 2+ shared significant words (with news verbs stopworded out)
      - a shared distinctive token containing a digit (gpt5, ios26, m5...)
      - >= 50% overlap of the smaller word set
    Returns the matching memory entry (dict) if found, else None.
    """
    words = _title_sig_words(title)
    if not words:
        return None
    today = datetime.date.today()
    for entry in memory.values():
        try:
            covered = datetime.date.fromisoformat(entry["date"])
        except Exception:
            continue
        if (today - covered).days >= STORY_MEMORY_DAYS:
            continue
        prev_words = _title_sig_words(entry.get("title", ""))
        if not prev_words:
            continue
        overlap = words & prev_words
        if not overlap:
            continue
        smaller = min(len(words), len(prev_words))
        ratio = len(overlap) / smaller if smaller else 0
        digit_entity = any(any(c.isdigit() for c in w) for w in overlap)
        if len(overlap) >= 2 or digit_entity or ratio >= 0.5:
            return entry
    return None


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

    # Separate never-covered stories (always eligible) from recently-covered ones.
    # We do NOT use keyword matching to decide if a repeat is allowed — that's
    # unreliable. Instead we FLAG recently-covered stories and let the AI judge
    # (in the selection prompt) whether there's a significant NEW development that
    # justifies re-covering. Stories never covered pass through untouched.
    fresh, recently_covered = [], []
    for a in unique:
        prior = was_recently_covered(a["title"], memory)
        if prior:
            a["_prior_date"]  = prior.get("date", "")
            a["_prior_title"] = prior.get("title", "")
            recently_covered.append(a)
        else:
            fresh.append(a)

    if recently_covered:
        print(f"  {len(recently_covered)} stories were covered in the last "
              f"{STORY_MEMORY_DAYS} days — flagged for AI to judge if there's a "
              f"significant update")

    # Sort fresh by cross-source coverage (importance signal), highest first
    fresh.sort(key=lambda x: x["source_count"], reverse=True)

    # Recently-covered candidates go at the END of the pool, clearly marked, so
    # the AI sees them but treats them as repeats to be judged, not new stories.
    for a in recently_covered:
        a["_is_repeat"] = True
    fresh_all = fresh + recently_covered

    # Report the strongest cross-source stories
    multi = [f for f in fresh if f["source_count"] > 1]
    if multi:
        print(f"  {len(multi)} stories covered by multiple sources (higher importance):")
        for f in multi[:5]:
            print(f"    [{f['source_count']}x] {f['title'][:60]}")

    fresh = fresh_all

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

    def _fmt_candidate(i, c):
        tag = f"[{c.get('source_count', 1)} source(s)]"
        if c.get("_is_repeat"):
            tag += f" [ALREADY COVERED on {c.get('_prior_date', '?')} — only re-cover if genuinely significant NEW development]"
        return f"{i+1}. {tag} {c['title']}: {c['summary']}"

    candidate_block = "\n".join(
        _fmt_candidate(i, c) for i, c in enumerate(candidates)
    )

    prompt = f"""Today is {today}.

You are writing the script for a daily tech news podcast called Daily Dump: Tech.
The listener is TECH-SAVVY BUT NOT NECESSARILY A WORKING ENGINEER — someone who
follows tech closely and wants to stay informed on what's actually happening in the
industry. Think: the important tech stories an informed person would want to know
about and might bring up with a colleague. NOT a developer changelog, NOT deep
in-the-weeds tooling detail, but also NOT dumbed-down consumer fluff. Smart, factual,
fast, zero hype. This is a briefing people listen to on a commute.

Below are {len(candidates)} candidate stories.

STEP 0 — MERGE DUPLICATES FIRST:
Several candidates may be the SAME underlying event reported by different outlets
with different headlines (e.g. "OpenAI unveils GPT-5", "GPT-5 is here", "Sam Altman
announces new model" are ONE story). Before anything else, mentally group these.
Treat each real-world event as a SINGLE story — never cover the same event twice
just because it appears multiple times in the list. When you write it up, combine
the details from all the duplicate entries into one segment.

REPEATS — DO NOT RE-COVER OLD NEWS UNLESS THERE'S A REAL UPDATE:
Some candidates are marked "[ALREADY COVERED on <date>]". These were in a recent
episode. Do NOT cover them again UNLESS the current reporting shows a genuinely
significant NEW development since then — not just the same story still circulating,
and not a trivial follow-up. Judge significance by substance: a major new fact, a
resolution, a big escalation, real new numbers, a reversal. If it's just the same
story being re-reported with nothing materially new, SKIP it — the listener already
heard it. When in doubt, skip it. Prefer genuinely fresh stories over any repeat.

STEP 1 — THE BAR: WHAT MAKES A STORY WORTH INCLUDING
For each candidate, apply this three-part test. The strongest stories pass all three;
be skeptical of anything that clearly fails one.
  1. TALKABLE: Would a tech-savvy person actually bring this up with a colleague or
     friend? Is it interesting on its own, not just trade-industry housekeeping?
  2. BROADLY MATTERS: Does it affect or matter to a LOT of people — not just the ops
     team at one big company? Impact and reach, not niche relevance.
  3. GENUINELY NEW / A REAL CHANGE: Is this a first, a launch, a real shift — not a
     routine, recurring, or expected event?

WHAT TO INCLUDE (with the calibration you should use):
- New AI models and major AI product releases (a new OpenAI/Google/Anthropic model = yes).
- Major tech business, legal, and culture news IF it's big: a major company suing
  another (Apple v. OpenAI = yes), a landmark acquisition, a major exec/company shakeup.
- A major developer tool or framework release ONLY if it's a huge, landmark version —
  not routine point-releases. A general "big GitHub update / new capability" is fine;
  a minor version bump is not.
- A big consumer product launch ONLY if it's genuinely significant (a major new iPhone,
  a landmark device) — not routine refreshes or gadget reviews.
- Tech policy/regulation ONLY if it directly hits major companies or products (a big
  antitrust ruling, a major fine, an AI law that changes how big players operate).
- Security news ONLY if it's genuinely widespread and matters to a lot of ordinary
  people — a breach or vulnerability affecting millions, a major platform compromise.

WHAT TO EXCLUDE (this is where quality lives):
- Routine maintenance: minor OS point-releases, "app gets small update", patch roundups.
- Common/frequent security noise: "another ransomware strain found", a run-of-the-mill
  CVE, a breach at one company that only its own security team needs to care about.
  Only include security if it's truly widespread and broadly important.
- In-the-weeds developer tooling that only working engineers on that stack would care about.
- Gadget reviews, hands-on impressions, deal roundups, gaming, entertainment fluff.
- Rumors, speculation posts, and "here's what to expect" pieces with no concrete news.

A story covered by many sources is only meaningful if it ALSO passes the three-part test.
Wide coverage of a routine event (a normal iOS update) is just routine coverage — do not
mistake volume for importance. Weight your own editorial judgment above the source count.

STEP 2 — DECIDE HOW MANY TO COVER ({MIN_STORIES} to {max_stories}):
This is a dense briefing, so lean toward covering MORE stories when the news supports
it — breadth is the point. Cover every story that clears the bar above. A big news day
should hit {max_stories}; only a genuinely slow day drops toward {MIN_STORIES}. Never
pad with routine non-events to hit a number, but don't artificially limit yourself —
if there are 9 stories that genuinely pass the test, cover 9.

CANDIDATE STORIES (with how many sources covered each — use as ONE input, not the decider):
{candidate_block}

Output in EXACTLY this format (list ONLY the titles you actually chose to cover,
separated by the pipe character — between {MIN_STORIES} and {max_stories} of them):

TITLES: <title 1> | <title 2> | <title 3> | ...

SCRIPT:
<the full script>

=== HOW TO WRITE IT (this is the important part) ===

DENSITY — KEEP IT TIGHT, PACK IN MORE STORIES:
This is a fast, factual briefing, not a talk show. The goal is maximum information
per minute. Most stories should be just 2-3 tight sentences: the fact, the key
number or detail, done. Do NOT stretch a story into a paragraph to fill time.
- Biggest 1-2 stories of the day: up to 4 sentences if there's real substance.
- Everything else: 2-3 sentences. State the fact and the one detail that matters, move on.
- A short, dense episode covering MORE stories beats a padded one covering fewer.
  Prefer breadth: more stories, each tight, over a few stories each drawn out.
- Never add a sentence just to reach a length target. If a story's told in two
  sentences, that's two sentences.

WHAT COUNTS AS SIGNAL VS NOISE — THE MOST IMPORTANT PRINCIPLE:
The source articles are written by journalists who pad stories with narrative color:
anecdotes about individuals, quoted reactions, "one user tried X", human-interest
framing, and explanations of basic concepts. To a technical listener, that color is
NOISE — it carries zero information. Your job is to extract the SIGNAL and throw the
rest away. Before including ANY fact, ask: "Does this change what a knowledgeable
engineer knows or thinks?" If yes, include it. If it's just flavor, cut it.

SIGNAL (include) — facts that change the listener's model of the world:
  what shipped or happened, hard numbers (price, performance, funding, users affected),
  capabilities gained or lost, benchmarks, what it competes with, what breaks, who's
  affected at scale, the actual consequence.

NOISE (cut, even though it's in the source) — narrative color that informs no one:
  - Anecdotes about a specific person: "one developer used it to make an image of X",
    "a user on social media said", "someone built a demo that". Nobody cares what one
    random person did with it. Report the CAPABILITY, not the anecdote.
  - Reactions and sentiment: "users are excited", "the community is divided",
    "reviewers praised it". Report what it DOES, not how people feel about it.
  - Explanations of things the audience knows: defining common terms, spelling out
    what "Lite" or "beta" or "open source" means. Cut entirely.
  - Vague attributions: "reports suggest", "sources say", "it's rumored".

WORKED EXAMPLES:
- Source says: "Google's Nano Banana 2 Lite is a lighter version of its image model.
  One developer on X used it to generate a photorealistic cat in under a second."
  BAD (relays the anecdote): "Google released Nano Banana 2 Lite, and one developer
  used it to create a photorealistic cat image."
  GOOD (extracts the signal): "Google shipped Nano Banana 2 Lite — a smaller image
  model that generates in under a second, aimed at cheap, low-latency use."
  (The "under a second" is signal — it's a real capability. The specific cat and the
  specific developer are noise.)

FACTS ONLY — NO SPECULATION, NO ANALYSIS, NO OPINION, NO ADVICE:
You are a wire service relaying facts, not a commentator, analyst, or advisor.
Report what HAPPENED and stop. Three hard rules:

1. NO ADVICE OR RECOMMENDATIONS. Never tell the listener what they "should" do.
   They are experienced engineers — they already know how to do their jobs. Telling
   them the obvious is condescending and wastes time.
   - BANNED: "security teams should patch right away", "developers should update to",
     "you'll want to test this before", "it's worth keeping an eye on", "make sure to",
     "users are advised to", "the takeaway is", "if you're running X, you should".
   - Just state the fact. "A critical flaw in OpenSSL lets an attacker run code
     remotely; a patch is out." — that's it. The listener knows a critical remote-code
     flaw means patch now. Do NOT say it.

2. NO SPECULATION OR PREDICTION about the future or the meaning of events.
   - BANNED: "this could imply", "this might affect", "this may signal", "expect to
     see", "this suggests", "it remains to be seen", "time will tell", "this positions
     them to", "this raises questions about", "in the long run", or ANY sentence about
     what might/could/may happen next.

3. NO EDITORIALIZING. Don't tell the listener how to feel or how big a deal something
   is. Report the facts that establish scale (the numbers, who's affected) and let
   them judge.

If a sentence is about the FUTURE, about what someone SHOULD do, or about what an
event MEANS rather than what happened, delete it. Factual context IS allowed: what it
replaces, what it costs, what it's compatible with, who uses it, what the prior version
did, and concrete stated plans ("the company says the fix ships in August"). Those are
verifiable facts. Advice, predictions, and interpretation are not.

TIGHT AND FACTUAL, NOT DOCUMENTATION AND NOT CHATTY:
- State what happened plus the one or two factual details that matter (a real number,
  what it replaces, who's affected). Then move to the next story. No warm-up, no wind-down.
- Write for a tech-savvy but non-engineer listener: explain at the level of "what
  happened and why it's a big deal," not deep implementation detail. Skip jargon,
  code names, version-string minutiae, and anything only a specialist on that stack
  would follow.
- BAD (too in-the-weeds): "Git 2.55 enables the gix-pack cache delta decode crate and
  optimizes stat syscall patterns." Nobody but a Git internals dev cares.
- BAD (too chatty): "So this is actually a pretty interesting one, and it's something
  a lot of people have been waiting for..." — cut all of that.
- GOOD (tight, right altitude): "GitHub shipped a major update to its AI coding
  assistant — it can now handle multi-file changes across a whole repo, something it
  couldn't do before." One or two sentences, the change and why it matters, done.
- Give the ONE or TWO details that matter, not all ten. No hype words (exciting,
  fascinating, groundbreaking, revolutionary, game-changer).

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
- Crisp and factual. Lead every story with the concrete fact: company, number, what shipped.
- Clear and natural to hear, but not chatty — no filler, no warm-up phrases, no personal
  asides. Think a sharp newsreader, not a podcaster riffing.
- Contractions are fine for natural flow. Keep sentences short and declarative.
- Do NOT add "why this matters" sermons, advice, or vague attributions ("reports say").
- Ban these words: exciting, fascinating, groundbreaking, revolutionary, game-changer,
  buckle up, let's dive in, stay tuned, it's worth noting, interestingly, notably.

TRANSITIONS — GENERATE THEM FROM THE STORIES, NEVER FROM A STOCK PHRASE:
Every story after the first opens with a transition so the listener hears a new
subject start. The problem to avoid: sounding like the same template every day.
So DERIVE each transition from the actual stories — do not reach for a generic
catch-all like "Now in tech news" or "In other news." Use this priority:

1. IF the new story connects to the previous one (same company, same theme, a
   contrast, a follow-on), transition using that real link:
     "Google had a second announcement today —"
     "Staying with AI —"
     "Apple's in the headlines for a very different reason —"
     "That's not the only chipmaker making moves —"
   These are best: they're never repetitive because they come from the specific stories.

2. IF there's no natural link, use a SHORT category cue tied to this story's topic:
     "On the hardware side —" / "In security —" / "Turning to the legal front —"
     "Over in AI —" / "In startup news —"
   Vary these; do not use the same category cue twice in one episode.

HARD RULES:
- NEVER use these stock phrases: "Now in tech news", "In other news", "In tech news",
  "Elsewhere", "Moving on", "Next up". They are banned — they're the repetitive filler
  we're eliminating.
- Do not reuse ANY transition twice within the same episode.
- Keep each transition short — a few words, not a sentence of throat-clearing.
- The FINAL story must start with "Finally," so the listener knows it's the last one.
- The opener line and the first story get NO transition — the opener leads straight
  into story one.

FORMAT:
- Spoken prose only. No markdown, bullets, asterisks, or headers.
- Don't name the news outlets. Don't start sentences with "today" or "here's".
- OPENER: start with exactly this line, filling in the real date:
  "It's [Month Day]. This is your Daily Dump of tech news."
- CLOSER: end with exactly this line: "And that's all for today. Tune in tomorrow for another Daily Dump."
- BETWEEN STORIES: put the marker [[PAUSE]] on its own line between each story
  (after you finish one story, before you start the next). This signals a beat of
  silence so the listener hears a clear break between subjects. Do NOT put a pause
  after the opener or before the closer — only between the story bodies.

LENGTH: The episode should run about 5 minutes — roughly 790 words. Fill that with
BREADTH, not padding: more stories, each tight, rather than a few stretched out. If
you're short on length, ADD ANOTHER STORY rather than lengthening the ones you have.
Only if there genuinely aren't enough worthwhile stories should you give the top ones
slightly more room. Never inflate a story with filler to hit the word count.

Begin now:"""

    full_text = call_gemini(prompt, temperature=0.8, max_tokens=5000)

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
    prompt = f"""This tech news briefing script is a bit short. It's {current} words
but should be about {target_low} words for a 5-minute episode.

Add more FACTUAL substance to the existing stories: real numbers, what a thing
replaces, the prior version, who's affected, concrete details from the reporting.
Keep each addition tight and factual — 1-2 dense sentences, not padding.
STRICT RULES:
- NO advice or recommendations ("should patch", "developers should", "worth keeping
  an eye on"). The audience are experts; never tell them what to do.
- NO speculation or prediction ("this could", "this might", "this suggests", "expect").
- NO opinion or editorializing. Facts only.
- Do NOT add new stories. Do NOT add technical minutiae like function names, file
  paths, crate names, or code symbols (read aloud by TTS — they sound broken).
- Keep the crisp, factual, non-chatty newsreader voice. Spoken prose, no markdown.

Return ONLY the expanded script, nothing else.

SCRIPT TO EXPAND:
{short_script}"""

    try:
        expanded = call_gemini(prompt, temperature=0.8, max_tokens=5000)
        # Keep whichever is longer, just in case
        if len(expanded.split()) > len(short_script.split()):
            return expanded
    except Exception as e:
        print(f"  Expansion failed: {e}")
    return short_script


def tighten_script(long_script: str, target_high: int = 820) -> str:
    """
    Ask Gemini to trim an over-long script back toward the target while keeping
    the same voice, all the stories, the opener/closer, and the [[PAUSE]] markers.
    Returns the tightened script (or the original if the call fails).
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return long_script

    current = _script_wordcount(long_script)
    prompt = f"""This tech news podcast script is too long. It's {current} words
but should be about {target_high} words for a 5-minute episode.

Tighten it by cutting filler, redundant explanation, and any sentence that just
restates something obvious or defines a common term. Keep ALL the same stories.
Keep the exact opener and closer lines. Keep the [[PAUSE]] markers between stories.
Keep the transition cues that start each story. Do NOT drop a whole story. Just make
each write-up leaner. Keep the same conversational engineer-to-engineer voice.
Spoken prose only, no markdown.

Return ONLY the tightened script, nothing else.

SCRIPT TO TIGHTEN:
{long_script}"""

    try:
        tightened = call_gemini(prompt, temperature=0.7, max_tokens=5000)
        # Only accept if it actually got shorter but didn't collapse
        if 400 < _script_wordcount(tightened) < current:
            return tightened
    except Exception as e:
        print(f"  Tighten failed: {e}")
    return long_script


def _script_wordcount(s: str) -> int:
    """Word count excluding [[PAUSE]] markers."""
    import re
    return len(re.sub(r"\[\[\s*PAUSE\s*\]\]", " ", s, flags=re.IGNORECASE).split())


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
        # Matches "And that's all for today. Tune in tomorrow for another Daily Dump."
        closer = None
        closer_pat = re.compile(
            r"(and that'?s all for today\.?\s*tune in tomorrow for another daily dump[.!]?)\s*$",
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

        # Load a short pre-made silent MP3 for the gap between stories. It's a
        # single small file bundled in the repo (docs/silence.mp3, ~0.8s). Using a
        # fixed file keeps the gap length exact and predictable — no synthesis
        # surprises that could balloon the runtime. Missing file → no gap.
        gap = b""
        for cand in (os.path.join(FEED_DIR, "silence.mp3"), "silence.mp3"):
            if os.path.exists(cand):
                try:
                    with open(cand, "rb") as sf:
                        gap = sf.read()
                    break
                except Exception:
                    gap = b""

        async def _run() -> bytes:
            out = bytearray()
            for i, seg in enumerate(segments):
                out.extend(await _render(seg, RATE, f"segment {i+1}"))
                is_last_body = (i == len(segments) - 1)
                if not is_last_body and gap:
                    out.extend(gap)
            if closer:
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
    memory = purge_old_memory(memory, days=45)
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
    print(f"  Script: {word_count} words (~{word_count * 60 // 165}s at pace)")

    # Always aim for ~5 minutes. At +12% speed that's ~790 words. Expand if short.
    if word_count < 740:
        print(f"  Under 5-min target — asking Gemini to expand...")
        script = expand_script(script, target_low=790)
        word_count = _wc(script)
        print(f"  After expansion: {word_count} words")

    # If WAY over (would run long), ask Gemini to tighten back toward target.
    # ~950 words ≈ 5:45+ at +12%, which is too long for a "5-minute" episode.
    if word_count > 950:
        print(f"  Over 5-min target ({word_count}w) — asking Gemini to tighten...")
        script = tighten_script(script, target_high=820)
        word_count = _wc(script)
        print(f"  After tighten: {word_count} words")

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
