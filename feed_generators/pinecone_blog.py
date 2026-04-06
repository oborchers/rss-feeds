import argparse
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytz
import undetected_chromedriver  # noqa: F401 (detected by run_all_feeds.py)
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
from selenium.webdriver.common.by import By

from utils import get_cache_dir, get_feeds_dir, setup_feed_links, setup_selenium_driver, sort_posts_for_feed

# Set up logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

BLOG_URL = "https://www.pinecone.io/blog/?view=list"
FEED_NAME = "pinecone"
MAX_CLICKS_FULL = 15
MAX_CLICKS_INCREMENTAL = 3


def stable_fallback_date(identifier):
    """Generate a stable date from a URL or title hash."""
    hash_val = abs(hash(identifier)) % 730
    epoch = datetime(2023, 1, 1, 0, 0, 0, tzinfo=pytz.UTC)
    return epoch + timedelta(days=hash_val)


def get_cache_file():
    """Get the cache file path."""
    return get_cache_dir() / "pinecone_posts.json"


def fetch_blog_content(max_clicks=MAX_CLICKS_FULL):
    """Fetch the fully loaded HTML content using Selenium.

    Args:
        max_clicks: Maximum number of "Load More" button clicks.
                   Use 15 for full fetch, 3 for incremental updates.
    """
    driver = None
    try:
        logger.info(f"Fetching content from {BLOG_URL} (max_clicks={max_clicks})")
        driver = setup_selenium_driver()
        driver.get(BLOG_URL)

        # Wait for initial page load
        time.sleep(5)
        logger.info("Page loaded, looking for Load More button...")

        clicks = 0
        while clicks < max_clicks:
            try:
                load_more = driver.find_element(
                    By.XPATH, "//button[.//span[text()='Load More'] or text()='Load More']"
                )
                if load_more and load_more.is_displayed():
                    logger.info(f"Clicking 'Load More' (click {clicks + 1})...")
                    driver.execute_script("arguments[0].click();", load_more)
                    clicks += 1
                    time.sleep(2)
                else:
                    logger.info("Load More button not visible, stopping")
                    break
            except Exception:
                logger.info(f"No more Load More button found after {clicks} clicks")
                break

        html = driver.page_source
        logger.info(f"Fetched page source after {clicks} clicks")
        return html

    except Exception as e:
        logger.error(f"Error fetching blog content: {e}")
        raise
    finally:
        if driver:
            driver.quit()


def parse_blog_html(html):
    """Parse the blog HTML and extract post information."""
    soup = BeautifulSoup(html, "html.parser")
    posts = []
    seen_urls = set()

    # Parse featured posts (top section)
    featured_links = soup.select('a[href^="/blog/"][href$="/"]')
    for link in featured_links:
        href = link.get("href", "")
        if href == "/blog/" or "/tag" in href:
            continue

        h2 = link.select_one("h2")
        if not h2:
            continue

        title = h2.text.strip()
        url = f"https://www.pinecone.io{href}"

        if url in seen_urls:
            continue
        seen_urls.add(url)

        # Date from span.text-text-secondary
        date_el = link.select_one("span.text-text-secondary")
        date_obj = None
        if date_el:
            try:
                date_obj = datetime.strptime(date_el.text.strip(), "%b %d, %Y").replace(
                    tzinfo=pytz.UTC
                )
            except ValueError:
                pass

        # Category
        cat_el = link.select_one("span.text-brand-blue, span[class*='brand']")
        category = cat_el.text.strip() if cat_el else ""

        if not date_obj:
            date_obj = stable_fallback_date(url)

        posts.append(
            {
                "url": url,
                "title": title,
                "date": date_obj,
                "category": category,
                "description": "",
            }
        )

    # Parse list view rows
    list_rows = soup.select('a[target="_self"][href^="/blog/"]')
    for row in list_rows:
        href = row.get("href", "")
        url = f"https://www.pinecone.io{href}"

        if url in seen_urls:
            continue
        seen_urls.add(url)

        # Category and date from text-text-secondary divs
        secondary_divs = row.select("div.text-text-secondary")
        category = secondary_divs[0].text.strip() if len(secondary_divs) > 0 else ""
        date_text = secondary_divs[1].text.strip() if len(secondary_divs) > 1 else ""

        # Title
        title_el = row.select_one("div.text-xl")
        title = title_el.text.strip() if title_el else ""

        if not title:
            continue

        date_obj = None
        if date_text:
            try:
                date_obj = datetime.strptime(date_text, "%b %d, %Y").replace(
                    tzinfo=pytz.UTC
                )
            except ValueError:
                pass

        if not date_obj:
            date_obj = stable_fallback_date(url)

        posts.append(
            {
                "url": url,
                "title": title,
                "date": date_obj,
                "category": category,
                "description": "",
            }
        )

    logger.info(f"Parsed {len(posts)} posts")
    return posts


def load_cache():
    """Load existing cache or return empty structure."""
    cache_file = get_cache_file()
    if cache_file.exists():
        with open(cache_file, "r") as f:
            data = json.load(f)
            # Deserialize dates
            for post in data.get("posts", []):
                if post.get("date") and isinstance(post["date"], str):
                    try:
                        post["date"] = datetime.fromisoformat(post["date"])
                    except ValueError:
                        post["date"] = None
            logger.info(f"Loaded cache with {len(data.get('posts', []))} posts")
            return data
    logger.info("No cache file found, will do full fetch")
    return {"last_updated": None, "posts": []}


def save_cache(posts):
    """Save posts to cache file."""
    cache_file = get_cache_file()
    serializable = []
    for post in posts:
        p = dict(post)
        if isinstance(p.get("date"), datetime):
            p["date"] = p["date"].isoformat()
        serializable.append(p)

    data = {
        "last_updated": datetime.now(pytz.UTC).isoformat(),
        "posts": serializable,
    }
    with open(cache_file, "w") as f:
        json.dump(data, f, indent=2)
    logger.info(f"Saved cache with {len(posts)} posts to {cache_file}")


def merge_articles(new_posts, cached_posts):
    """Merge new posts into cache, dedupe by URL, sort by date."""
    existing_urls = {p["url"] for p in cached_posts}
    merged = list(cached_posts)

    added_count = 0
    for post in new_posts:
        if post["url"] not in existing_urls:
            merged.append(post)
            existing_urls.add(post["url"])
            added_count += 1

    logger.info(f"Added {added_count} new posts to cache")
    return sort_posts_for_feed(merged, date_field="date")


def generate_rss_feed(posts):
    """Generate RSS feed from blog posts."""
    fg = FeedGenerator()
    fg.title("Pinecone Blog")
    fg.description(
        "Latest from Pinecone: insights, tutorials, and updates on vector databases and AI infrastructure."
    )
    fg.language("en")
    fg.author({"name": "Pinecone"})
    fg.subtitle("Latest updates from Pinecone")
    setup_feed_links(fg, blog_url="https://www.pinecone.io/blog/", feed_name=FEED_NAME)

    for post in posts:
        fe = fg.add_entry()
        fe.title(post["title"])
        fe.link(href=post["url"])
        fe.id(post["url"])

        if post.get("description"):
            fe.description(post["description"])

        if post.get("category"):
            fe.category(term=post["category"])

        if post.get("date"):
            date = post["date"]
            if isinstance(date, str):
                try:
                    date = datetime.fromisoformat(date)
                except ValueError:
                    date = None
            if date:
                if date.tzinfo is None:
                    date = date.replace(tzinfo=pytz.UTC)
                fe.published(date)

    logger.info(f"Generated RSS feed with {len(posts)} entries")
    return fg


def save_rss_feed(feed_generator):
    """Save the RSS feed to a file in the feeds directory."""
    feeds_dir = get_feeds_dir()
    output_file = feeds_dir / f"feed_{FEED_NAME}.xml"
    feed_generator.rss_file(str(output_file), pretty=True)
    logger.info(f"Saved RSS feed to {output_file}")
    return output_file


def main(full_reset=False):
    """Main function to generate RSS feed.

    Args:
        full_reset: If True, click Load More up to MAX_CLICKS_FULL times.
                   If False, click MAX_CLICKS_INCREMENTAL times and merge with cache.
    """
    cache = load_cache()

    if full_reset or not cache["posts"]:
        mode = "full reset" if full_reset else "no cache exists"
        logger.info(f"Running full fetch ({mode})")
        max_clicks = MAX_CLICKS_FULL
    else:
        logger.info("Running incremental update")
        max_clicks = MAX_CLICKS_INCREMENTAL

    html = fetch_blog_content(max_clicks=max_clicks)
    new_posts = parse_blog_html(html)

    if full_reset or not cache["posts"]:
        posts = sort_posts_for_feed(new_posts, date_field="date")
    else:
        posts = merge_articles(new_posts, cache["posts"])

    save_cache(posts)
    feed = generate_rss_feed(posts)
    save_rss_feed(feed)

    logger.info("Done!")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate Pinecone Blog RSS feed")
    parser.add_argument(
        "--full", action="store_true", help="Force full reset (fetch all pages)"
    )
    args = parser.parse_args()
    main(full_reset=args.full)
