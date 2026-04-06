import argparse
import json
import logging
from datetime import datetime, timedelta

import pytz
import requests
from feedgen.feed import FeedGenerator
from utils import get_cache_dir, get_feeds_dir, setup_feed_links, sort_posts_for_feed

FEED_NAME = "cohere"
BLOG_URL = "https://cohere.com/blog"
GHOST_API_URL = "https://cohere-ai.ghost.io/ghost/api/content/posts/"
GHOST_API_KEY = "572d288a9364f8e4186af1d60a"
MAX_POSTS_FULL = 50
MAX_POSTS_INCREMENTAL = 15

# Set up logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def stable_fallback_date(identifier):
    """Generate a stable date from a URL or title hash."""
    hash_val = abs(hash(identifier)) % 730
    epoch = datetime(2023, 1, 1, 0, 0, 0, tzinfo=pytz.UTC)
    return epoch + timedelta(days=hash_val)


def get_cache_file():
    """Get the cache file path."""
    return get_cache_dir() / f"{FEED_NAME}_posts.json"


def load_cache():
    cache_file = get_cache_file()
    if cache_file.exists():
        with open(cache_file, "r") as f:
            data = json.load(f)
            logger.info(f"Loaded cache with {len(data.get('posts', []))} posts")
            return data
    logger.info("No cache file found, will do full fetch")
    return {"last_updated": None, "posts": []}


def save_cache(posts):
    cache_file = get_cache_file()
    serializable = []
    for post in posts:
        post_copy = post.copy()
        if isinstance(post_copy.get("date"), datetime):
            post_copy["date"] = post_copy["date"].isoformat()
        serializable.append(post_copy)

    data = {
        "last_updated": datetime.now(pytz.UTC).isoformat(),
        "posts": serializable,
    }
    with open(cache_file, "w") as f:
        json.dump(data, f, indent=2)
    logger.info(f"Saved cache with {len(posts)} posts to {cache_file}")


def deserialize_posts(posts):
    result = []
    for post in posts:
        post_copy = post.copy()
        if isinstance(post_copy.get("date"), str):
            try:
                post_copy["date"] = datetime.fromisoformat(post_copy["date"])
            except ValueError:
                post_copy["date"] = stable_fallback_date(post_copy.get("link", ""))
        result.append(post_copy)
    return result


def merge_posts(new_posts, cached_posts):
    existing_links = {p["link"] for p in cached_posts}
    merged = list(cached_posts)

    added_count = 0
    for post in new_posts:
        if post["link"] not in existing_links:
            merged.append(post)
            existing_links.add(post["link"])
            added_count += 1

    logger.info(f"Added {added_count} new posts to cache")
    return sort_posts_for_feed(merged, date_field="date")


def fetch_posts(limit=15, page=1):
    """Fetch posts from the Ghost Content API."""
    params = {
        "key": GHOST_API_KEY,
        "limit": limit,
        "page": page,
        "include": "tags,authors",
        "order": "published_at desc",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; RSS Feed Generator)",
        "Accept": "application/json",
    }

    response = requests.get(GHOST_API_URL, params=params, headers=headers)
    response.raise_for_status()
    return response.json()


def parse_api_posts(api_data):
    """Extract post dicts from Ghost API response."""
    posts = []
    for post in api_data.get("posts", []):
        title = post.get("title", "").strip()
        if not title:
            continue

        slug = post.get("slug", "")
        link = f"https://cohere.com/blog/{slug}"

        # Parse date from ISO 8601
        date = None
        published_at = post.get("published_at")
        if published_at:
            try:
                date = datetime.fromisoformat(published_at)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=pytz.UTC)
            except ValueError:
                logger.warning(f"Could not parse date for: {title}")

        if not date:
            date = stable_fallback_date(link)

        # Extract description
        description = post.get("custom_excerpt") or title

        # Extract category from first tag
        tags = post.get("tags", [])
        category = tags[0]["name"] if tags else "Blog"

        posts.append(
            {
                "title": title,
                "link": link,
                "date": date,
                "description": description,
                "category": category,
            }
        )

    return posts


def fetch_all_posts(max_posts=MAX_POSTS_FULL):
    """Fetch posts from Ghost API, paginating until max_posts reached."""
    all_posts = []
    page = 1
    per_page = min(max_posts, 15)

    while len(all_posts) < max_posts:
        logger.info(f"Fetching page {page} (limit={per_page})...")
        api_data = fetch_posts(limit=per_page, page=page)
        posts = parse_api_posts(api_data)

        if not posts:
            logger.info(f"No posts on page {page}, stopping")
            break

        all_posts.extend(posts)
        logger.info(f"Page {page}: {len(posts)} posts (total: {len(all_posts)})")

        # Check if there are more pages
        pagination = api_data.get("meta", {}).get("pagination", {})
        if not pagination.get("next"):
            logger.info("No more pages available")
            break

        page += 1

    # Trim to max_posts
    all_posts = all_posts[:max_posts]
    logger.info(f"Total posts fetched: {len(all_posts)}")
    return all_posts


def generate_rss_feed(posts):
    try:
        fg = FeedGenerator()
        fg.title("The Cohere Blog")
        fg.description("Latest news, research, and product updates from Cohere")
        fg.language("en")
        fg.author({"name": "Cohere"})
        fg.logo("https://cohere.com/favicon.ico")
        fg.subtitle("Enterprise AI research and product updates from Cohere")

        setup_feed_links(fg, blog_url=BLOG_URL, feed_name=FEED_NAME)

        posts_sorted = sort_posts_for_feed(posts, date_field="date")

        for post in posts_sorted:
            fe = fg.add_entry()
            fe.title(post["title"])
            fe.description(post["description"])
            fe.link(href=post["link"])
            fe.published(post["date"])
            fe.category(term=post["category"])
            fe.id(post["link"])

        logger.info("Successfully generated RSS feed")
        return fg

    except Exception as e:
        logger.error(f"Error generating RSS feed: {str(e)}")
        raise


def save_rss_feed(feed_generator):
    try:
        feeds_dir = get_feeds_dir()
        output_filename = feeds_dir / f"feed_{FEED_NAME}.xml"
        feed_generator.rss_file(str(output_filename), pretty=True)
        logger.info(f"Successfully saved RSS feed to {output_filename}")
        return output_filename
    except Exception as e:
        logger.error(f"Error saving RSS feed: {str(e)}")
        raise


def main(full_reset=False):
    """Main function to generate RSS feed from Cohere's blog.

    Args:
        full_reset: If True, fetch up to 50 posts from the API.
                   If False, fetch 15 posts (page 1) and merge with cache.
    """
    try:
        cache = load_cache()
        cached_posts = deserialize_posts(cache.get("posts", []))

        if full_reset or not cached_posts:
            mode = "full reset" if full_reset else "no cache exists"
            logger.info(f"Running full fetch ({mode})")
            new_posts = fetch_all_posts(max_posts=MAX_POSTS_FULL)
        else:
            logger.info("Running incremental update (page 1 only)")
            api_data = fetch_posts(limit=MAX_POSTS_INCREMENTAL, page=1)
            new_posts = parse_api_posts(api_data)

        logger.info(f"Found {len(new_posts)} posts from API")

        if cached_posts and not full_reset:
            posts = merge_posts(new_posts, cached_posts)
        else:
            posts = sort_posts_for_feed(new_posts, date_field="date")

        if not posts:
            logger.warning("No posts found. Please check the API response.")
            return False

        save_cache(posts)

        feed = generate_rss_feed(posts)
        save_rss_feed(feed)

        logger.info(f"Successfully generated RSS feed with {len(posts)} posts")
        return True

    except Exception as e:
        logger.error(f"Failed to generate RSS feed: {str(e)}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate Cohere Blog RSS feed")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Force full reset (fetch up to 50 posts)",
    )
    args = parser.parse_args()
    main(full_reset=args.full)
