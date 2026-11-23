import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
WP_WEBHOOK_URL = os.environ.get("WP_WEBHOOK_URL", "").strip()

SEEN_FILE = "seen.json"
OUTPUT_FILE = "news.json"
MAX_NEW_PER_LANG = 3      # Keeps each run fast and well within free AI quotas
MAX_FEED_ITEMS = 45       # Rolling window in news.json

VALID_CATEGORIES = [
    "tunisia",
    "world",
    "economy",
    "sports",
    "culture-tech",
    "society-health",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,ar;q=0.8,en;q=0.7",
}

SOURCES = {
    "ar": {
        "name": "Mosaïque FM",
        "listings": [
            ("https://www.mosaiquefm.net/ar/actualites", "tunisia"),
            ("https://www.mosaiquefm.net/ar/actualites/%D8%A7%D9%84%D8%A3%D8%AE%D8%A8%D8%A7%D8%B1-%D8%A7%D9%84%D8%B9%D8%A7%D9%84%D9%85%D9%8A%D8%A9/3", "world"),
        ],
    },
    "fr": {
        "name": "France 24",
        "listings": [
            ("https://www.france24.com/fr/afrique/", "tunisia"),
            ("https://www.france24.com/fr/", "world"),
            ("https://www.france24.com/fr/sports/", "sports"),
            ("https://www.france24.com/fr/culture/", "culture-tech"),
        ],
    },
    "en": {
        "name": "France 24",
        "listings": [
            ("https://www.france24.com/en/africa/", "tunisia"),
            ("https://www.france24.com/en/europe/", "world"),
            ("https://www.france24.com/en/business/", "economy"),
            ("https://www.france24.com/en/sport/", "sports"),
            ("https://www.france24.com/en/culture/", "culture-tech"),
            ("https://www.france24.com/en/health/", "society-health"),
        ],
    },
}


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def url_hash(url):
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:16]


def fetch_html(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code == 200:
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
    except Exception as e:
        print(f"[FETCH ERROR] {url}: {e}")
    return None


def extract_listing_links(lang, listing_url, html):
    soup = BeautifulSoup(html, "lxml")
    links = []
    seen_local = set()

    for a in soup.find_all("a", href=True):
        href = urljoin(listing_url, a["href"].strip()).split("#")[0].split("?")[0]
        if href in seen_local:
            continue

        if lang == "ar":
            # Mosaïque FM article URLs contain /ar/ followed by a numeric ID or slug
            if "mosaiquefm.net/ar/" in href and re.search(r"/\d{5,}/", href):
                seen_local.add(href)
                links.append(href)
        else:
            # France 24 article URLs contain /fr/YYYYMMDD- or /en/YYYYMMDD-
            if f"france24.com/{lang}/" in href and re.search(r"/20\d{6}-", href):
                seen_local.add(href)
                links.append(href)

        if len(links) >= 12:
            break

    return links


def get_img_src(img_tag, base_url):
    for attr in ("data-src", "data-lazy-src", "data-original", "src"):
        val = img_tag.get(attr)
        if val and not val.startswith("data:image"):
            return urljoin(base_url, val.strip())
    return None


def parse_article(lang, url):
    html = fetch_html(url)
    if not html:
        return None

    soup = BeautifulSoup(html, "lxml")

    # Title
    h1 = soup.find("h1")
    og_title = soup.find("meta", property="og:title")
    title = (
        h1.get_text(" ", strip=True)
        if h1
        else (og_title["content"].strip() if og_title and og_title.get("content") else "")
    )
    if not title or len(title) < 15:
        return None

    # Featured image
    og_img = soup.find("meta", property="og:image")
    featured_image = og_img["content"].strip() if og_img and og_img.get("content") else ""

    # Locate main article container
    container = None
    if lang == "ar":
        for sel in (
            ".article-content",
            ".desc",
            ".article-body",
            "article .content",
            "article",
        ):
            container = soup.select_one(sel)
            if container and len(container.get_text(strip=True)) > 180:
                break
    else:
        for sel in (".t-content__body", "article .t-content", "article"):
            container = soup.select_one(sel)
            if container and len(container.get_text(strip=True)) > 180:
                break

    if not container:
        return None

    # Strip ads, scripts, share widgets, and related boxes
    for bad in container.select(
        "script, style, iframe, nav, aside, .m-read-more, .related, .share, .ad, .ads, figcaption button"
    ):
        bad.decompose()

    inline_images = []
    blocks = []

    for elem in container.find_all(["p", "h2", "h3", "blockquote", "figure", "img"]):
        if elem.name == "figure" or elem.name == "img":
            img = elem if elem.name == "img" else elem.find("img")
            if not img:
                continue
            src = get_img_src(img, url)
            if not src or src == featured_image or src in [i["url"] for i in inline_images]:
                continue
            if any(skip in src.lower() for skip in ("logo", "icon", "avatar", "sprite")):
                continue
            token = f"[[IMG_{len(inline_images) + 1}]]"
            alt = img.get("alt", "").strip() or title
            inline_images.append({"token": token, "url": src, "alt": alt})
            blocks.append(token)
        else:
            # Avoid duplicating text inside nested elements
            if elem.find_parent(["blockquote", "figure"]):
                continue
            text = elem.get_text(" ", strip=True)
            if len(text) < 20:
                continue
            if elem.name in ("h2", "h3"):
                blocks.append(f"<h2>{text}</h2>")
            elif elem.name == "blockquote":
                blocks.append(f"<blockquote>{text}</blockquote>")
            else:
                blocks.append(f"<p>{text}</p>")

    body_html = "\n".join(blocks)
    if len(re.sub(r"<[^>]+>", "", body_html)) < 220:
        return None

    return {
        "title": title,
        "body_html": body_html,
        "featured_image": featured_image,
        "inline_images": inline_images,
        "url": url,
    }


def build_ai_prompt(article, lang, hint_category):
    lang_names = {"ar": "Arabic", "fr": "French", "en": "English"}
    return f"""You are a senior news editor for twensa.net.
Task: Lightly polish the article below in {lang_names[lang]} so it is NOT copy-paste duplicate text, while keeping the FULL length, ALL facts, quotes, and details intact. Do NOT summarize or shorten it.

Rules:
1. Keep the exact language: {lang_names[lang]} ({lang}).
2. Rephrase the headline cleanly with the same meaning.
3. Lightly vary phrasing and transitions in every paragraph without dropping facts. Remove source self-references (like "أفاد مراسل موزاييك" or "France 24").
4. PRESERVE every image placeholder token like [[IMG_1]], [[IMG_2]] in its exact place. Never rename or remove them.
5. Output valid HTML using only <p>, <h2>, <blockquote> tags and the [[IMG_X]] tokens.
6. Confirm the best category key from ONLY this list: {VALID_CATEGORIES} (hint: {hint_category}).
7. Generate a clean SEO title (max 60 chars) and SEO meta description (max 155 chars) in {lang_names[lang]}.

Return ONLY a valid JSON object with these exact keys:
{{
  "lang": "{lang}",
  "category": "one_of_the_6_keys",
  "title": "modified headline",
  "content_html": "full modified HTML keeping [[IMG_X]] tokens",
  "seo_title": "SEO title",
  "seo_description": "SEO meta description"
}}

ORIGINAL HEADLINE:
{article["title"]}

ORIGINAL ARTICLE:
{article["body_html"][:9000]}
"""


def parse_json_response(raw_text):
    text = raw_text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        text = text[start : end + 1]
    return json.loads(text)


def call_gemini(prompt):
    if not GEMINI_API_KEY:
        return None
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent"
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY,
    }
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.4,
            "responseMimeType": "application/json",
        },
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=45)
        if r.status_code == 200:
            data = r.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return parse_json_response(text)
        print(f"[GEMINI {r.status_code}] {r.text[:200]}")
    except Exception as e:
        print(f"[GEMINI ERROR] {e}")
    return None


def call_groq(prompt):
    if not GROQ_API_KEY:
        return None
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GROQ_API_KEY}",
    }
    for model in ("openai/gpt-oss-120b", "openai/gpt-oss-20b"):
        payload = {
            "model": model,
            "temperature": 0.4,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "You return only strict valid JSON."},
                {"role": "user", "content": prompt},
            ],
        }
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=45)
            if r.status_code == 200:
                text = r.json()["choices"][0]["message"]["content"]
                return parse_json_response(text)
            print(f"[GROQ {model} {r.status_code}] {r.text[:200]}")
        except Exception as e:
            print(f"[GROQ {model} ERROR] {e}")
    return None


def restore_inline_images(html_content, inline_images):
    for img in inline_images:
        figure = (
            f'<figure class="wp-block-image size-large">'
            f'<img src="{img["url"]}" alt="{img["alt"]}" />'
            f"</figure>"
        )
        if img["token"] in html_content:
            html_content = html_content.replace(img["token"], figure)
        else:
            html_content += "\n" + figure
    return html_content


def main():
    seen = load_json(SEEN_FILE, [])
    seen_set = set(seen)
    feed = load_json(OUTPUT_FILE, {"updated_at": "", "items": []})
    existing_items = feed.get("items", [])
    new_items = []

    for lang, cfg in SOURCES.items():
        collected = 0
        for listing_url, hint_cat in cfg["listings"]:
            if collected >= MAX_NEW_PER_LANG:
                break
            html = fetch_html(listing_url)
            if not html:
                continue
            links = extract_listing_links(lang, listing_url, html)

            for link in links:
                if collected >= MAX_NEW_PER_LANG:
                    break
                h = url_hash(link)
                if h in seen_set:
                    continue

                article = parse_article(lang, link)
                if not article:
                    continue

                prompt = build_ai_prompt(article, lang, hint_cat)
                ai_data = call_gemini(prompt)
                if not ai_data:
                    ai_data = call_groq(prompt)

                # Pause to respect free-tier RPM limits
                time.sleep(6)

                if not ai_data or not ai_data.get("title") or not ai_data.get("content_html"):
                    continue

                category = ai_data.get("category", hint_cat)
                if category not in VALID_CATEGORIES:
                    category = hint_cat

                final_html = restore_inline_images(
                    ai_data["content_html"], article["inline_images"]
                )

                new_items.append({
                    "hash": h,
                    "lang": lang,
                    "category": category,
                    "title": ai_data["title"].strip(),
                    "content_html": final_html,
                    "featured_image": article["featured_image"],
                    "inline_images": [i["url"] for i in article["inline_images"]],
                    "seo_title": ai_data.get("seo_title", ai_data["title"]).strip(),
                    "seo_description": ai_data.get("seo_description", "").strip(),
                    "source_name": cfg["name"],
                    "source_url": link,
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                })

                seen.append(h)
                seen_set.add(h)
                collected += 1
                print(f"[OK] ({lang}/{category}) {ai_data['title'][:65]}")

    if new_items:
        merged = new_items + existing_items
        feed = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "items": merged[:MAX_FEED_ITEMS],
        }
        save_json(OUTPUT_FILE, feed)
        save_json(SEEN_FILE, seen[-1000:])
        print(f"Saved {len(new_items)} new articles to {OUTPUT_FILE}.")

        if WP_WEBHOOK_URL:
            try:
                r = requests.get(WP_WEBHOOK_URL, timeout=30)
                print(f"[WEBHOOK] Status {r.status_code}: {r.text[:120]}")
            except Exception as e:
                print(f"[WEBHOOK ERROR] {e}")
    else:
        print("No new articles found.")


if __name__ == "__main__":
    main()
