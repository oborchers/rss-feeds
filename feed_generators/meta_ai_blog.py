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

FEED_NAME = "meta_ai"
BLOG_URL = "https://ai.meta.com/blog/"

# Set up logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

DATE_PATTERN = re.compile(
    r"(January|February|March|April|May|June|July|August"
    r"|September|October|November|December)\s+\d{1,2},\s+\d{4}"
)

CATEGORIES = {
    "featured",
    "ml applications",
    "open source",
    "research",
    "computer vision",
    "hardware",
    "natural language processing",
    "generative ai",
}


def stable_fallback_date(identifier):
    """Generate a stable date from a URL or title hash."""
    hash_val = abs(hash(identifier)) % 730
    epoch = datetime(2023, 1, 1, 0, 0, 0, tzinfo=pytz.UTC)
    return epoch + timedelta(days=hash_val)


def get_cache_file():
    return get_cache_dir() / f"{FEED_NAME}_posts.json"


def load_cache():
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
    """Set up Selenium WebDriver with undetected-chromedriver."""
    options = uc.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
    version = get_chrome_major_version()
    return uc.Chrome(options=options, version_main=version)


def fetch_blog_content(url=BLOG_URL, max_clicks=20):
    """Fetch the fully loaded HTML of the Meta AI blog using Selenium.

    The blog is a React SPA that loads articles dynamically.
    A "Load more" button must be clicked repeatedly to get all articles.

    Args:
        url: Blog URL
        max_clicks: Maximum "Load more" clicks. Use 20 for full fetch, 2-3 for incremental.
    """
    driver = None
    try:
        logger.info(f"Fetching content from URL: {url} (max_clicks={max_clicks})")
        driver = setup_selenium_driver()
        driver.get(url)

        wait_time = 5
        logger.info(f"Waiting {wait_time} seconds for the page to fully load...")
        time.sleep(wait_time)

        # Wait for article links to appear
        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, 'a[href*="/blog/"]')
                )
            )
            logger.info("Blog articles loaded successfully")
        except Exception:
            logger.warning(
                "Could not confirm articles loaded, proceeding anyway..."
            )

        # Click "Load more" button repeatedly
        clicks = 0
        while clicks < max_clicks:
            try:
                load_more = None
                # Try CSS selector for the known button class
                try:
                    load_more = driver.find_element(By.CSS_SELECTOR, "button._amto")
                    if not load_more.is_displayed():
                        load_more = None
                except Exception:
                    pass

                # Fallback: find by text
                if not load_more:
                    try:
                        load_more = driver.find_element(
                            By.XPATH,
                            "//button[contains(text(), 'Load more')]",
                        )
                    except Exception:
                        pass

                if load_more and load_more.is_displayed():
                    logger.info(
                        f"Clicking 'Load more' button (click {clicks + 1})..."
                    )
                    driver.execute_script("arguments[0].click();", load_more)
                    clicks += 1
                    time.sleep(2)
                else:
                    logger.info(
                        f"No more 'Load more' button after {clicks} clicks"
                    )
                    break
            except Exception as e:
                logger.info(
                    f"No more 'Load more' button after {clicks} clicks: {e}"
                )
                break

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
    """Parse date from 'Month DD, YYYY' format."""
    date_text = date_text.strip()
    try:
        date = datetime.strptime(date_text, "%B %d, %Y")
        return date.replace(tzinfo=pytz.UTC)
    except ValueError:
        logger.warning(f"Could not parse date: {date_text}")
        return None


def extract_articles(soup):
    """Extract article information from the parsed HTML.

    The Meta AI blog has two card layouts:
    1. "Latest News" section: div._amda containers with div._amde (title),
       div._amdj (category + date), and aria-label on links.
    2. "More from AI at Meta" grid: div._amsu containers with p._amt0 (category),
       p._amt2 (title), p._amt3 (description), p._amt4 (date).

    Both layouts contain a[href*="/blog/"] links.
    """
    articles = []
    seen_links = set()

    # --- Section 1: Featured hero ---
    hero = soup.select_one("div._amcy")
    if hero:
        link = hero.find("a", href=True)
        if link:
            href = link.get("href", "")
            if href.startswith("/"):
                href = f"https://ai.meta.com{href}"
            if href not in seen_links:
                seen_links.add(href)
                title_elem = hero.find("div", class_="_amd1")
                cat_elem = hero.find("div", class_="_amug")
                date_elem = hero.find("div", class_="_amdj")

                title = title_elem.get_text(strip=True) if title_elem else None
                # Fallback: aria-label
                if not title:
                    aria = link.get("aria-label", "")
                    if aria.startswith("Read "):
                        title = aria[5:]

                if title:
                    date = None
                    if date_elem:
                        date_match = DATE_PATTERN.search(date_elem.get_text())
                        if date_match:
                            date = parse_date(date_match.group())
                    if not date:
                        date = stable_fallback_date(href)

                    category = cat_elem.get_text(strip=True) if cat_elem else "AI"

                    articles.append({
                        "title": title,
                        "link": href,
                        "date": date,
                        "category": category,
                        "description": title,
                    })

    # --- Section 2: Latest News cards (div._amda) ---
    for card in soup.select("div._amda"):
        link = card.find("a", href=True)
        if not link:
            continue
        href = link.get("href", "")
        if href.startswith("/"):
            href = f"https://ai.meta.com{href}"
        if href in seen_links or href in ("/blog/", "/blog"):
            continue
        seen_links.add(href)

        # Title from div._amde or aria-label
        title = None
        title_elem = card.find("div", class_="_amde")
        if title_elem:
            title = title_elem.get_text(strip=True)
        if not title:
            aria = link.get("aria-label", "")
            if aria.startswith("Read "):
                title = aria[5:]
        if not title:
            continue

        # Category and date from div._amdj (first = category, last = date)
        amdj_elems = card.select("div._amdj")
        category = "AI"
        date = None
        for elem in amdj_elems:
            text = elem.get_text(strip=True)
            date_match = DATE_PATTERN.search(text)
            if date_match:
                date = parse_date(date_match.group())
            elif text.lower() in CATEGORIES:
                category = text

        # Also try short date format "Mar 27, 2026"
        if not date:
            for elem in amdj_elems:
                text = elem.get_text(strip=True)
                try:
                    date = datetime.strptime(text, "%b %d, %Y").replace(
                        tzinfo=pytz.UTC
                    )
                except ValueError:
                    pass

        if not date:
            date = stable_fallback_date(href)

        # Description
        description = title
        desc_elem = card.find("p", class_="text-secondary") or card.find(
            "p", class_="_amt3"
        )
        if desc_elem:
            description = desc_elem.get_text(strip=True)[:300]

        articles.append({
            "title": title,
            "link": href,
            "date": date,
            "category": category,
            "description": description,
        })

    # --- Section 3: "More from AI at Meta" grid (div._amsu) ---
    for card in soup.select("div._amsu"):
        link = card.find("a", href=True)
        if not link:
            continue
        href = link.get("href", "")
        if href.startswith("/"):
            href = f"https://ai.meta.com{href}"
        if href in seen_links or href in ("/blog/", "/blog"):
            continue
        seen_links.add(href)

        # Title from p._amt2
        title_elem = card.find("p", class_="_amt2")
        title = title_elem.get_text(strip=True) if title_elem else None
        if not title:
            continue

        # Category from p._amt0
        cat_elem = card.find("p", class_="_amt0")
        category = cat_elem.get_text(strip=True) if cat_elem else "AI"

        # Date from p._amt4
        date = None
        date_elem = card.find("p", class_="_amt4")
        if date_elem:
            date_text = date_elem.get_text(strip=True)
            date_match = DATE_PATTERN.search(date_text)
            if date_match:
                date = parse_date(date_match.group())
            else:
                # Try short format "Mar 27, 2026"
                try:
                    date = datetime.strptime(date_text, "%b %d, %Y").replace(
                        tzinfo=pytz.UTC
                    )
                except ValueError:
                    pass
        if not date:
            date = stable_fallback_date(href)

        # Description from p._amt3
        desc_elem = card.find("p", class_="_amt3")
        description = (
            desc_elem.get_text(strip=True)[:300] if desc_elem else title
        )

        articles.append({
            "title": title,
            "link": href,
            "date": date,
            "category": category,
            "description": description,
        })

    logger.info(f"Successfully parsed {len(articles)} articles")
    return articles


def parse_blog_html(html_content):
    try:
        soup = BeautifulSoup(html_content, "html.parser")
        return extract_articles(soup)
    except Exception as e:
        logger.error(f"Error parsing HTML content: {str(e)}")
        raise


def generate_rss_feed(articles):
    try:
        fg = FeedGenerator()
        fg.title("AI at Meta Blog")
        fg.description("Latest AI news and research from Meta")
        fg.language("en")
        fg.author({"name": "Meta AI"})
        fg.logo("https://ai.meta.com/static-resource/2222277787986997/")
        fg.subtitle("AI research, open source, and applications from Meta")

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
    """Main function to generate RSS feed from Meta AI's blog.

    Args:
        full_reset: If True, click "Load more" up to 20 times for full archive.
                   If False, click 2-3 times for recent articles and merge with cache.
    """
    try:
        cache = load_cache()
        cached_articles = deserialize_articles(cache.get("articles", []))

        if full_reset or not cached_articles:
            mode = "full reset" if full_reset else "no cache exists"
            logger.info(f"Running full fetch ({mode})")
            html_content = fetch_blog_content(max_clicks=20)
            new_articles = parse_blog_html(html_content)
        else:
            logger.info("Running incremental update (3 clicks only)")
            html_content = fetch_blog_content(max_clicks=3)
            new_articles = parse_blog_html(html_content)

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
        description="Generate Meta AI Blog RSS feed"
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Force full reset (click Load more up to 20 times)",
    )
    args = parser.parse_args()
    main(full_reset=args.full)
