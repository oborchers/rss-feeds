import argparse
import json
import logging
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

FEED_NAME = "mistral"
BLOG_URL = "https://mistral.ai/news"
MAX_PAGES_FULL = 6
MAX_PAGES_INCREMENTAL = 1

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
    return get_cache_dir() / f"{FEED_NAME}_posts.json"


def load_cache():
    cache_file = get_cache_file()
    if cache_file.exists():
        with open(cache_file, "r") as f:
            data = json.load(f)
            logger.info(f"Loaded cache with {len(data.get('articles', []))} articles")
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


def parse_page_articles(html):
    """Extract articles from a single page's HTML.

    Mistral news cards are wrapped in <a class="group" href="/news/[slug]">
    containing an <article> element. Page 1 has a hero card with <h1>,
    subsequent pages have grid cards with <h2>.
    """
    soup = BeautifulSoup(html, "html.parser")
    articles = []
    seen_links = set()

    # Find all article links pointing to /news/ subpages
    news_links = soup.select('a[href^="/news/"]')
    logger.info(f"Found {len(news_links)} potential news links on page")

    for card in news_links:
        href = card.get("href", "")
        if not href or href in ("/news", "/news/"):
            continue

        link = f"https://mistral.ai{href}"
        if link in seen_links:
            continue

        # Only process cards that contain an <article> element
        article_elem = card.find("article")
        if not article_elem:
            continue

        seen_links.add(link)

        # Title: <h1> for hero card, <h2> for grid cards
        title_elem = article_elem.find("h1") or article_elem.find("h2")
        if not title_elem:
            continue
        title = title_elem.get_text(strip=True)
        if not title or len(title) < 3:
            continue

        # Category: <span> with rounded-full + border classes
        category = "News"
        for span in article_elem.find_all("span"):
            classes = " ".join(span.get("class", []))
            if "rounded-full" in classes and "border" in classes:
                cat_text = span.get_text(strip=True)
                if cat_text:
                    category = cat_text
                break

        # Description: <p> with opacity or color modifier classes
        description = title
        for p in article_elem.find_all("p"):
            classes = " ".join(p.get("class", []))
            if "opacity" in classes or "text-black/50" in classes:
                desc_text = p.get_text(strip=True)
                if desc_text:
                    description = desc_text[:300]
                break

        # Date: from footer area, format "Mar 31, 2026"
        date = None
        for div in article_elem.find_all("div"):
            classes = " ".join(div.get("class", []))
            if "text-sm" in classes:
                date_text = div.get_text(strip=True)
                # Try short month format first
                try:
                    date = datetime.strptime(date_text, "%b %d, %Y")
                    date = date.replace(tzinfo=pytz.UTC)
                    break
                except ValueError:
                    pass
                # Try long month format
                try:
                    date = datetime.strptime(date_text, "%B %d, %Y")
                    date = date.replace(tzinfo=pytz.UTC)
                    break
                except ValueError:
                    continue

        if not date:
            logger.warning(f"Could not parse date for article: {title}")
            date = stable_fallback_date(link)

        articles.append(
            {
                "title": title,
                "link": link,
                "date": date,
                "category": category,
                "description": description,
            }
        )

    logger.info(f"Parsed {len(articles)} articles from page")
    return articles


def fetch_all_articles(max_pages=MAX_PAGES_FULL):
    """Fetch articles across multiple pages using Selenium.

    Unlike "Load more" patterns that append content, Mistral uses numbered
    pagination that replaces content on each page. We extract articles
    from each page before clicking "next".

    Args:
        max_pages: Maximum number of pages to fetch.
    """
    driver = None
    all_articles = []
    seen_links = set()

    try:
        logger.info(f"Fetching articles from {BLOG_URL} (max_pages={max_pages})")
        driver = setup_selenium_driver()
        driver.get(BLOG_URL)

        wait_time = 5
        logger.info(f"Waiting {wait_time} seconds for the page to fully load...")
        time.sleep(wait_time)

        # Wait for article links to appear
        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'a[href^="/news/"]'))
            )
            logger.info("News articles loaded successfully")
        except Exception:
            logger.warning("Could not confirm articles loaded, proceeding anyway...")

        for page_num in range(1, max_pages + 1):
            logger.info(f"Extracting articles from page {page_num}...")
            html = driver.page_source
            page_articles = parse_page_articles(html)

            # Dedupe against previously seen articles
            new_count = 0
            for article in page_articles:
                if article["link"] not in seen_links:
                    all_articles.append(article)
                    seen_links.add(article["link"])
                    new_count += 1
            logger.info(
                f"Page {page_num}: {new_count} new articles "
                f"(total: {len(all_articles)})"
            )

            # Stop if this is the last requested page
            if page_num >= max_pages:
                break

            # Click "next" arrow button to go to next page
            try:
                # Find the pagination container and the next (right arrow) button
                # The next button is after the numbered buttons
                next_btn = None

                # Try finding the right arrow button by its position
                # Pagination structure: [<-] [1] [2] ... [8] [->]
                # The next button contains an SVG arrow pointing right
                pagination_buttons = driver.find_elements(
                    By.CSS_SELECTOR,
                    "button.size-8, button[class*='size-8']",
                )

                if pagination_buttons:
                    # The last button in the pagination row is the "next" arrow
                    candidate = pagination_buttons[-1]
                    # Verify it's not a numbered page button (should contain SVG)
                    try:
                        candidate.find_element(By.TAG_NAME, "svg")
                        next_btn = candidate
                    except Exception:
                        pass

                # Fallback: find by XPath - button with SVG after the last number
                if not next_btn:
                    try:
                        next_btn = driver.find_element(
                            By.XPATH,
                            "//button[contains(@class, 'size-8')][last()]",
                        )
                    except Exception:
                        pass

                if next_btn and next_btn.is_displayed():
                    logger.info(f"Clicking next button to page {page_num + 1}...")
                    driver.execute_script("arguments[0].click();", next_btn)
                    time.sleep(3)

                    # Wait for new content to load
                    try:
                        WebDriverWait(driver, 10).until(
                            EC.presence_of_element_located(
                                (By.CSS_SELECTOR, 'a[href^="/news/"]')
                            )
                        )
                    except Exception:
                        logger.warning("Timeout waiting for next page content")
                else:
                    logger.info(f"No next button found after page {page_num}")
                    break

            except Exception as e:
                logger.info(
                    f"Could not navigate to next page after page {page_num}: {e}"
                )
                break

        logger.info(f"Total articles fetched: {len(all_articles)}")
        return all_articles

    except Exception as e:
        logger.error(f"Error fetching content: {e}")
        raise
    finally:
        if driver:
            driver.quit()


def generate_rss_feed(articles):
    try:
        fg = FeedGenerator()
        fg.title("Mistral AI News")
        fg.description("Latest news and updates from Mistral AI")
        fg.language("en")
        fg.author({"name": "Mistral AI"})
        fg.logo("https://mistral.ai/favicon.ico")
        fg.subtitle("News, research, and product updates from Mistral AI")

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
    """Main function to generate RSS feed from Mistral AI's news page.

    Args:
        full_reset: If True, fetch up to 6 pages (~54 articles).
                   If False, fetch page 1 only (~9 articles) and merge with cache.
    """
    try:
        cache = load_cache()
        cached_articles = deserialize_articles(cache.get("articles", []))

        if full_reset or not cached_articles:
            mode = "full reset" if full_reset else "no cache exists"
            logger.info(f"Running full fetch ({mode})")
            new_articles = fetch_all_articles(max_pages=MAX_PAGES_FULL)
        else:
            logger.info("Running incremental update (page 1 only)")
            new_articles = fetch_all_articles(max_pages=MAX_PAGES_INCREMENTAL)

        logger.info(f"Found {len(new_articles)} articles from page(s)")

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
    parser = argparse.ArgumentParser(description="Generate Mistral AI News RSS feed")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Force full reset (fetch up to 6 pages)",
    )
    args = parser.parse_args()
    main(full_reset=args.full)
