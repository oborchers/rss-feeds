import argparse
import json
import logging
from datetime import datetime
from pathlib import Path

import pytz
import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator

from utils import get_cache_dir, get_feeds_dir, setup_feed_links, sort_posts_for_feed

# Set up logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

BLOG_URL = "https://weaviate.io/blog"
FEED_NAME = "weaviate"
MAX_PAGES_FULL = 5
MAX_PAGES_INCREMENTAL = 1


def get_cache_file():
    """Get the cache file path."""
    return get_cache_dir() / "weaviate_posts.json"


def fetch_page(url):
    """Fetch a single page HTML."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
    }
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    return response.text


def parse_posts(html_content):
    """Parse the blog HTML content and extract post information.

    Returns (posts, has_next_page).
    """
    soup = BeautifulSoup(html_content, "html.parser")
    blog_posts = []

    articles = soup.select("article.margin-bottom--xl")
    for article in articles:
        title_elem = article.select_one("h2")
        if not title_elem:
            continue
        title = title_elem.text.strip()

        time_elem = article.select_one("time[datetime]")
        date_str = None
        if time_elem:
            date_str = time_elem["datetime"]

        url_elem = article.select_one('a[itemprop="url"]')
        if not url_elem or not url_elem.get("href"):
            continue
        url = url_elem["href"]
        if url.startswith("/"):
            url = f"https://weaviate.io{url}"

        desc_elem = article.select_one('meta[itemprop="description"]')
        description = desc_elem["content"] if desc_elem else ""

        blog_posts.append(
            {
                "url": url,
                "title": title,
                "date": date_str,
                "description": description,
            }
        )

    next_link = soup.select_one("a.pagination-nav__link--next")
    has_next_page = next_link is not None

    return blog_posts, has_next_page


def load_cache():
    """Load existing cache or return empty structure."""
    cache_file = get_cache_file()
    if cache_file.exists():
        with open(cache_file, "r") as f:
            data = json.load(f)
            logger.info(f"Loaded cache with {len(data.get('posts', []))} posts")
            return data
    logger.info("No cache file found, will do full fetch")
    return {"last_updated": None, "posts": []}


def save_cache(posts):
    """Save posts to cache file."""
    cache_file = get_cache_file()
    data = {
        "last_updated": datetime.now(pytz.UTC).isoformat(),
        "posts": posts,
    }
    with open(cache_file, "w") as f:
        json.dump(data, f, indent=2)
    logger.info(f"Saved cache with {len(posts)} posts to {cache_file}")


def merge_posts(new_posts, cached_posts):
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


def fetch_all_pages(max_pages):
    """Follow pagination until no next link or max_pages reached."""
    all_posts = []

    for page_num in range(1, max_pages + 1):
        if page_num == 1:
            url = BLOG_URL
        else:
            url = f"{BLOG_URL}/page/{page_num}"

        logger.info(f"Fetching page {page_num}: {url}")
        html = fetch_page(url)
        posts, has_next_page = parse_posts(html)
        all_posts.extend(posts)
        logger.info(f"Found {len(posts)} posts on page {page_num}")

        if not has_next_page:
            break

    # Dedupe by URL
    seen = set()
    unique_posts = []
    for post in all_posts:
        if post["url"] not in seen:
            unique_posts.append(post)
            seen.add(post["url"])

    sorted_posts = sort_posts_for_feed(unique_posts, date_field="date")
    logger.info(f"Total unique posts across all pages: {len(sorted_posts)}")
    return sorted_posts


def generate_rss_feed(posts):
    """Generate RSS feed from blog posts."""
    fg = FeedGenerator()
    fg.title("Weaviate Blog")
    fg.description(
        "Read the latest from the Weaviate team: insights, tutorials, and updates on vector databases, AI-native applications, and search."
    )
    fg.language("en")
    fg.author({"name": "Weaviate"})
    fg.subtitle("Latest updates from Weaviate")
    setup_feed_links(fg, blog_url=BLOG_URL, feed_name=FEED_NAME)

    for post in posts:
        fe = fg.add_entry()
        fe.title(post["title"])
        fe.description(post["description"])
        fe.link(href=post["url"])
        fe.id(post["url"])

        if post.get("date"):
            try:
                dt = datetime.fromisoformat(post["date"].replace("Z", "+00:00"))
                fe.published(dt)
            except ValueError:
                pass

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
    """Main function to generate RSS feed from blog URL.

    Args:
        full_reset: If True, fetch up to MAX_PAGES_FULL pages.
                   If False, only fetch page 1 and merge with cache.
    """
    cache = load_cache()

    if full_reset or not cache["posts"]:
        mode = "full reset" if full_reset else "no cache exists"
        logger.info(f"Running full fetch ({mode})")
        posts = fetch_all_pages(MAX_PAGES_FULL)
    else:
        logger.info("Running incremental update (page 1 only)")
        html = fetch_page(BLOG_URL)
        new_posts, _ = parse_posts(html)
        logger.info(f"Found {len(new_posts)} posts on page 1")
        posts = merge_posts(new_posts, cache["posts"])

    save_cache(posts)
    feed = generate_rss_feed(posts)
    save_rss_feed(feed)

    logger.info("Done!")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate Weaviate Blog RSS feed")
    parser.add_argument(
        "--full", action="store_true", help="Force full reset (fetch all pages)"
    )
    args = parser.parse_args()
    main(full_reset=args.full)
