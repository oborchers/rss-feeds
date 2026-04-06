import logging
from datetime import datetime, timedelta
import pytz
import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator

from utils import get_feeds_dir, setup_feed_links, sort_posts_for_feed

FEED_NAME = "groq"
BLOG_URL = "https://groq.com/blog/"

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
    hash_val = abs(hash(identifier)) % 730
    epoch = datetime(2023, 1, 1, 0, 0, 0, tzinfo=pytz.UTC)
    return epoch + timedelta(days=hash_val)




def fetch_blog_content():
    """Fetch blog page. Static HTML with redirect follow."""
    logger.info(f"Fetching {BLOG_URL}")
    response = requests.get(BLOG_URL, headers=HEADERS, timeout=15, allow_redirects=True)
    response.raise_for_status()
    return response.text


def parse_blog_html(html_content):
    """Parse Groq blog HTML. Cards are article.card elements."""
    soup = BeautifulSoup(html_content, "html.parser")
    articles = []
    seen_links = set()

    for card in soup.select("article.card"):
        # Title and link from h2.card__title > a
        title_link = card.select_one("h2.card__title a")
        if not title_link:
            continue

        href = title_link.get("href", "")
        if not href or href in ("/blog/", "/blog"):
            continue

        link = f"https://groq.com{href}" if href.startswith("/") else href

        if link in seen_links:
            continue
        seen_links.add(link)

        title = title_link.get_text(strip=True)
        if not title:
            continue

        # Date from time.card__eyebrow[datetime]
        date = None
        time_elem = card.select_one("time.card__eyebrow")
        if time_elem:
            dt_attr = time_elem.get("datetime")
            if dt_attr:
                try:
                    date = datetime.fromisoformat(
                        dt_attr.replace("Z", "+00:00")
                    )
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=pytz.UTC)
                except ValueError:
                    pass

        if not date:
            date = stable_fallback_date(link)

        articles.append({
            "title": title,
            "link": link,
            "date": date,
            "description": title,
        })

    logger.info(f"Parsed {len(articles)} articles")
    return articles


def main():
    try:
        html = fetch_blog_content()
        articles = parse_blog_html(html)

        if not articles:
            logger.warning("No articles found.")
            return False

        articles = sort_posts_for_feed(articles, date_field="date")

        fg = FeedGenerator()
        fg.title("Groq Blog")
        fg.description("Latest news and updates from Groq")
        fg.language("en")
        fg.author({"name": "Groq"})
        fg.subtitle("LPU inference, AI infrastructure, and developer updates")

        setup_feed_links(fg, blog_url=BLOG_URL, feed_name=FEED_NAME)

        for article in articles:
            fe = fg.add_entry()
            fe.title(article["title"])
            fe.description(article["description"])
            fe.link(href=article["link"])
            fe.published(article["date"])
            fe.id(article["link"])

        feeds_dir = get_feeds_dir()
        output_file = feeds_dir / f"feed_{FEED_NAME}.xml"
        fg.rss_file(str(output_file), pretty=True)
        logger.info(f"Saved RSS feed to {output_file} with {len(articles)} articles")
        return True

    except Exception as e:
        logger.error(f"Failed to generate RSS feed: {str(e)}")
        return False


if __name__ == "__main__":
    main()
