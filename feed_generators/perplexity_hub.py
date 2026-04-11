import argparse
import json
import logging
import re
import time
from datetime import datetime, timedelta
import pytz
import undetected_chromedriver as uc
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from utils import get_cache_dir, get_chrome_major_version, get_feeds_dir, setup_feed_links, sort_posts_for_feed

FEED_NAME = "perplexity_hub"
BLOG_URL = "https://www.perplexity.ai/hub"

# Set up logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def stable_fallback_date(identifier):
    """Generate a stable date from a URL or title hash.

    Prevents RSS readers from seeing entries as 'new' when date
    extraction fails intermittently.
    """
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
            logger.info(f"Loaded cache with {len(data.get('articles', []))} articles")
            return data
    logger.info("No cache file found, will do full fetch")
    return {"last_updated": None, "articles": []}


def save_cache(articles):
    """Save articles to cache file."""
    cache_file = get_cache_file()
    serializable_articles = []
    for article in articles:
        article_copy = article.copy()
        if isinstance(article_copy.get("date"), datetime):
            article_copy["date"] = article_copy["date"].isoformat()
        serializable_articles.append(article_copy)

    data = {
        "last_updated": datetime.now(pytz.UTC).isoformat(),
        "articles": serializable_articles,
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
    """Merge new articles into cache, dedupe by link, sort by date desc."""
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


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)


def setup_selenium_driver():
    """Set up Selenium WebDriver with undetected-chromedriver.

    Forces English content via Accept-Language HTTP header (CDP).
    Perplexity geo-redirects to localized URLs based on request headers.
    """
    options = uc.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--window-size=1920,10000")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--lang=en-US")
    options.add_argument(f"--user-agent={USER_AGENT}")
    version = get_chrome_major_version()
    driver = uc.Chrome(options=options, version_main=version)
    # Set Accept-Language HTTP header via CDP. This is what the server
    # actually checks for locale routing (not --lang or setLocaleOverride).
    driver.execute_cdp_cmd("Network.enable", {})
    driver.execute_cdp_cmd(
        "Network.setUserAgentOverride",
        {
            "userAgent": USER_AGENT,
            "acceptLanguage": "en-US,en;q=0.9",
        },
    )
    return driver


def fetch_hub_content(url=BLOG_URL):
    """Fetch the fully loaded HTML content of the Perplexity Hub using Selenium.

    The Perplexity Hub is built with Framer and renders client-side.
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

        # Wait for blog article links to be present
        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, 'a[href*="/hub/blog/"]')
                )
            )
            logger.info("Blog articles loaded successfully")
        except Exception:
            logger.warning("Could not confirm articles loaded, proceeding anyway...")

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


def parse_hub_html(html_content):
    """Parse the Perplexity Hub HTML and extract article information.

    The hub uses Framer with this structure:
    - Hero card: <a href="./hub/blog/..."> with <h4> title, no <time>
    - Article cards: <a data-framer-name="Article Card" href="./hub/blog/...">
      containing <h6> title, <time datetime="..."> date, and <p> tags for
      date text and category labels.
    """
    try:
        soup = BeautifulSoup(html_content, "html.parser")
        articles = []
        seen_links = set()

        # Find all links that point to blog articles
        all_blog_links = soup.select('a[href*="/hub/blog/"]')
        logger.info(f"Found {len(all_blog_links)} potential blog article links")

        for card in all_blog_links:
            href = card.get("href", "")
            if not href:
                continue

            # Build full URL - handle relative paths and strip locale prefixes
            if href.startswith("./"):
                link = f"https://www.perplexity.ai/{href[2:]}"
            elif href.startswith("/"):
                link = f"https://www.perplexity.ai{href}"
            elif href.startswith("http"):
                link = href
            else:
                link = f"https://www.perplexity.ai/{href}"

            # Strip locale prefix from URL to get canonical path
            # e.g., /de/hub/blog/... -> /hub/blog/...
            link = re.sub(
                r"(perplexity\.ai)/[a-z]{2}/hub/",
                r"\1/hub/",
                link,
            )

            # Skip duplicates
            if link in seen_links:
                continue
            seen_links.add(link)

            # Extract title from heading tags (h4 for hero, h6 for cards)
            title = None
            for tag in ["h4", "h6", "h3", "h2", "h5"]:
                elem = card.select_one(tag)
                if elem and elem.text.strip():
                    title = elem.text.strip()
                    break

            # Fallback: use link text, but clean it
            if not title:
                text = card.get_text(strip=True)
                if text and len(text) > 5:
                    # Remove date-like strings and "Read more" / "Lesen Sie mehr"
                    title = text[:150]
                else:
                    logger.debug(f"Could not extract title for link: {link}")
                    continue

            # Extract date from <time> element
            date = None
            time_elem = card.select_one("time")
            if time_elem:
                datetime_attr = time_elem.get("datetime")
                if datetime_attr:
                    try:
                        date = datetime.fromisoformat(
                            datetime_attr.replace("Z", "+00:00")
                        )
                        if date.tzinfo is None:
                            date = date.replace(tzinfo=pytz.UTC)
                    except ValueError:
                        pass

            # Fallback date from text content
            if not date:
                date = stable_fallback_date(link)

            # Extract category from paragraph tags.
            # Cards have <p> tags for date ("Apr 2, 2026") and category ("Company").
            # Skip paragraphs that look like dates (contain digits + month names).
            category = "Blog"
            date_patterns = re.compile(
                r"\d|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
                r"|Januar|Februar|März|April|Mai|Juni|Juli|August"
                r"|September|Oktober|November|Dezember"
            )
            paragraphs = card.select("p")
            for p in paragraphs:
                text = p.text.strip()
                if len(text) < 3 or len(text) > 30:
                    continue
                if date_patterns.search(text):
                    continue
                category = text
                break

            article = {
                "title": title,
                "link": link,
                "date": date,
                "category": category,
                "description": title,
            }

            if validate_article(article):
                articles.append(article)

        logger.info(f"Successfully parsed {len(articles)} valid articles")
        return articles

    except Exception as e:
        logger.error(f"Error parsing HTML content: {str(e)}")
        raise


def validate_article(article):
    """Validate that article has all required fields with reasonable values."""
    if not article.get("title") or len(article["title"]) < 5:
        logger.warning(f"Invalid title for article: {article.get('link', 'unknown')}")
        return False

    if not article.get("link") or not article["link"].startswith("http"):
        logger.warning(f"Invalid link for article: {article.get('title', 'unknown')}")
        return False

    if not article.get("date"):
        logger.warning(f"Missing date for article: {article.get('title', 'unknown')}")
        return False

    return True


def generate_rss_feed(articles):
    """Generate RSS feed from blog articles."""
    try:
        fg = FeedGenerator()
        fg.title("Perplexity Blog")
        fg.description("Latest news, updates, and research from Perplexity AI")
        fg.language("en")
        fg.author({"name": "Perplexity AI"})
        fg.logo("https://www.perplexity.ai/favicon.ico")
        fg.subtitle("Updates from Perplexity AI")

        setup_feed_links(fg, blog_url=BLOG_URL, feed_name=FEED_NAME)

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
    """Main function to generate RSS feed from Perplexity's hub page.

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

        html_content = fetch_hub_content()
        new_articles = parse_hub_html(html_content)
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

        logger.info(f"Successfully generated RSS feed with {len(articles)} articles")
        return True

    except Exception as e:
        logger.error(f"Failed to generate RSS feed: {str(e)}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate Perplexity Hub RSS feed"
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Force full reset (ignore cache)",
    )
    args = parser.parse_args()
    main(full_reset=args.full)
