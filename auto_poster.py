"""
Photo History Auto Poster
=========================

This script automates the process of fetching images for photographers listed in a Markdown file
and posting the articles to a WordPress site.

Prerequisites:
    pip install -r requirements.txt

Configuration:
    Copy .env.example to .env and fill in your WordPress credentials.

Usage:
    python3 auto_poster.py [--dry-run]

    --dry-run:  If specified, or if credentials are missing, the script will simulate the
                WordPress posting process without making actual API calls.
"""

import os
import re
import requests
import markdown
import json
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

class ImageFetcher:
    def __init__(self, filepath):
        self.filepath = filepath
        # Regex to find: **■ Japanese Name (English Name) ｜ Country**
        # We need to capture the English Name for searching.
        # Use [\uff5c|] to match both fullwidth and halfwidth pipes.
        self.pattern = re.compile(r'^\s*\*\*■\s+(?P<jp_name>.+?)\s*\((?P<en_name>.+?)\)\s*[\uff5c|]')

    def fetch_wikimedia_image(self, query):
        """
        Search Wikimedia Commons for an image.
        Returns the URL of the first result or None.
        """
        print(f"Searching Wikimedia for: {query}")
        base_url = "https://commons.wikimedia.org/w/api.php"
        headers = {
            "User-Agent": "PhotoHistoryBot/1.0 (https://example.com; bot@example.com)"
        }

        # Step 1: Search for the file page
        search_params = {
            "action": "query",
            "generator": "search",
            "gsrnamespace": "6",  # File namespace
            "gsrsearch": query,
            "gsrlimit": "1",
            "prop": "imageinfo",
            "iiprop": "url",
            "format": "json"
        }

        try:
            response = requests.get(base_url, params=search_params, headers=headers)
            response.raise_for_status()
            data = response.json()

            pages = data.get("query", {}).get("pages", {})
            if not pages:
                return None

            # pages is a dict like {'1234': {...}}
            for page_id in pages:
                image_info = pages[page_id].get("imageinfo", [])
                if image_info:
                    url = image_info[0]["url"]
                    # Filter for valid image extensions to avoid PDFs etc.
                    if url.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                        return url
        except Exception as e:
            print(f"Error fetching from Wikimedia: {e}")

        return None

    def fetch_google_books_image(self, query):
        """
        Search Google Books for a book cover.
        Returns the thumbnail URL of the first result or None.
        """
        print(f"Searching Google Books for: {query}")
        base_url = "https://www.googleapis.com/books/v1/volumes"
        headers = {
            "User-Agent": "PhotoHistoryBot/1.0"
        }
        params = {
            "q": query,
            "maxResults": 1
        }

        try:
            response = requests.get(base_url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()

            items = data.get("items", [])
            if not items:
                return None

            volume_info = items[0].get("volumeInfo", {})
            image_links = volume_info.get("imageLinks", {})
            return image_links.get("thumbnail")

        except Exception as e:
            print(f"Error fetching from Google Books: {e}")

        return None

    def process_file(self):
        if not os.path.exists(self.filepath):
            print(f"File not found: {self.filepath}")
            return

        with open(self.filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        new_lines = []
        i = 0
        while i < len(lines):
            line = lines[i]
            new_lines.append(line)
            i += 1

            match = self.pattern.match(line)
            if match:
                en_name = match.group('en_name')

                # Check if next line is already an image (simple idempotency check)
                if i < len(lines) and lines[i].strip().startswith("!["):
                    print(f"Skipping {en_name}, image already present.")
                    continue

                print(f"Found photographer: {en_name}")
                image_url = self.fetch_wikimedia_image(en_name)

                if not image_url:
                    image_url = self.fetch_google_books_image(en_name)

                if image_url:
                    print(f"Found image: {image_url}")
                    # Insert image format: ![Name](URL)
                    jp_name = match.group('jp_name')
                    new_lines.append(f"![{jp_name} ({en_name})]({image_url})\n\n")
                else:
                    print(f"No image found for {en_name}")

        with open(self.filepath, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
        print("Phase 1: Image injection complete.")


class WordPressPoster:
    def __init__(self, filepath, dry_run=False):
        self.filepath = filepath
        self.dry_run = dry_run
        self.wp_user = os.getenv("WP_USER")
        self.wp_password = os.getenv("WP_APP_PASSWORD")
        self.wp_site = os.getenv("WP_SITE_URL")

        if not self.dry_run and not all([self.wp_user, self.wp_password, self.wp_site]):
            print("Warning: WP credentials missing in .env. Switching to dry_run mode.")
            self.dry_run = True

    def split_articles(self, content):
        """
        Splits the content by '## 記事Vol'.
        Returns a list of dictionaries with 'title' and 'content' (html).
        """
        # Split by the header pattern
        # Note: The split will result in [intro, article1, article2...]
        # We need to handle the separator carefully.

        # Regex to split but keep the delimiter is tricky if we want to include the '## 記事Vol' line in the body?
        # The prompt says: "Extract Title from '### タイトル：'".
        # "Article Title" for WP should be extracted. The body should likely exclude the raw markdown title if we use it as WP title, or keep it.
        # Usually WP posts take the title separate from content.

        # Strategy: Split by `## 記事Vol`
        parts = re.split(r'(?=## 記事Vol)', content)

        articles = []
        for part in parts:
            if not part.strip() or "## 記事Vol" not in part:
                continue

            # Extract Title
            title_match = re.search(r'### タイトル：(.+)', part)
            if title_match:
                title = title_match.group(1).strip()
                # Remove the title line from the body content to avoid duplication
                part = part.replace(title_match.group(0), "")
            else:
                # Fallback title if regex fails
                first_line = part.strip().split('\n')[0]
                title = first_line.replace("#", "").strip()

            # Convert to HTML
            html_content = markdown.markdown(part)

            articles.append({
                "title": title,
                "content": html_content
            })

        return articles

    def post_article(self, article):
        url = f"{self.wp_site}/wp-json/wp/v2/posts"

        payload = {
            "title": article["title"],
            "content": article["content"],
            "status": "draft"
        }

        if self.dry_run:
            print(f"--- [DRY RUN] Posting Article: {article['title']} ---")
            print(f"Endpoint: {url}")
            print(f"Payload (truncated): {payload['content'][:100]}...")
            return

        try:
            # Use Application Password Auth (Basic Auth)
            auth = (self.wp_user, self.wp_password)
            response = requests.post(url, json=payload, auth=auth)
            response.raise_for_status()
            print(f"Successfully posted: {article['title']} (ID: {response.json().get('id')})")
        except Exception as e:
            print(f"Failed to post {article['title']}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(e.response.text)

    def process(self):
        if not os.path.exists(self.filepath):
            print(f"File not found: {self.filepath}")
            return

        with open(self.filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        articles = self.split_articles(content)
        print(f"Phase 2: Found {len(articles)} articles. Starting upload...")

        for article in articles:
            self.post_article(article)

        print("Phase 2: Complete.")

if __name__ == "__main__":
    import sys

    # Phase 1
    fetcher = ImageFetcher("photo_history.md")
    fetcher.process_file()

    # Phase 2
    # Check for dry-run argument
    dry_run = "--dry-run" in sys.argv
    # Also default to dry_run if env is dummy
    if os.getenv("WP_SITE_URL") == "https://example.com":
        dry_run = True
        print("Detected dummy config, enforcing dry-run.")

    poster = WordPressPoster("photo_history.md", dry_run=dry_run)
    poster.process()
