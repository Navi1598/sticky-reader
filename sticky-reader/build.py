#!/usr/bin/env python3
"""
Sticky Reader - daily article pages for the Seeed reTerminal Sticky (SenseCraft HMI).

Each run:
  1. Reads config.yaml (topics, feeds, keywords).
  2. For each topic, picks the newest article not shown before (preferring one
     that fits in the page budget), and extracts its full text.
  3. Splits the text into e-ink pages (480x800 portrait) and writes them to
     docs/p01.html, docs/p02.html, ... in reading order.
  4. Records shown articles in docs/state.json so you don't get repeats.

GitHub Pages serves the docs/ folder; each SenseCraft design points at one page URL.
"""

from __future__ import annotations

import calendar
import datetime as dt
import html
import json
import re
import sys
import time
from pathlib import Path

import feedparser
import requests
import yaml

try:
    import trafilatura
except ImportError:  # extraction falls back to the feed's own content
    trafilatura = None

ROOT = Path(__file__).parent
DOCS = ROOT / "docs"
STATE_FILE = DOCS / "state.json"
USER_AGENT = "StickyReader/1.0 (personal e-ink reader; +https://github.com)"
TIMEOUT = 20


# ---------------------------------------------------------------- helpers
def load_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"shown": []}


def save_state(state: dict) -> None:
    state["shown"] = state["shown"][-500:]  # keep the file small
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def strip_tags(text: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</h\d>|</li>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def entry_time(entry) -> float:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return calendar.timegm(t)
    return 0.0


def entry_summary(entry) -> str:
    return strip_tags(entry.get("summary", "") or "")


def entry_feed_content(entry) -> str:
    """Full text if the feed itself carries it (The Conversation does)."""
    best = ""
    for c in entry.get("content", []) or []:
        txt = strip_tags(c.get("value", ""))
        if len(txt) > len(best):
            best = txt
    return best


def fetch_article_text(url: str) -> str:
    if trafilatura is None:
        return ""
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        r.raise_for_status()
        text = trafilatura.extract(
            r.text, include_comments=False, include_tables=False, favor_precision=True
        )
        return text or ""
    except Exception as exc:  # network errors shouldn't kill the whole run
        print(f"    ! could not fetch {url}: {exc}", file=sys.stderr)
        return ""


CLUTTER = re.compile(
    r"^(advertisement|subscribe|sign up|read more|related:|share this|"
    r"ftc: we use|this article is republished|disclosure statement|"
    r"you're reading electrek|stay up to date)",
    re.I,
)


def clean_paragraphs(text: str) -> list[str]:
    paras = []
    for p in re.split(r"\n+", text):
        p = p.strip()
        if len(p) < 2 or CLUTTER.match(p):
            continue
        paras.append(p)
    return paras


# ---------------------------------------------------------------- pagination
# Pagination works in "lines" of body text. Defaults match the CSS below
# (20px Georgia, 1.42 line height, 432px text width) with a small safety margin.
PARA_GAP = 0.45  # space between paragraphs, in lines


def n_lines(text: str, cpl: int) -> int:
    """Estimate wrapped line count by word-wrapping at `cpl` characters."""
    lines, cur = 1, 0
    for word in text.split():
        add = len(word) + (1 if cur else 0)
        if cur + add > cpl and cur:
            lines += 1
            cur = len(word)
        else:
            cur += add
    return lines


def take_lines(text: str, max_lines: int, cpl: int) -> tuple[str, str]:
    """Split text so the head fits in max_lines; prefer sentence boundaries."""
    sentences = re.split(r"(?<=[.!?”\"])\s+", text)
    head = ""
    for i, s in enumerate(sentences):
        trial = f"{head} {s}".strip()
        if n_lines(trial, cpl) > max_lines:
            if head:
                return head, " ".join(sentences[i:])
            break  # first sentence alone is too long: cut on words
        head = trial
    else:
        return head, ""
    words, head = text.split(), ""
    for i, w in enumerate(words):
        trial = f"{head} {w}".strip()
        if n_lines(trial, cpl) > max_lines:
            return head, " ".join(words[i:])
        head = trial
    return head, ""


def paginate(title: str, paras: list[str], lines_per_page: int, cpl: int) -> list[list[str]]:
    title_lines = n_lines(title, int(cpl * 0.72)) * 1.15 + 1.6  # big headline + byline
    pages: list[list[str]] = [[]]
    left = lines_per_page - title_lines
    queue = list(paras)
    while queue:
        para = queue.pop(0)
        need = n_lines(para, cpl)
        if need <= left:
            pages[-1].append(para)
            left -= need + PARA_GAP
            continue
        if left >= 3:  # fill the rest of this page, carry the remainder over
            head, tail = take_lines(para, int(left), cpl)
            if head:
                pages[-1].append(head)
                para = tail
        pages.append([])
        left = lines_per_page
        if para:
            queue.insert(0, para)
    pages = [p for p in pages if p]
    # The last page also carries a ~3-line credit note; make room for it.
    if pages and left < 3.5 and len(pages[-1]) > 1:
        pages.append([pages[-1].pop()])
    return pages


# ---------------------------------------------------------------- selection
def candidates_for(topic: dict, max_age_days: int, shown: set[str]) -> list[dict]:
    keywords = [k.lower() for k in topic.get("keywords", []) or []]
    cutoff = time.time() - max_age_days * 86400
    out, seen_links = [], set()
    for feed_url in topic["feeds"]:
        try:
            resp = requests.get(feed_url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
            feed = feedparser.parse(resp.content)
        except Exception as exc:
            print(f"    ! feed failed {feed_url}: {exc}", file=sys.stderr)
            continue
        source = feed.feed.get("title", feed_url)
        for e in feed.entries:
            link = e.get("link", "")
            if not link or link in shown or link in seen_links:
                continue
            ts = entry_time(e)
            if ts and ts < cutoff:
                continue
            if keywords:
                hay = f"{e.get('title', '')} {entry_summary(e)}".lower()
                if not any(k in hay for k in keywords):
                    continue
            seen_links.add(link)
            out.append({"entry": e, "link": link, "ts": ts, "source": source})
    out.sort(key=lambda c: c["ts"], reverse=True)
    return out


def pick_article(topic: dict, cfg: dict, shown: set[str]) -> dict | None:
    lpp = cfg.get("lines_per_page", 23)
    cpl = cfg.get("chars_per_line", 47)
    max_pages = cfg["pages_per_topic"]
    cands = candidates_for(topic, cfg.get("max_age_days", 4), shown)
    fallback = None
    for c in cands[:8]:  # look at the 8 newest at most
        e = c["entry"]
        text = entry_feed_content(e)
        if len(text) < 1200:  # feed only has a teaser -> fetch the page
            text = fetch_article_text(c["link"]) or text
        paras = clean_paragraphs(text)
        if sum(len(p) for p in paras) < 600:  # too thin to be worth reading
            continue
        title = strip_tags(e.get("title", "Untitled"))
        pages = paginate(title, paras, lpp, cpl)
        art = {
            "title": title,
            "author": strip_tags(e.get("author", "") or ""),
            "link": c["link"],
            "source": c["source"],
            "date": dt.datetime.utcfromtimestamp(c["ts"]).strftime("%b %d") if c["ts"] else "",
            "pages": pages,
        }
        if len(pages) <= max_pages:
            return art  # newest article that fits completely
        if fallback is None:
            fallback = art
    return fallback  # nothing fits: newest one, truncated


# ---------------------------------------------------------------- rendering
PAGE_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{width:480px;height:800px;overflow:hidden;background:#fff;color:#000}
body{font-family:Georgia,'Times New Roman',serif;padding:22px 24px 0 24px;position:relative}
.top{font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:bold;
     text-transform:uppercase;letter-spacing:1px;border-bottom:2px solid #000;
     padding-bottom:6px;margin-bottom:14px;display:flex;justify-content:space-between}
h1{font-size:27px;line-height:1.2;margin-bottom:8px}
.by{font-family:Arial,Helvetica,sans-serif;font-size:14px;margin-bottom:14px}
p{font-size:20px;line-height:1.42;margin-bottom:12px;text-align:left;hyphens:auto}
.end{font-family:Arial,Helvetica,sans-serif;font-size:15px;border-top:1px solid #000;
     padding-top:8px;margin-top:6px}
.foot{position:absolute;left:24px;right:24px;bottom:14px;font-family:Arial,Helvetica,sans-serif;
      font-size:14px;display:flex;justify-content:space-between;border-top:1px solid #000;padding-top:6px}
.empty{display:flex;flex-direction:column;justify-content:center;height:640px;text-align:center}
.empty h1{font-size:30px}
.empty p{font-size:19px}
"""


def render_page(topic_name, art, page_idx, n_pages, global_no, total, built, truncated):
    esc = html.escape
    body = []
    if art is None:
        body.append(
            f'<div class="empty"><h1>No new {esc(topic_name)} article</h1>'
            "<p>Nothing new today. Press down to continue.</p></div>"
        )
    elif page_idx >= n_pages:
        body.append(
            f'<div class="empty"><h1>End of article</h1>'
            f"<p>Press down for the next topic.</p></div>"
        )
    else:
        if page_idx == 0:
            body.append(f"<h1>{esc(art['title'])}</h1>")
            meta = " · ".join(x for x in (art.get("author"), art["source"], art["date"]) if x)
            body.append(f'<div class="by">{esc(meta)}</div>')
        for para in art["pages"][page_idx]:
            body.append(f"<p>{esc(para)}</p>")
        if page_idx == n_pages - 1:
            note = "Continues online at the original link." if truncated else "End of article."
            body.append(
                f'<div class="end">{note} Republished from {esc(art["source"])}: '
                f'{esc(art["link"])}</div>'
            )

    shown_pages = max(n_pages, 1) if art else 1
    page_label = f"{min(page_idx + 1, shown_pages)}/{shown_pages}" if art and page_idx < n_pages else ""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=480,height=800,initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta name="robots" content="noindex,nofollow">
<title>Sticky Reader {global_no:02d}</title>
<style>{PAGE_CSS}</style></head>
<body>
<div class="top"><span>{esc(topic_name)}</span><span>{page_label}</span></div>
{''.join(body)}
<div class="foot"><span>{esc(built)}</span><span>{global_no}/{total}</span></div>
</body></html>
"""


def render_index(pages_meta, built):
    rows = "".join(
        f'<li><a href="{m["file"]}">{m["file"]}</a> - {html.escape(m["label"])}</li>' for m in pages_meta
    )
    frames = "".join(
        f'<iframe src="{m["file"]}" width="480" height="800" '
        f'style="border:1px solid #999;margin:6px;transform-origin:0 0"></iframe>'
        for m in pages_meta
    )
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Sticky Reader</title></head>
<body style="font-family:Arial;padding:16px">
<h2>Sticky Reader - built {html.escape(built)}</h2>
<ol>{rows}</ol>
<h3>Preview</h3><div style="display:flex;flex-wrap:wrap">{frames}</div>
</body></html>"""


# ---------------------------------------------------------------- main
def main() -> int:
    cfg = load_config()
    state = load_state()
    shown = set(state.get("shown", []))
    per_topic = cfg["pages_per_topic"]
    topics = cfg["topics"]
    total = per_topic * len(topics)
    built = dt.datetime.now(dt.timezone.utc).astimezone().strftime("%a %b %d")

    DOCS.mkdir(exist_ok=True)
    (DOCS / ".nojekyll").write_text("")
    pages_meta, n = [], 0

    for topic in topics:
        print(f"* {topic['name']}")
        art = pick_article(topic, cfg, shown)
        if art:
            print(f"    -> {art['title']} ({len(art['pages'])} pages)")
            shown.add(art["link"])
            state["shown"].append(art["link"])
        else:
            print("    -> nothing new")
        truncated = bool(art) and len(art["pages"]) > per_topic
        n_pages = min(len(art["pages"]), per_topic) if art else 0
        for i in range(per_topic):
            n += 1
            fname = f"p{n:02d}.html"
            (DOCS / fname).write_text(
                render_page(topic["name"], art, i, n_pages, n, total, built, truncated),
                encoding="utf-8",
            )
            label = f"{topic['name']} page {i + 1}"
            if art and i == 0:
                label += f": {art['title']}"
            pages_meta.append({"file": fname, "label": label})

    # remove stale pages if you reduced topics/pages in config
    for old in DOCS.glob("p*.html"):
        m = re.fullmatch(r"p(\d+)\.html", old.name)
        if m and int(m.group(1)) > total:
            old.unlink()

    (DOCS / "index.html").write_text(render_index(pages_meta, built), encoding="utf-8")
    save_state(state)
    print(f"Wrote {total} pages to {DOCS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
