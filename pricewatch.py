#!/usr/bin/env python3
"""SugarSisters competitor price watch, all in one file.
Run:  python pricewatch.py            (daily run)
      python pricewatch.py --no-email
"""
from __future__ import annotations

# ==================== scrapers ====================

import gzip
import json
import logging
import re
import time
import urllib.robotparser
from html import unescape
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("watch")


class Fetcher:
    """Polite HTTP client: one session, robots.txt checked, a pause between requests."""

    def __init__(self, user_agent: str, delay: float = 1.5, session=None):
        self.ua = user_agent
        self.delay = delay
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": user_agent, "Accept-Language": "en-IE,en;q=0.8"})
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._last: dict[str, float] = {}

    def allowed(self, url: str) -> bool:
        host = urlparse(url).scheme + "://" + urlparse(url).netloc
        if host not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.s.get(host + "/robots.txt", timeout=20)
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                else:
                    rp = None  # no robots file: everything allowed
            except requests.RequestException:
                rp = None
            self._robots[host] = rp
        rp = self._robots[host]
        return True if rp is None else rp.can_fetch(self.ua, url)

    def get(self, url: str, **kw):
        if not self.allowed(url):
            raise PermissionError(f"robots.txt disallows {url}")
        host = urlparse(url).netloc
        wait = self.delay - (time.time() - self._last.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        for attempt in range(3):
            try:
                r = self.s.get(url, timeout=30, **kw)
                self._last[host] = time.time()
                if r.status_code in (429, 503):
                    time.sleep(10 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"gave up on {url}")


def _money(v) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d.,]", "", str(v))
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------- Shopify
def read_shopify(site: dict, f: Fetcher) -> list[dict]:
    base = site["url"].rstrip("/")
    items, page = [], 1
    while True:
        r = f.get(f"{base}/products.json", params={"limit": 250, "page": page})
        products = r.json().get("products", [])
        if not products:
            break
        for p in products:
            for v in p.get("variants", []):
                vt = v.get("title") or ""
                items.append({
                    "site": site["name"],
                    "title": unescape(p.get("title", "")).strip(),
                    "variant": "" if vt == "Default Title" else vt,
                    "sku": v.get("sku") or "",
                    "barcode": v.get("barcode") or "",
                    "price": _money(v.get("price")),
                    "available": v.get("available"),
                    "url": f"{base}/products/{p.get('handle')}",
                })
        page += 1
        if page > 200:
            break
    return items


# ---------------------------------------------------------------- WooCommerce
def read_woocommerce(site: dict, f: Fetcher) -> list[dict]:
    base = site["url"].rstrip("/")
    items, page = [], 1
    while True:
        r = f.get(f"{base}/wp-json/wc/store/v1/products", params={"per_page": 100, "page": page})
        products = r.json()
        if not products:
            break
        for p in products:
            pr = p.get("prices") or {}
            minor = int(pr.get("currency_minor_unit", 2) or 2)
            raw = pr.get("price")
            price = (int(raw) / 10 ** minor) if raw not in (None, "") else None
            items.append({
                "site": site["name"],
                "title": BeautifulSoup(unescape(p.get("name", "")), "html.parser").get_text().strip(),
                "variant": "",
                "sku": p.get("sku") or "",
                "barcode": "",
                "price": price,
                "available": p.get("is_in_stock"),
                "url": p.get("permalink", ""),
            })
        total_pages = int(r.headers.get("X-WP-TotalPages", page))
        page += 1
        if page > total_pages or page > 200:
            break
    return items


# ---------------------------------------------------------------- Sitemap + product pages
def _sitemap_urls(url: str, f: Fetcher, depth=0) -> list[str]:
    r = f.get(url)
    body = r.content
    if url.endswith(".gz") or body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    soup = BeautifulSoup(body, "xml")
    out = []
    if soup.find("sitemapindex") and depth < 3:
        for loc in soup.select("sitemap > loc"):
            out += _sitemap_urls(loc.get_text().strip(), f, depth + 1)
    else:
        out = [loc.get_text().strip() for loc in soup.select("url > loc")]
    return out


def parse_product_page(html: str, url: str, site_name: str) -> dict | None:
    """Pull name, price and stock from a product page. Tries, in order:
    schema.org JSON-LD, schema.org microdata, Open Graph / product meta tags."""
    soup = BeautifulSoup(html, "html.parser")
    name = price = avail = sku = barcode = None

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, dict) and "@graph" in d:
                stack += d["@graph"]
                continue
            if not isinstance(d, dict):
                continue
            t = d.get("@type")
            if t == "Product" or (isinstance(t, list) and "Product" in t):
                name = name or d.get("name")
                sku = sku or d.get("sku")
                barcode = barcode or d.get("gtin13") or d.get("gtin") or d.get("gtin8")
                offers = d.get("offers")
                if isinstance(offers, list):
                    offers = offers[0] if offers else None
                if isinstance(offers, dict):
                    price = price if price is not None else _money(offers.get("price") or offers.get("lowPrice"))
                    a = str(offers.get("availability", ""))
                    if a:
                        avail = "InStock" in a or "PreOrder" in a
    if price is None:
        el = soup.find(attrs={"itemprop": "price"})
        if el:
            price = _money(el.get("content") or el.get_text())
        el = soup.find(attrs={"itemprop": "availability"})
        if el and avail is None:
            a = el.get("href") or el.get("content") or ""
            avail = "InStock" in a if a else None
        el = soup.find(attrs={"itemprop": "name"})
        if el and not name:
            name = el.get("content") or el.get_text()
    if price is None:
        for prop in ("product:price:amount", "og:price:amount"):
            el = soup.find("meta", attrs={"property": prop})
            if el:
                price = _money(el.get("content"))
                break
    if not name:
        el = soup.find("meta", attrs={"property": "og:title"}) or soup.find("h1")
        if el:
            name = el.get("content") if el.name == "meta" else el.get_text()
    if avail is None:
        text = soup.get_text(" ", strip=True).lower()
        if "out of stock" in text or "sold out" in text:
            avail = False
        elif "in stock" in text:
            avail = True
    if not name or price is None:
        return None
    return {
        "site": site_name,
        "title": re.sub(r"\s+", " ", unescape(str(name))).strip(),
        "variant": "",
        "sku": str(sku or ""),
        "barcode": str(barcode or ""),
        "price": price,
        "available": avail,
        "url": url,
    }


SITEMAP_GUESSES = ["/sitemap.xml", "/sitemap_index.xml", "/index.php?route=extension/feed/google_sitemap",
                   "/index.php?route=feed/google_sitemap", "/index.php?route=information/sitemap"]


def _find_product_urls(site: dict, f: Fetcher) -> list[str]:
    base = site["url"].rstrip("/")
    tried = [site.get("sitemap")] if site.get("sitemap") else []
    try:  # sitemaps listed in robots.txt
        r = f.s.get(base + "/robots.txt", timeout=20)
        tried += [l.split(":", 1)[1].strip() for l in r.text.splitlines() if l.lower().startswith("sitemap:")]
    except Exception:
        pass
    tried += [base + g for g in SITEMAP_GUESSES]
    want = site.get("product_url_contains")
    for sm in dict.fromkeys(tried):
        try:
            urls = _sitemap_urls(sm, f)
        except Exception as e:
            log.info("%s: sitemap %s failed (%s)", site["name"], sm, e)
            continue
        if not urls:
            continue
        prod = [u for u in urls if want in u] if want else urls
        log.info("%s: %s gave %d urls, %d look like products", site["name"], sm, len(urls), len(prod))
        return prod or urls
    raise RuntimeError("no usable sitemap found (tried " + ", ".join(t for t in tried if t) + ")")


def read_sitemap(site: dict, f: Fetcher) -> list[dict]:
    urls = _find_product_urls(site, f)
    urls = list(dict.fromkeys(urls))[: int(site.get("max_pages", 1500))]
    items = []
    for i, u in enumerate(urls):
        try:
            r = f.get(u)
        except PermissionError:
            continue
        except Exception as e:  # one bad page shouldn't stop the run
            log.warning("%s: %s", u, e)
            continue
        it = parse_product_page(r.text, u, site["name"])
        if it:
            items.append(it)
        if i and i % 200 == 0:
            log.info("%s: %d/%d pages", site["name"], i, len(urls))
    return items


# ---------------------------------------------------------------- OpenCart (no product feed or sitemap)
def _clean_opencart_url(u: str) -> str:
    q = dict(p.split("=", 1) for p in urlparse(u).query.split("&") if "=" in p)
    pid = q.get("product_id")
    if not pid:
        return u
    return f"{urlparse(u).scheme}://{urlparse(u).netloc}{urlparse(u).path}?route=product/product&product_id={pid}"


def read_opencart(site: dict, f: Fetcher) -> list[dict]:
    """Reads the shop's own 'search everything' listing, page by page, like a customer browsing.
    Each listing page already shows name and price, so no need to open every product.
    Never uses the limit/sort parameters the shop's robots.txt asks robots to avoid."""
    base = site["url"].rstrip("/")
    items, seen = [], set()
    max_pages = int(site.get("max_pages", 400))
    for page in range(1, max_pages + 1):
        url = f"{base}/index.php?route=product/search&search=" + (f"&page={page}" if page > 1 else "")
        r = f.get(url)
        soup = BeautifulSoup(r.text, "html.parser")
        boxes = soup.select(".product-thumb")
        new = 0
        for b in boxes:
            a = b.select_one(".caption h4 a") or b.select_one("h4 a") or b.find("a", href=re.compile("product_id="))
            if not a:
                continue
            link = _clean_opencart_url(a.get("href", ""))
            if link in seen:
                continue
            pe = b.select_one(".price-new") or b.select_one(".price")
            if pe:
                for sub in pe.select(".price-old, .price-tax"):
                    sub.decompose()
            price = _money(pe.get_text(" ", strip=True).split()[0]) if pe and pe.get_text(strip=True) else None
            text = b.get_text(" ", strip=True).lower()
            avail = False if ("out of stock" in text or "sold out" in text) else None
            seen.add(link)
            new += 1
            items.append({
                "site": site["name"],
                "title": re.sub(r"\s+", " ", unescape(a.get_text(" ", strip=True))).strip(),
                "variant": "", "sku": "", "barcode": "",
                "price": price, "available": avail, "url": link,
            })
        if not boxes or not new:
            break
        if page % 50 == 0:
            log.info("%s: %d listing pages, %d products", site["name"], page, len(items))
    if not items:
        raise RuntimeError("product listing pages showed no products")
    return items


READERS = {"shopify": read_shopify, "woocommerce": read_woocommerce, "sitemap": read_sitemap,
           "opencart": read_opencart}


def read_site(site: dict, f: Fetcher) -> list[dict]:
    return READERS[site["type"]](site, f)


# ==================== matching ====================

import csv
import math
import os
import re

from difflib import SequenceMatcher


def ratio(a: str, b: str) -> float:
    return 100 * SequenceMatcher(None, a, b).ratio()


def token_set_ratio(a: str, b: str) -> float:
    """Same idea as fuzzywuzzy's token_set_ratio: compare shared words plus each side's extras."""
    ta, tb = set(a.split()), set(b.split())
    common = " ".join(sorted(ta & tb))
    ra = (common + " " + " ".join(sorted(ta - tb))).strip()
    rb = (common + " " + " ".join(sorted(tb - ta))).strip()
    if not common:
        return ratio(ra, rb)
    return max(ratio(common, ra), ratio(common, rb), ratio(ra, rb))

BRANDS = [
    "callebaut", "belcolade", "renshaw", "culpitt", "funcakes", "sugarflair", "colour mill",
    "pme", "wilton", "squires", "saracino", "satinara", "decora", "dekofee", "cake star",
    "colour splash", "progel", "frost form", "lorann", "crisco", "scrumptious", "rainbow dust",
    "sweet stamp", "sweetstamp", "carat", "favorich", "deco melts", "candy melts", "halo",
]

UNIT = r"(kg|kgs|g|gr|grams?|ml|l|ltr|in|inch|cm|mm|oz)"
STOP = {"the", "and", "with", "for", "of", "a", "belgian", "default", "title", "luxury", "new", "asstd", "assorted",
        "size", "sizes", "single", "bulk"}
SYN = {"drum": "board", "drums": "board", "boards": "board", "cakeboard": "board", "callets": "", "callet": "",
       "chips": "", "buttons": "", "colour": "color", "colouring": "color", "colourant": "color",
       "fondant": "sugarpaste", "icing": "sugarpaste", "paste": "paste", "sugar": "sugar",
       "boxe": "box", "boxes": "box", "cupcakes": "cupcake"}


def norm(s: str) -> str:
    s = s.lower()
    s = s.replace("″", '"').replace("”", '"').replace("''", '"')
    s = re.sub(r'(\d)\s*(?:"|inch(?:es)?\b)', r"\1in ", s)
    s = re.sub(r"(\d)\s*(kgs?|grams?|gr|g|ml|ltr|l|cm|mm|oz)\b", r"\1\2 ", s)
    s = re.sub(r"\bkgs\b", "kg", s)
    s = re.sub(r"(\d+)\s*grams?\b", r"\1g", s)
    s = re.sub(r"\b(pk|pack|packs?|box(?:ed)?|x)\s*(?:of\s*)?(\d+)\b", r"pk\2", s)
    s = re.sub(r"\b(\d+)\s*(?:pk|pack)\b", r"pk\1", s)
    s = re.sub(r"\b(\d+)\s*s\b", r"\1", s)  # 12s / 12 s -> 12 (cupcake boxes)
    s = re.sub(r"[^a-z0-9.%\s]", " ", s)
    words = []
    for w in s.split():
        w = SYN.get(w, w)
        if w in STOP:
            continue
        if len(w) > 4 and w.endswith("es") and not w.endswith("ses"):
            w = w[:-2]
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        words.append(SYN.get(w, w))
    return " ".join(dict.fromkeys(words))


def sizes(n: str) -> set[str]:
    out = set(re.findall(r"\b\d+(?:\.\d+)?(?:kg|g|ml|l|in|cm|mm|oz)\b", n))
    out |= set(re.findall(r"\bpk\d+\b", n))
    # 1kg == 1000g
    conv = set()
    for t in out:
        m = re.match(r"(\d+(?:\.\d+)?)(kg|g)$", t)
        if m:
            g = float(m.group(1)) * (1000 if m.group(2) == "kg" else 1)
            conv.add(f"{g:g}g")
        else:
            conv.add(t)
    return conv


def brand(n: str) -> str | None:
    for b in BRANDS:
        if b in n:
            return b
    return None


def _w(tok: str) -> float:
    if re.fullmatch(r"\d+(?:\.\d+)?(?:kg|g|ml|l|in|cm|mm|oz)|pk\d+", tok):
        return 3.0
    if re.fullmatch(r"[\d.]+%", tok):
        return 0.5
    if any(tok == b or tok in b.split() for b in BRANDS):
        return 2.0
    if tok.isdigit():
        return 1.5
    return 1.0


def _canon(n: str) -> list[str]:
    out = []
    for t in n.split():
        m = re.fullmatch(r"(\d+(?:\.\d+)?)(kg|g)", t)
        out.append(f"{float(m.group(1)) * (1000 if m.group(2) == 'kg' else 1):g}g" if m else t)
    return [t for t in out if t]


def score(ours_norm: str, theirs_norm: str, idf=None) -> float:
    """0-100: how much of each name is covered by the words they share. Sizes and brands count extra, and
    words that are rare in the competitor's catalogue (e.g. 'eucalyptus', 'dinosaur') count far more than
    common ones ('cutter', 'set', 'pme'), so two different cutters from the same brand don't pair up."""
    a, b = set(_canon(ours_norm)), set(_canon(theirs_norm))
    if not a or not b:
        return 0.0
    idf = idf or (lambda t: 1.0)

    def w(t):
        base = _w(t)
        return base if base == 3.0 else base * idf(t)  # sizes/packs keep a fixed heavy weight

    shared = a & b
    # the most distinctive word in OUR name (e.g. 'dinosaur', 'skull', 'eucalyptus') must appear in theirs
    words = sorted((t for t in a if t.isalpha() and len(t) > 2 and _w(t) == 1.0), key=idf, reverse=True)
    if any(t not in b for t in words[: 2 if len(words) >= 3 else 1]):
        return 0.0
    wa, wb, ws = sum(map(w, a)), sum(map(w, b)), sum(map(w, shared))
    if ws / wa < 0.7:  # most of OUR product's name (by importance) must be found in theirs
        return 0.0
    return 100 * (0.7 * ws / wa + 0.3 * ws / wb)


def label(item: dict) -> str:
    return f"{item['title']} {item.get('variant') or ''}".strip()


def _unit_sizes(n: str) -> set[str]:
    return {t for t in sizes(n) if not t.startswith("pk")}


def _packs(n: str) -> set[str]:
    return {t for t in sizes(n) if t.startswith("pk")}


def item_sizes(item: dict) -> tuple[set[str], set[str]]:
    """Sizes and pack counts. A size in the variant (e.g. '1KG') beats sizes in the title
    (titles like 'Ivory Sugarpaste 1Kg OR 2.5KG' list every option)."""
    v, t = norm(item.get("variant") or ""), norm(item["title"])
    units = _unit_sizes(v) or _unit_sizes(t)
    packs = _packs(v) | _packs(t)
    return units, packs


COLOURS = {"black", "white", "red", "blue", "green", "yellow", "pink", "purple", "orange", "brown", "grey", "gray",
           "gold", "silver", "ivory", "navy", "teal", "lilac", "violet", "cream", "peach", "coral", "mint", "lime",
           "burgundy", "maroon", "fuchsia", "turquoise", "aqua", "beige", "nude", "copper", "bronze", "rose", "ruby",
           "cerise", "magenta", "lavender", "sage", "olive", "forest", "royal", "sky", "baby", "dusky", "dusty",
           "champagne", "rainbow", "pastel", "neon", "caramel", "chocolate", "tangerine", "lemon", "jade", "emerald"}


SIZE_WORDS = {"mini", "small", "medium", "large", "xl", "jumbo", "giant", "tall", "extra"}


def colours(n: str) -> set[str]:
    return set(n.split()) & COLOURS


def compatible(o: dict, t: dict, on: str, tn: str) -> bool:
    # a black colour is not a pink colour: if both name a colour, they must share one
    ca, cb = colours(on), colours(tn)
    if ca and cb and not (ca & cb):
        return False
    sa, sb = set(on.split()) & SIZE_WORDS, set(tn.split()) & SIZE_WORDS
    if sa and sb and not (sa & sb):  # 'large star' is not 'small star'
        return False
    ua, pa = item_sizes(o)
    ub, pb = item_sizes(t)
    if ua and ub and not (ua & ub):
        return False
    # a bulk pack of 10 is not the same thing as a single box
    if (pa or pb) and not ((pa or {"pk1"}) & (pb or {"pk1"})):
        return False
    ba, bb = brand(on), brand(tn)
    if ba and bb and ba != bb:
        return False
    # wildly different prices almost always mean a different pack size or product
    if o["price"] and t["price"] and not (1 / 3 <= t["price"] / o["price"] <= 3):
        return False
    return True


def load_overrides(path: str) -> dict:
    """matches.csv lets you fix a wrong pair or block one:
    our_product,competitor,competitor_url   (competitor_url 'none' = no match)"""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            out[(row["our_product"].strip().lower(), row["competitor"].strip().lower())] = row["competitor_url"].strip()
    return out


def match_all(ours: list[dict], theirs: list[dict], threshold: int, overrides: dict) -> list[dict]:
    """Returns one row per (our item, competitor) that matched."""
    by_site: dict[str, list[dict]] = {}
    for t in theirs:
        if t.get("price"):  # skips missing and zero prices
            by_site.setdefault(t["site"], []).append(t)

    rows = []
    for site, items in by_site.items():
        names = [norm(label(t)) for t in items]
        word_index: dict[str, set[int]] = {}
        for i, n in enumerate(names):
            for w in set(n.split()):
                if len(w) > 2:
                    word_index.setdefault(w, set()).add(i)
        df: dict[str, int] = {}
        for n in names:
            for t in set(_canon(n)):
                df[t] = df.get(t, 0) + 1
        N = len(names)
        idf = lambda t, df=df, N=N: math.log((N + 1) / (df.get(t, 0) + 1)) + 1
        by_barcode = {t["barcode"]: t for t in items if t.get("barcode")}
        by_url: dict[str, list[dict]] = {}
        for t in items:
            by_url.setdefault(t["url"], []).append(t)

        def pick(o, cands):
            # one address can hold Single / 10 Pack / 25 Pack: take the one sold the same way as ours
            mine = item_sizes(o)[1]
            same = [t for t in cands if item_sizes(t)[1] == mine]
            if not same and not mine:
                same = [t for t in cands if "single" in (t.get("variant") or "").lower()]
            return min(same or cands, key=lambda t: t["price"])
        for o in ours:
            if not o.get("price"):
                continue
            ol = label(o)
            key = (ol.lower(), site.lower())
            hit, best = None, 0
            if key in overrides:
                url = overrides[key]
                if url.lower() == "none":
                    continue
                cands = by_url.get(url) or by_url.get(url.rstrip("/")) or by_url.get(url + "/")
                hit, best = (pick(o, cands) if cands else None), 100
            elif o.get("barcode") and o["barcode"] in by_barcode:
                hit, best = by_barcode[o["barcode"]], 100
            else:
                on = norm(ol)
                # only score candidates sharing at least two meaningful words (keeps it fast on big catalogues)
                counts: dict[int, int] = {}
                for w in set(on.split()):
                    for i in word_index.get(w, ()):
                        counts[i] = counts.get(i, 0) + 1
                need = 2 if len([w for w in on.split() if len(w) > 2]) >= 2 else 1
                for idx in (i for i, c in counts.items() if c >= need):
                    cand_name = names[idx]
                    if not compatible(o, items[idx], on, cand_name):
                        continue
                    sc = round(score(on, cand_name, idf))
                    if sc >= threshold and sc > best:
                        hit, best = items[idx], sc
            if hit:
                rows.append({"our": o, "their": hit, "site": site, "score": best})
    return rows


# ==================== emailer ====================

import logging
import os
import smtplib
from email.message import EmailMessage
from html import escape

log = logging.getLogger("watch")


def eur(v):
    return "n/a" if v is None else f"€{v:,.2f}"


def summary_parts(data: dict) -> tuple[str, str, str]:
    ch = data["changes"]
    mine = [c for c in ch if c.get("ours")]
    price = [c for c in mine if c["kind"] == "price"]
    outs = [c for c in mine if c["kind"] == "out"]
    backs = [c for c in mine if c["kind"] == "back"]
    dearer = [r for r in data["rows"] if r["gap"] is not None and r["gap"] > 0.05][:15]
    broken = [n for n, s in data["status"].items() if not s.get("ok")]

    subject = (f"Competitor watch {data['date']}: {len(price)} price changes on your lines, "
               f"{sum(1 for r in data['rows'] if (r['gap'] or 0) > 0.05)} where you're dearer")

    lines, html = [], []

    def section(title, items):
        if not items:
            return
        lines.append(f"\n{title}")
        html.append(f"<h3 style='margin:18px 0 6px;font:600 15px system-ui'>{escape(title)}</h3><table style='border-collapse:collapse;font:14px system-ui'>")
        for txt in items:
            lines.append(f"  {txt}")
            html.append(f"<tr><td style='padding:3px 0'>{escape(txt)}</td></tr>")
        html.append("</table>")

    section("Price changes on products you sell", [
        f"{c['site']}: {c['item']['title']}  {eur(c['was'])} to {eur(c['item']['price'])}  "
        f"({'down' if c['item']['price'] < c['was'] else 'up'}; yours {eur(c['ours']['price'])})"
        for c in sorted(price, key=lambda c: c['item']['price'] - c['was'])])
    section("Competitors now OUT of stock on your lines (a chance to sell)", [
        f"{c['site']}: {c['item']['title']}  (yours {eur(c['ours']['price'])}, "
        f"{'in stock' if c['ours'].get('available') else 'you are out too'})" for c in outs])
    section("Competitors back in stock on your lines", [f"{c['site']}: {c['item']['title']}" for c in backs])
    section("Where you are dearest today", [
        f"{r['title']}{' ' + r['variant'] if r['variant'] else ''}: yours {eur(r['price'])}, cheapest "
        f"{eur(r['cheapest_other'])} at {min(r['theirs'], key=lambda k: r['theirs'][k]['price'] or 1e9)}"
        for r in dearer])
    if data.get("new_count"):
        section("New products", [f"{data['new_count']} new products listed by competitors today (see the page)"])
    if broken:
        section("Sites that could not be read today",
                [f"{n}: {data['status'][n].get('error') or 'no products found'}" for n in broken])
    if not lines:
        lines.append("\nNo changes on your lines since yesterday.")
        html.append("<p>No changes on your lines since yesterday.</p>")

    url = os.environ.get("PAGE_URL", "")
    if url:
        lines.append(f"\nFull comparison: {url}")
        html.append(f"<p style='margin-top:18px'><a href='{escape(url)}'>Open the full comparison</a></p>")
    return subject, "\n".join(lines).strip(), "".join(html)


def send_summary(data: dict):
    host = os.environ.get("SMTP_HOST")
    to = os.environ.get("MAIL_TO")
    if not host or not to:
        log.info("email not configured (SMTP_HOST / MAIL_TO missing); skipping")
        return
    subject, text, html = summary_parts(data)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("MAIL_FROM") or os.environ.get("SMTP_USER")
    msg["To"] = to
    msg.set_content(text)
    msg.add_alternative(f"<div style='font:14px system-ui;color:#222'>{html}</div>", subtype="html")
    port = int(os.environ.get("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=60) as s:
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)
    log.info("email sent to %s", to)


# ==================== report ====================

import json
from pathlib import Path




def build_page(data: dict) -> str:
    blob = json.dumps(data).replace("</", "<\\/")
    return PAGE_TEMPLATE.replace("__DATA__", blob)


# ==================== run ====================

import argparse
import datetime as dt
import gzip
import json
import logging
import os
import shutil
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent
SNAP = ROOT / "data" / "snapshots"
DOCS = ROOT / "docs"
log = logging.getLogger("watch")


def key(it: dict) -> str:
    return f"{it['url']}|{it.get('variant') or ''}"


def save_snapshot(day: str, site: str, items: list[dict]):
    d = SNAP / day
    d.mkdir(parents=True, exist_ok=True)
    with gzip.open(d / f"{site.lower().replace(' ', '_')}.json.gz", "wt", encoding="utf-8") as fh:
        json.dump(items, fh)


def load_previous(day: str, site: str) -> list[dict] | None:
    if not SNAP.exists():
        return None
    name = f"{site.lower().replace(' ', '_')}.json.gz"
    for d in sorted((p for p in SNAP.iterdir() if p.is_dir() and p.name < day), reverse=True):
        f = d / name
        if f.exists():
            with gzip.open(f, "rt", encoding="utf-8") as fh:
                return json.load(fh)
    return None


def diff(prev: list[dict] | None, now: list[dict], min_change: float) -> list[dict]:
    if prev is None:
        return []
    old = {key(i): i for i in prev}
    out = []
    for it in now:
        o = old.get(key(it))
        if o is None:
            out.append({"kind": "new", "item": it})
            continue
        if o.get("price") is not None and it.get("price") is not None and abs(it["price"] - o["price"]) >= min_change:
            out.append({"kind": "price", "item": it, "was": o["price"]})
        if o.get("available") is True and it.get("available") is False:
            out.append({"kind": "out", "item": it})
        elif o.get("available") is False and it.get("available") is True:
            out.append({"kind": "back", "item": it})
    nowkeys = {key(i) for i in now}
    for k, o in old.items():
        if k not in nowkeys:
            out.append({"kind": "gone", "item": o})
    return out


def prune(keep_days: int, today: str):
    if not SNAP.exists():
        return
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=keep_days)).isoformat()
    for d in SNAP.iterdir():
        if d.is_dir() and d.name < cutoff:
            shutil.rmtree(d)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--offline", help="folder of <site>.json files to use instead of fetching")
    ap.add_argument("--date", help="pretend today is YYYY-MM-DD (testing)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    st = cfg.get("settings", {})
    today = a.date or dt.date.today().isoformat()
    f = Fetcher(st.get("user_agent", "PriceWatch/1.0"), float(st.get("delay_seconds", 1.5)))

    def fetch(site):
        if a.offline:
            p = Path(a.offline) / f"{site['name'].lower().replace(' ', '_')}.json"
            return json.loads(p.read_text()) if p.exists() else []
        return read_site(site, f)

    status = {}
    ours = fetch(cfg["our_shop"])
    status[cfg["our_shop"]["name"]] = {"ok": bool(ours), "count": len(ours)}
    save_snapshot(today, cfg["our_shop"]["name"], ours)

    theirs, changes = [], []
    for site in cfg["competitors"]:
        try:
            items = fetch(site)
            status[site["name"]] = {"ok": bool(items), "count": len(items),
                                    "error": None if items else "No products found"}
        except Exception as e:
            log.exception("%s failed", site["name"])
            items = []
            status[site["name"]] = {"ok": False, "count": 0, "error": str(e)[:200]}
        if items:
            changes += [dict(c, site=site["name"]) for c in diff(load_previous(today, site["name"]), items,
                                                                  float(st.get("min_price_change", 0.05)))]
            save_snapshot(today, site["name"], items)
            theirs += items
        log.info("%s: %d products", site["name"], len(items))

    pairs = match_all(ours, theirs, int(st.get("match_threshold", 75)), load_overrides(str(ROOT / "matches.csv")))

    # Which competitor products are matched to things we sell (so their changes matter most)
    matched_urls = {(p["site"], key(p["their"])): p["our"] for p in pairs}
    for c in changes:
        c["ours"] = matched_urls.get((c["site"], key(c["item"])))

    data = {
        "date": today,
        "status": status,
        "competitors": [s["name"] for s in cfg["competitors"]],
        "rows": build_rows(ours, pairs),
        "changes": [c for c in changes if c["kind"] != "new" or c.get("ours")]
                   + [c for c in changes if c["kind"] == "new" and not c.get("ours")][:200],
        "new_count": sum(1 for c in changes if c["kind"] == "new"),
    }
    DOCS.mkdir(exist_ok=True)
    (DOCS / "data.json").write_text(json.dumps(data))
    (DOCS / "index.html").write_text(build_page(data), encoding="utf-8")
    prune(int(st.get("keep_days", 120)), today)

    if not a.no_email:
        send_summary(data)
    log.info("done: %d of our products matched at least once", sum(1 for r in data["rows"] if r["theirs"]))


def build_rows(ours: list[dict], pairs: list[dict]) -> list[dict]:
    by_ours: dict[str, dict] = {}
    for p in pairs:
        o = p["our"]
        r = by_ours.setdefault(key(o), {
            "title": o["title"], "variant": o.get("variant", ""), "url": o["url"],
            "price": o["price"], "available": o.get("available"), "theirs": {}})
        t = p["their"]
        prev = r["theirs"].get(p["site"])
        if prev is None or p["score"] > prev["score"]:
            r["theirs"][p["site"]] = {"title": t["title"] + (f" ({t['variant']})" if t.get("variant") else ""),
                                      "price": t["price"], "available": t.get("available"),
                                      "url": t["url"], "score": p["score"]}
    rows = []
    for r in by_ours.values():
        prices = [v["price"] for v in r["theirs"].values() if v["price"] is not None]
        low = min(prices) if prices else None
        r["cheapest_other"] = low
        r["gap"] = round(r["price"] - low, 2) if low is not None else None
        rows.append(r)
    rows.sort(key=lambda r: -(r["gap"] or -999))
    return rows





PAGE_TEMPLATE = r"""<!doctype html>
<html lang="en-IE">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Competitor Price Watch</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600&family=Source+Sans+3:wght@400;600&display=swap">
<style>
/* Layout: status strip, then two views (comparison table / today's changes) on one page */
:root{--bg:#fbf8fa;--fg:#2a2229;--muted:#7a6d77;--line:#e8dee5;--card:#fff;--accent:#b0336f;
--bad:#c0392b;--badbg:#fbe7e4;--good:#2e7d4f;--goodbg:#e3f3e8;--warn:#9a6a00;--warnbg:#fbf1d8;
--display:"Fraunces",Georgia,serif;--body:"Source Sans 3",system-ui,sans-serif;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#1c171b;--fg:#f1e9ef;--muted:#b3a5af;--line:#3a3038;--card:#241e23;--accent:#e57aa9;
--bad:#ff8a7a;--badbg:#3b1f1c;--good:#7fd39d;--goodbg:#1d3326;--warn:#f0c35a;--warnbg:#382d14;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 var(--body)}
.wrap{max-width:1200px;margin:0 auto;padding:24px max(16px,env(safe-area-inset-right)) 48px max(16px,env(safe-area-inset-left));display:grid;gap:20px}
h1{font:600 1.8rem var(--display);margin:0}
.sub{color:var(--muted);margin:4px 0 0}
.status{display:flex;flex-wrap:wrap;gap:8px}
.chip{padding:3px 10px;border-radius:99px;font-size:.85rem;background:var(--goodbg);color:var(--good)}
.chip.bad{background:var(--badbg);color:var(--bad)}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line)}
.tabs button{font:600 .95rem var(--body);background:none;border:0;padding:10px 14px;color:var(--muted);cursor:pointer;border-bottom:2px solid transparent}
.tabs button[aria-selected=true]{color:var(--fg);border-color:var(--accent)}
.controls{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center}
.controls input[type=search]{font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);min-width:0;width:min(320px,100%)}
.controls label{display:flex;gap:6px;align-items:center;font-size:.92rem}
.tw{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);position:sticky;top:0;background:var(--card)}
.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.cheaper{color:var(--bad);font-weight:600}
.dearer{color:var(--good)}
.oos{font-size:.75rem;color:var(--warn);display:block}
.mt{font-size:.75rem;color:var(--muted);display:block;max-width:22ch;white-space:normal}
a{color:var(--accent)}
.gap.pos{color:var(--bad);font-weight:600}.gap.neg{color:var(--good)}
.count{color:var(--muted);font-size:.9rem}
.kind{font-size:.75rem;font-weight:600;padding:1px 7px;border-radius:99px;white-space:nowrap}
.k-price-down{background:var(--badbg);color:var(--bad)}.k-price-up{background:var(--goodbg);color:var(--good)}
.k-out{background:var(--goodbg);color:var(--good)}.k-back{background:var(--warnbg);color:var(--warn)}
.k-new,.k-gone{background:var(--line);color:var(--muted)}
button:focus-visible,input:focus-visible,a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Competitor Price Watch</h1>
    <p class="sub" id="sub"></p>
  </header>
  <div class="status" id="status"></div>
  <div class="tabs" role="tablist">
    <button role="tab" id="t-compare" aria-selected="true">Your products vs competitors</button>
    <button role="tab" id="t-changes" aria-selected="false">Today's changes</button>
  </div>

  <section id="v-compare">
    <div class="controls">
      <input type="search" id="q" placeholder="Search products">
      <label><input type="checkbox" id="f-dearer"> Only where I'm dearer</label>
      <label><input type="checkbox" id="f-oos"> Only where a competitor is out of stock</label>
      <span class="count" id="count"></span>
    </div>
    <div class="tw" style="margin-top:12px"><table id="cmp"><thead></thead><tbody></tbody></table></div>
    <p class="sub" style="font-size:.85rem;margin-top:10px">Red price = cheaper than you. Pairs are matched by name and size; the small grey text shows what was matched. Fix a wrong pair in matches.csv.</p>
  </section>

  <section id="v-changes" hidden>
    <div class="controls">
      <label><input type="checkbox" id="f-mine" checked> Only products I sell</label>
      <span class="count" id="ccount"></span>
    </div>
    <div class="tw" style="margin-top:12px"><table id="chg"><thead><tr><th>Shop</th><th>Change</th><th>Product</th><th class="n">Was</th><th class="n">Now</th><th class="n">Yours</th></tr></thead><tbody></tbody></table></div>
  </section>
</div>
<script id="data" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent);
const eur = v => v == null ? '' : '€' + v.toFixed(2);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmtDate = d => new Date(d + 'T12:00').toLocaleDateString('en-IE', {weekday:'long', day:'numeric', month:'long', year:'numeric'});
document.getElementById('sub').textContent = 'Scanned ' + fmtDate(D.date) + ' · ' + D.rows.length + ' of your products found at one or more competitors';
document.getElementById('status').innerHTML = Object.entries(D.status).map(([n, s]) =>
  `<span class="chip ${s.ok ? '' : 'bad'}" title="${esc(s.error || '')}">${esc(n)}: ${s.ok ? s.count.toLocaleString() + ' items' : 'not read'}</span>`).join('');

const comps = D.competitors;
document.querySelector('#cmp thead').innerHTML = '<tr><th>Product</th><th class="n">Yours</th>' +
  comps.map(c => `<th class="n">${esc(c)}</th>`).join('') + '<th class="n">You vs cheapest</th></tr>';

function renderCompare() {
  const q = document.getElementById('q').value.trim().toLowerCase();
  const fd = document.getElementById('f-dearer').checked, fo = document.getElementById('f-oos').checked;
  const rows = D.rows.filter(r =>
    (!q || (r.title + ' ' + r.variant).toLowerCase().includes(q)) &&
    (!fd || (r.gap ?? 0) > 0.05) &&
    (!fo || Object.values(r.theirs).some(t => t.available === false)));
  document.getElementById('count').textContent = rows.length + ' shown';
  document.querySelector('#cmp tbody').innerHTML = rows.slice(0, 800).map(r => {
    const cells = comps.map(c => {
      const t = r.theirs[c];
      if (!t) return '<td class="n"></td>';
      const cls = t.price < r.price - 0.005 ? 'cheaper' : (t.price > r.price + 0.005 ? 'dearer' : '');
      return `<td class="n"><a class="${cls}" href="${esc(t.url)}" target="_blank" rel="noopener">${eur(t.price)}</a>` +
        (t.available === false ? '<span class="oos">out of stock</span>' : '') +
        `<span class="mt">${esc(t.title)}</span></td>`;
    }).join('');
    const g = r.gap;
    return `<tr><td><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.title)}</a>${r.variant ? ' <span class="mt" style="display:inline">' + esc(r.variant) + '</span>' : ''}` +
      (r.available === false ? '<span class="oos">you are out of stock</span>' : '') + `</td>` +
      `<td class="n">${eur(r.price)}</td>${cells}` +
      `<td class="n gap ${g > 0.05 ? 'pos' : (g < -0.05 ? 'neg' : '')}">${g == null ? '' : (g > 0 ? '+' : '') + eur(g).replace('€-', '-€')}</td></tr>`;
  }).join('');
}

function renderChanges() {
  const mine = document.getElementById('f-mine').checked;
  const label = c => c.kind === 'price' ? (c.item.price < c.was ? ['price-down', 'Price down'] : ['price-up', 'Price up'])
    : ({out: ['out', 'Out of stock'], back: ['back', 'Back in stock'], new: ['new', 'New'], gone: ['gone', 'Removed']})[c.kind];
  const list = D.changes.filter(c => !mine || c.ours);
  document.getElementById('ccount').textContent = list.length + ' changes since the last scan';
  document.querySelector('#chg tbody').innerHTML = list.map(c => {
    const [k, t] = label(c);
    return `<tr><td>${esc(c.site)}</td><td><span class="kind k-${k}">${t}</span></td>` +
      `<td><a href="${esc(c.item.url)}" target="_blank" rel="noopener">${esc(c.item.title)}</a></td>` +
      `<td class="n">${eur(c.was)}</td><td class="n">${eur(c.item.price)}</td><td class="n">${c.ours ? eur(c.ours.price) : ''}</td></tr>`;
  }).join('') || '<tr><td colspan="6">No changes since the last scan.</td></tr>';
}

function tab(which) {
  for (const n of ['compare', 'changes']) {
    document.getElementById('t-' + n).setAttribute('aria-selected', n === which);
    document.getElementById('v-' + n).hidden = n !== which;
  }
}
document.getElementById('t-compare').onclick = () => tab('compare');
document.getElementById('t-changes').onclick = () => tab('changes');
['q', 'f-dearer', 'f-oos'].forEach(id => document.getElementById(id).addEventListener('input', renderCompare));
document.getElementById('f-mine').addEventListener('input', renderChanges);
renderCompare(); renderChanges();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
