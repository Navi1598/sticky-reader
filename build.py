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
    r"this article is republished|disclosure statement|stay up to date|"
    r"get the conversation|sign up for)",
    re.I,
)
# Photo captions / credits that sneak into extracted text
CREDIT = re.compile(
    r"(getty images|ap photo|reuters|shutterstock|afp via|via wikimedia|"
    r"cc by|cc-by|photo by|image credit|/flickr|istock|nasa/jpl)",
    re.I,
)


def clean_paragraphs(text: str) -> list[str]:
    paras = []
    for p in re.split(r"\n+", text):
        p = p.strip()
        if len(p) < 2 or CLUTTER.match(p):
            continue
        if CREDIT.search(p) and len(p) < 140:  # short line with a photo credit = caption
            continue
        paras.append(p)
    return paras


# ---------------------------------------------------------------- pagination
# Pagination works in "lines" of body text and must match PAGE_CSS below:
# body text 19px Georgia, 1.36 line height, 440px wide -> about 27 lines, 54 chars/line.
PARA_GAP = 0.35  # space between paragraphs, in lines


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


def header_lines(title: str, byline: str, cpl: int) -> float:
    # headline: 25px bold (~0.62x chars per line), 1.18 line height ~= 1.14 body lines
    # byline: 13px sans (~1.45x chars per line), ~0.68 body lines each
    return (n_lines(title, int(cpl * 0.62)) * 1.14 + 0.5
            + n_lines(byline, int(cpl * 1.45)) * 0.68 + 0.6)


def paginate(title, byline, paras, lpp, cpl):
    """Return list of pages; each page = {"paras": [...], "left": lines_left}."""
    pages = [{"paras": [], "left": lpp - header_lines(title, byline, cpl)}]
    queue = list(paras)
    while queue:
        para = queue.pop(0)
        page = pages[-1]
        need = n_lines(para, cpl)
        if need <= page["left"]:
            page["paras"].append(para)
            page["left"] -= need + PARA_GAP
            continue
        if page["left"] >= 2:  # fill the rest of this page, carry the remainder over
            head, tail = take_lines(para, int(page["left"]), cpl)
            if head:
                page["paras"].append(head)
                page["left"] = 0
                para = tail
        pages.append({"paras": [], "left": lpp})
        if para:
            queue.insert(0, para)
    pages = [p for p in pages if p["paras"]]
    # The last page also carries a ~2-line "end of article" note.
    if pages and pages[-1]["left"] < 2.2 and len(pages[-1]["paras"]) > 1:
        moved = pages[-1]["paras"].pop()
        pages.append({"paras": [moved], "left": lpp - n_lines(moved, cpl)})
    return pages


def truncate(pages, n, lpp, cpl):
    """Keep the first n pages and make room for the 'continues online' note."""
    pages = pages[:n]
    last = pages[-1]
    if last["left"] < 2.2 and len(last["paras"]) > 1:
        last["paras"].pop()
    elif last["left"] < 2.2:
        head, _ = take_lines(last["paras"][0], max(1, lpp - 3), cpl)
        last["paras"][0] = head + " …"
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


def short_source(source: str) -> str:
    # "Science + Tech – The Conversation" -> "The Conversation"
    parts = re.split(r"\s+[–—-]\s+", source)
    return parts[-1].strip() if parts else source


def build_articles(topic, cfg, shown):
    """Paginate up to 8 of the newest unseen articles for a topic."""
    lpp, cpl = cfg["lines_per_page"], cfg["chars_per_line"]
    arts = []
    for c in candidates_for(topic, cfg.get("max_age_days", 4), shown)[:8]:
        e = c["entry"]
        text = entry_feed_content(e)
        if len(text) < 1200:  # feed only has a teaser -> fetch the page
            text = fetch_article_text(c["link"]) or text
        paras = clean_paragraphs(text)
        if sum(len(p) for p in paras) < 600:  # too thin to be worth reading
            continue
        title = strip_tags(e.get("title", "Untitled"))
        author = strip_tags(e.get("author", "") or "")
        date = dt.datetime.utcfromtimestamp(c["ts"]).strftime("%b %d") if c["ts"] else ""
        byline = " · ".join(x for x in (author, short_source(c["source"]), date) if x)
        arts.append({
            "topic": topic["name"], "title": title, "byline": byline, "link": c["link"],
            "source": short_source(c["source"]), "pages": paginate(title, byline, paras, lpp, cpl),
        })
    return arts


def plan(topics, cfg, shown):
    """Share `total_pages` between topics. Each topic gets a fair share; an
    article that's shorter than its share gives the spare pages to later topics."""
    total = cfg["total_pages"]
    lpp, cpl = cfg["lines_per_page"], cfg["chars_per_line"]
    chosen, remaining = [], total
    pools = []
    for t in topics:
        print(f"* {t['name']}")
        pools.append(build_articles(t, cfg, shown))
    used_links = set()
    for i, arts in enumerate(pools):
        topics_left = len(topics) - i
        arts = [a for a in arts if a["link"] not in used_links]  # no repeats across topics
        if not arts or remaining <= 0:
            print(f"    -> nothing new for {topics[i]['name']}" if not arts else "    -> no pages left")
            continue
        # guarantee each later topic with an article at least 2 pages
        later = sum(1 for p in pools[i + 1:] if any(a["link"] not in used_links for a in p))
        budget = max(1, remaining - 2 * later) if later else remaining
        fair = max(2, remaining // topics_left)
        fits = [a for a in arts if len(a["pages"]) <= budget]
        if fits:
            # newest article that fits in the fair share; else newest that fits at all
            near = [a for a in fits if len(a["pages"]) <= fair]
            art = near[0] if near else min(fits, key=lambda a: len(a["pages"]))
            art["truncated"] = False
        else:
            art = min(arts, key=lambda a: len(a["pages"]))  # shortest, cut to budget
            art["pages"] = truncate(art["pages"], budget, lpp, cpl)
            art["truncated"] = True
        remaining -= len(art["pages"])
        used_links.add(art["link"])
        chosen.append(art)
        print(f"    -> {art['title']} ({len(art['pages'])} pages"
              f"{', truncated' if art['truncated'] else ''})")
    return chosen


# ---------------------------------------------------------------- rendering
PAGE_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{width:480px;height:800px;overflow:hidden;background:#fff;color:#000}
body{font-family:Georgia,'Times New Roman',serif;padding:18px 20px 0 20px;position:relative}
.top{font-family:Arial,Helvetica,sans-serif;font-size:14px;font-weight:bold;
     text-transform:uppercase;letter-spacing:1px;border-bottom:2px solid #000;
     padding-bottom:5px;margin-bottom:12px;display:flex;justify-content:space-between}
h1{font-size:25px;line-height:1.18;margin-bottom:7px}
.by{font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:1.35;margin-bottom:10px}
p{font-size:19px;line-height:1.36;margin-bottom:9px;text-align:left;hyphens:auto}
h2{font-family:Arial,Helvetica,sans-serif;font-size:18px;line-height:1.36;margin:2px 0 7px 0}
.end{font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:1.3;
     border-top:1px solid #000;padding-top:5px;margin-top:4px}
.foot{position:absolute;left:20px;right:20px;bottom:10px;font-family:Arial,Helvetica,sans-serif;
      font-size:13px;display:flex;justify-content:space-between;border-top:1px solid #000;padding-top:5px}
.empty{display:flex;flex-direction:column;justify-content:center;height:640px;text-align:center}
.empty h1{font-size:28px}
.empty p{font-size:18px}
"""


def page_html(top_left, top_right, body, global_no, total, built):
    esc = html.escape
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=480,height=800,initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta name="robots" content="noindex,nofollow">
<title>Sticky Reader {global_no:02d}</title>
<style>{PAGE_CSS}</style></head>
<body>
<div class="top"><span>{esc(top_left)}</span><span>{esc(top_right)}</span></div>
{body}
<div class="foot"><span>{esc(built)}</span><span>{global_no}/{total}</span></div>
</body></html>
"""


def article_page_body(art, i):
    esc = html.escape
    out = []
    if i == 0:
        out.append(f"<h1>{esc(art['title'])}</h1>")
        out.append(f'<div class="by">{esc(art["byline"])}</div>')
    for p in art["pages"][i]["paras"]:
        is_head = len(p) < 70 and not re.search(r"[.!?:;,\u201d\"\u2026)]$", p)
        out.append(f"<h2>{esc(p)}</h2>" if is_head else f"<p>{esc(p)}</p>")
    if i == len(art["pages"]) - 1:
        m = re.search(r"(\d{4,})/?$", art["link"])
        ident = m.group(1) if m else art["link"].rstrip("/").split("/")[-1][-20:]
        what = "Continues online" if art["truncated"] else "End of article"
        out.append(f'<div class="end">{what} · republished from {esc(art["source"])} '
                   f'(CC BY-ND) · ref {esc(ident)}</div>')
    return "".join(out)


def render_index(pages_meta, built):
    rows = "".join(
        f'<li><a href="{m["file"]}">{m["file"]}</a> - {html.escape(m["label"])}</li>' for m in pages_meta
    )
    frames = "".join(
        f'<iframe src="{m["file"]}" width="480" height="800" style="border:1px solid #999;margin:6px"></iframe>'
        for m in pages_meta
    )
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="robots" content="noindex">
<title>Sticky Reader</title></head>
<body style="font-family:Arial;padding:16px">
<h2>Sticky Reader - built {html.escape(built)}</h2>
<ol>{rows}</ol>
<h3>Preview</h3><div style="display:flex;flex-wrap:wrap">{frames}</div>
</body></html>"""


# ---------------------------------------------------------------- main
def main() -> int:
    cfg = load_config()
    cfg.setdefault("lines_per_page", 26)
    cfg.setdefault("chars_per_line", 53)
    cfg.setdefault("total_pages", cfg.get("pages_per_topic", 4) * len(cfg["topics"]))
    state = load_state()
    shown = set(state.get("shown", []))
    total = cfg["total_pages"]
    built = dt.datetime.now(dt.timezone.utc).astimezone().strftime("%a %b %d")

    DOCS.mkdir(exist_ok=True)
    (DOCS / ".nojekyll").write_text("")

    articles = plan(cfg["topics"], cfg, shown)
    pages_meta, n = [], 0
    for art_no, art in enumerate(articles, 1):
        shown.add(art["link"])
        state["shown"].append(art["link"])
        for i in range(len(art["pages"])):
            n += 1
            right = f"{i + 1}/{len(art['pages'])}"
            body = article_page_body(art, i)
            (DOCS / f"p{n:02d}.html").write_text(
                page_html(art["topic"], right, body, n, total, built), encoding="utf-8")
            label = f"{art['topic']} {right}" + (f": {art['title']}" if i == 0 else "")
            pages_meta.append({"file": f"p{n:02d}.html", "label": label})

    # Fill the rest of the pool with "done" pages so old content never lingers.
    first_blank = n + 1
    while n < total:
        n += 1
        msg = ("<div class=\"empty\"><h1>That's all for today</h1>"
               "<p>New articles arrive tomorrow morning.</p></div>")
        if n == first_blank and not articles:
            msg = ("<div class=\"empty\"><h1>No new articles today</h1>"
                   "<p>Check back tomorrow.</p></div>")
        (DOCS / f"p{n:02d}.html").write_text(
            page_html("Sticky Reader", "", msg, n, total, built), encoding="utf-8")
        pages_meta.append({"file": f"p{n:02d}.html", "label": "(empty)"})

    for old in DOCS.glob("p*.html"):  # remove pages beyond the pool size
        m = re.fullmatch(r"p(\d+)\.html", old.name)
        if m and int(m.group(1)) > total:
            old.unlink()

    (DOCS / "index.html").write_text(render_index(pages_meta, built), encoding="utf-8")
    save_state(state)
    print(f"Wrote {total} pages ({first_blank - 1} with articles) to {DOCS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
