import argparse
import json
import logging
import time
from datetime import datetime, timedelta

import pytz
import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator

from utils import get_cache_dir, get_feeds_dir, setup_feed_links, sort_posts_for_feed

FEED_NAME = "ai_first_podcast"
BLOG_URL = "https://ai-first.ai/podcast"
BASE_URL = "https://ai-first.ai"

# Set up logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
}


def stable_fallback_date(identifier):
    """Generate a stable date from a URL or title hash."""
    hash_val = abs(hash(identifier)) % 730
    epoch = datetime(2023, 1, 1, 0, 0, 0, tzinfo=pytz.UTC)
    return epoch + timedelta(days=hash_val)


def get_cache_file():
    """Get the cache file path."""
    return get_cache_dir() / f"{FEED_NAME}_posts.json"


def load_cache():
    """Load existing cache or return empty structure."""
    cache_file = get_cache_file()
    if cache_file.exists():
        with open(cache_file, "r") as f:
            data = json.load(f)
            logger.info(f"Loaded cache with {len(data.get('episodes', []))} episodes")
            return data
    logger.info("No cache file found, will do full fetch")
    return {"last_updated": None, "episodes": []}


def save_cache(episodes):
    """Save episodes to cache file."""
    cache_file = get_cache_file()
    serializable = []
    for ep in episodes:
        ep_copy = ep.copy()
        if isinstance(ep_copy.get("date"), datetime):
            ep_copy["date"] = ep_copy["date"].isoformat()
        serializable.append(ep_copy)

    data = {
        "last_updated": datetime.now(pytz.UTC).isoformat(),
        "episodes": serializable,
    }
    with open(cache_file, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.info(f"Saved cache with {len(episodes)} episodes to {cache_file}")


def deserialize_episodes(episodes):
    """Convert cached episodes back to proper format with datetime objects."""
    result = []
    for ep in episodes:
        ep_copy = ep.copy()
        if isinstance(ep_copy.get("date"), str):
            try:
                ep_copy["date"] = datetime.fromisoformat(ep_copy["date"])
            except ValueError:
                ep_copy["date"] = stable_fallback_date(ep_copy.get("link", ""))
        result.append(ep_copy)
    return result


def fetch_listing_page():
    """Fetch the podcast listing page and extract episode links + titles."""
    logger.info(f"Fetching listing page: {BLOG_URL}")
    response = requests.get(BLOG_URL, headers=HEADERS, timeout=15)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    episodes = []
    seen_hrefs = set()

    for link in soup.select('a[href^="/podcast/"]'):
        href = link.get("href", "")
        if href in ("/podcast", "/podcast/") or href in seen_hrefs:
            continue
        seen_hrefs.add(href)

        # Title from aria-label (e.g., "Podcast: KI bei BASF")
        aria = link.get("aria-label", "")
        title = aria.removeprefix("Podcast: ").strip() if aria else None

        if not title:
            # Fallback: text content
            text = link.get_text(strip=True)
            if text and len(text) > 5:
                title = text[:200]

        if not title:
            continue

        episodes.append({
            "link": f"{BASE_URL}{href}",
            "title": title,
        })

    logger.info(f"Found {len(episodes)} episode links on listing page")
    return episodes


def fetch_episode_details(url):
    """Fetch an individual episode page and extract date + description from JSON-LD."""
    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        # Extract from JSON-LD PodcastEpisode schema
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.string)
            except (json.JSONDecodeError, TypeError):
                continue

            if data.get("@type") != "PodcastEpisode":
                continue

            date = None
            date_str = data.get("datePublished")
            if date_str:
                try:
                    date = datetime.fromisoformat(date_str)
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=pytz.UTC)
                except ValueError:
                    pass

            description = data.get("description", "")
            return {"date": date, "description": description}

        # Fallback: try <time> element
        time_elem = soup.select_one("time[datetime]")
        if time_elem:
            try:
                date = datetime.fromisoformat(
                    time_elem["datetime"].replace("Z", "+00:00")
                )
                if date.tzinfo is None:
                    date = date.replace(tzinfo=pytz.UTC)
                return {"date": date, "description": ""}
            except (ValueError, KeyError):
                pass

        return {"date": None, "description": ""}

    except requests.RequestException as e:
        logger.warning(f"Failed to fetch episode page {url}: {e}")
        return {"date": None, "description": ""}


def main(full_reset=False):
    """Main function to generate RSS feed from AI FIRST Podcast."""
    try:
        cache = load_cache()
        cached_episodes = deserialize_episodes(cache.get("episodes", []))
        cached_links = {ep["link"] for ep in cached_episodes}

        # Fetch listing page for all episode links
        listing_episodes = fetch_listing_page()

        if not listing_episodes:
            logger.warning("No episodes found on listing page.")
            return False

        # Determine which episodes need detail fetching
        if full_reset:
            to_fetch = listing_episodes
            logger.info(f"Full reset: fetching details for all {len(to_fetch)} episodes")
        else:
            to_fetch = [ep for ep in listing_episodes if ep["link"] not in cached_links]
            logger.info(f"Incremental: {len(to_fetch)} new episodes to fetch")

        # Fetch details for new episodes
        new_episodes = []
        for i, ep in enumerate(to_fetch):
            details = fetch_episode_details(ep["link"])
            date = details["date"] or stable_fallback_date(ep["link"])
            description = details["description"] or ep["title"]

            new_episodes.append({
                "title": ep["title"],
                "link": ep["link"],
                "date": date,
                "description": description,
            })

            if i < len(to_fetch) - 1:
                time.sleep(0.5)  # Be polite

            if (i + 1) % 10 == 0:
                logger.info(f"Fetched {i + 1}/{len(to_fetch)} episode details")

        # Merge with cache
        if full_reset:
            all_episodes = new_episodes
        else:
            existing_links = {ep["link"] for ep in cached_episodes}
            all_episodes = list(cached_episodes)
            added = 0
            for ep in new_episodes:
                if ep["link"] not in existing_links:
                    all_episodes.append(ep)
                    existing_links.add(ep["link"])
                    added += 1
            logger.info(f"Added {added} new episodes to cache")

        all_episodes = sort_posts_for_feed(all_episodes, date_field="date")

        # Save cache
        save_cache(all_episodes)

        # Generate feed
        fg = FeedGenerator()
        fg.title("AI FIRST Podcast")
        fg.description(
            "Der AI FIRST Podcast: Erfahre jeden Freitag aus erster Hand, "
            "wie Unternehmer und Führungskräfte AI einsetzen."
        )
        fg.language("de")
        fg.author({"name": "AI FIRST"})
        fg.logo("https://ai-first.ai/images/og/og-default.png")
        fg.subtitle("KI-Transformation, Produktivität und die Zukunft der Arbeit")

        setup_feed_links(fg, blog_url=BLOG_URL, feed_name=FEED_NAME)

        for ep in all_episodes:
            fe = fg.add_entry()
            fe.title(ep["title"])
            fe.description(ep["description"])
            fe.link(href=ep["link"])
            fe.published(ep["date"])
            fe.id(ep["link"])

        feeds_dir = get_feeds_dir()
        output_file = feeds_dir / f"feed_{FEED_NAME}.xml"
        fg.rss_file(str(output_file), pretty=True)
        logger.info(f"Saved RSS feed to {output_file} with {len(all_episodes)} episodes")

        return True

    except Exception as e:
        logger.error(f"Failed to generate RSS feed: {str(e)}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate AI FIRST Podcast RSS feed"
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Force full reset (fetch all episode details)",
    )
    args = parser.parse_args()
    main(full_reset=args.full)
