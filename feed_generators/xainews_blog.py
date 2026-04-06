import argparse
import json
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import pytz
import undetected_chromedriver as uc
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from utils import get_cache_dir, get_chrome_major_version, get_feeds_dir, setup_feed_links, sort_posts_for_feed

FEED_NAME = "xainews"
NEWS_URL = "https://x.ai/news"

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
    """Load existing cache or return empty structure."""
    cache_file = get_cache_file()
    if cache_file.exists():
        with open(cache_file, "r") as f:
            data = json.load(f)
            logger.info(
                f"Loaded cache with {len(data.get('articles', []))} articles"
            )
            return data
    logger.info("No cache file found, will do full fetch")
    return {"last_updated": None, "articles": []}


def save_cache(articles):
    """Save articles to cache file."""
    cache_file = get_cache_file()
    serializable = []
    for article in articles:
        article_copy = article.copy()
        if isinstance(article_copy.get("date"), datetime):
            article_copy["date"] = article_copy["date"].isoformat()
        serializable.append(article_copy)

    data = {
        "last_updated": datetime.now(pytz.UTC).isoformat(),
        "articles": serializable,
    }
    with open(cache_file, "w") as f:
        json.dump(data, f, indent=2)
    logger.info(f"Saved cache with {len(articles)} articles to {cache_file}")


def deserialize_articles(articles):
    """Convert cached articles back to proper format with datetime objects."""
    result = []
    for article in articles:
        article_copy = article.copy()
        if isinstance(article_copy.get("date"), str):
            try:
                article_copy["date"] = datetime.fromisoformat(article_copy["date"])
            except ValueError:
                article_copy["date"] = stable_fallback_date(
                    article_copy.get("link", "")
                )
        result.append(article_copy)
    return result


def merge_articles(new_articles, cached_articles):
    """Merge new articles into cache, dedupe by link, sort by date."""
    existing_links = {a["link"] for a in cached_articles}
    merged = list(cached_articles)

    added_count = 0
    for article in new_articles:
        if article["link"] not in existing_links:
            merged.append(article)
            existing_links.add(article["link"])
            added_count += 1

    logger.info(f"Added {added_count} new articles to cache")
    return sort_posts_for_feed(merged, date_field="date")


def setup_selenium_driver():
    """Set up Selenium WebDriver with undetected-chromedriver.

    xAI's site is behind Cloudflare, which blocks plain requests.
    undetected-chromedriver bypasses bot detection.
    """
    options = uc.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--window-size=1920,10000")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
    version = get_chrome_major_version()
    return uc.Chrome(options=options, version_main=version)


def fetch_news_content(url=NEWS_URL):
    """Fetch the fully loaded HTML content of the xAI news page using Selenium.

    The xAI news page is behind Cloudflare and requires a real browser.
    All articles load on the initial page without pagination.
    """
    driver = None
    try:
        logger.info(f"Fetching content from URL: {url}")
        driver = setup_selenium_driver()
        driver.get(url)

        # Wait for initial page load
        wait_time = 5
        logger.info(f"Waiting {wait_time} seconds for the page to fully load...")
        time.sleep(wait_time)

        # Wait for article containers to be present
        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "div.group.relative")
                )
            )
            logger.info("Article containers loaded successfully")
        except Exception:
            logger.warning(
                "Could not confirm articles loaded, proceeding anyway..."
            )

        # Scroll to bottom to trigger any lazy loading
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)

        html_content = driver.page_source
        logger.info("Successfully fetched HTML content")
        return html_content

    except Exception as e:
        logger.error(f"Error fetching content: {e}")
        raise
    finally:
        if driver:
            driver.quit()


def parse_date(date_text):
    """Parse date from various formats used on xAI news page."""
    date_formats = [
        "%B %d, %Y",  # September 19, 2025
        "%b %d, %Y",  # Sep 19, 2025
        "%B %d %Y",
        "%b %d %Y",
        "%Y-%m-%d",
        "%m/%d/%Y",
    ]

    date_text = date_text.strip()
    for date_format in date_formats:
        try:
            date = datetime.strptime(date_text, date_format)
            return date.replace(tzinfo=pytz.UTC)
        except ValueError:
            continue

    logger.warning(f"Could not parse date: {date_text}")
    return None


def extract_articles(soup):
    """Extract article information from the parsed HTML."""
    articles = []
    seen_links = set()

    # Find all article containers
    article_containers = soup.select("div.group.relative")

    logger.info(f"Found {len(article_containers)} potential article containers")

    for container in article_containers:
        try:
            # Extract the link and title
            title_link = container.select_one('a[href*="/news/"]')
            if not title_link:
                continue

            href = title_link.get("href", "")
            if not href:
                continue

            # Build full URL
            link = f"https://x.ai{href}" if href.startswith("/") else href

            # Skip duplicates
            if link in seen_links:
                continue

            # Skip the main news page link
            if link.endswith("/news") or link.endswith("/news/"):
                continue

            seen_links.add(link)

            # Extract title - can be in h3 or h4
            title_elem = title_link.select_one("h3, h4")
            if not title_elem:
                logger.debug(f"Could not extract title for link: {link}")
                continue

            title = title_elem.text.strip()

            # Extract description
            description_elem = container.select_one("p.text-secondary")
            description = (
                description_elem.text.strip() if description_elem else title
            )

            # Extract date - try multiple selectors
            date = None

            # First try: p.mono-tag.text-xs.leading-6 (featured article format)
            date_elem = container.select_one("p.mono-tag.text-xs.leading-6")
            if date_elem:
                date_text = date_elem.text.strip()
                if any(
                    month in date_text
                    for month in [
                        "January",
                        "February",
                        "March",
                        "April",
                        "May",
                        "June",
                        "July",
                        "August",
                        "September",
                        "October",
                        "November",
                        "December",
                    ]
                ):
                    date = parse_date(date_text)

            # Second try: span.mono-tag.text-xs in footer (grid article format)
            if not date:
                footer_elements = container.select(
                    "div.flex.items-center.justify-between span.mono-tag.text-xs"
                )
                for elem in footer_elements:
                    text = elem.text.strip()
                    if any(
                        month in text
                        for month in [
                            "January",
                            "February",
                            "March",
                            "April",
                            "May",
                            "June",
                            "July",
                            "August",
                            "September",
                            "October",
                            "November",
                            "December",
                        ]
                    ):
                        date = parse_date(text)
                        break

            # Fallback: use stable date if we couldn't extract one
            if not date:
                logger.warning(f"Could not extract date for article: {title}")
                date = stable_fallback_date(link)

            # Extract category
            category = "News"
            category_elem = container.select_one(
                "div:not(.flex.items-center.justify-between) span.mono-tag.text-xs"
            )
            if category_elem:
                category_text = category_elem.text.strip().lower()
                # Skip if it's a date
                if not any(
                    month.lower() in category_text
                    for month in [
                        "january",
                        "february",
                        "march",
                        "april",
                        "may",
                        "june",
                        "july",
                        "august",
                        "september",
                        "october",
                        "november",
                        "december",
                    ]
                ):
                    category = category_text.capitalize()

            article = {
                "title": title,
                "link": link,
                "date": date,
                "category": category,
                "description": description,
            }

            articles.append(article)
            logger.debug(f"Extracted article: {title} ({date})")

        except Exception as e:
            logger.warning(f"Error parsing article container: {str(e)}")
            continue

    logger.info(f"Successfully parsed {len(articles)} articles")
    return articles


def parse_news_html(html_content):
    """Parse the news HTML content and extract article information."""
    try:
        soup = BeautifulSoup(html_content, "html.parser")
        return extract_articles(soup)
    except Exception as e:
        logger.error(f"Error parsing HTML content: {str(e)}")
        raise


def generate_rss_feed(articles):
    """Generate RSS feed from news articles."""
    try:
        fg = FeedGenerator()
        fg.title("xAI News")
        fg.description("Latest news and updates from xAI")
        fg.language("en")
        fg.author({"name": "xAI"})
        fg.subtitle("Latest updates from xAI")

        setup_feed_links(fg, blog_url=NEWS_URL, feed_name=FEED_NAME)

        articles_sorted = sort_posts_for_feed(articles, date_field="date")

        for article in articles_sorted:
            fe = fg.add_entry()
            fe.title(article["title"])
            fe.description(article["description"])
            fe.link(href=article["link"])
            fe.published(article["date"])
            fe.category(term=article["category"])
            fe.id(article["link"])

        logger.info("Successfully generated RSS feed")
        return fg

    except Exception as e:
        logger.error(f"Error generating RSS feed: {str(e)}")
        raise


def save_rss_feed(feed_generator):
    """Save the RSS feed to a file in the feeds directory."""
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
    """Main function to generate RSS feed from xAI's news page.

    Args:
        full_reset: If True, ignore cache and do a fresh fetch.
                   If False, merge new articles with cached ones.
    """
    try:
        cache = load_cache()
        cached_articles = deserialize_articles(cache.get("articles", []))

        if full_reset or not cached_articles:
            mode = "full reset" if full_reset else "no cache exists"
            logger.info(f"Running full fetch ({mode})")
        else:
            logger.info("Running incremental update")

        html_content = fetch_news_content()
        new_articles = parse_news_html(html_content)
        logger.info(f"Found {len(new_articles)} articles from page")

        if cached_articles and not full_reset:
            articles = merge_articles(new_articles, cached_articles)
        else:
            articles = sort_posts_for_feed(new_articles, date_field="date")

        if not articles:
            logger.warning("No articles found. Please check the HTML structure.")
            return False

        save_cache(articles)

        feed = generate_rss_feed(articles)
        save_rss_feed(feed)

        logger.info(
            f"Successfully generated RSS feed with {len(articles)} articles"
        )
        return True

    except Exception as e:
        logger.error(f"Failed to generate RSS feed: {str(e)}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate xAI News RSS feed"
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Force full reset (ignore cache)",
    )
    args = parser.parse_args()
    main(full_reset=args.full)
