
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import requests


API_URL = "https://qiita.com/api/v2"
PER_PAGE = 100

IMAGE_PATTERN = re.compile(
    r'(!\[[^\]]*\]\()<?(https?://[^\s)>]+)>?'
    r'(\s+(?:"[^"]*"|\'[^\']*\'))?(\))'
)


def get_articles(token: str) -> list[dict]:
    """Qiitaから全記事を取得する。"""
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })

    articles = []

    for page in range(1, 101):
        response = session.get(
            f"{API_URL}/authenticated_user/items",
            params={"page": page, "per_page": PER_PAGE},
            timeout=30,
        )
        response.raise_for_status()

        items = response.json()
        if not items:
            break

        articles.extend(items)

        if len(items) < PER_PAGE:
            break

    return articles


def load_manifest(output: Path) -> dict:
    """前回の取得情報を読み込む。"""
    path = output / "manifest.json"

    if not path.exists():
        return {}

    return json.loads(path.read_text(encoding="utf-8"))


def save_manifest(output: Path, manifest: dict):
    """取得情報を保存する。"""
    path = output / "manifest.json"
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def make_frontmatter(article: dict) -> str:
    metadata = {
        "title": article["title"],
        "qiita_id": article["id"],
        "url": article["url"],
        "created_at": article["created_at"],
        "updated_at": article["updated_at"],
        "tags": [tag["name"] for tag in article.get("tags", [])],
        "private": article.get("private", False),
    }

    lines = ["---"]

    for key, value in metadata.items():
        lines.append(
            f"{key}: {json.dumps(value, ensure_ascii=False)}"
        )

    lines.append("---")
    return "\n".join(lines)


def download_image(
    session: requests.Session,
    url: str,
    image_dir: Path,
) -> str | None:
    """画像をダウンロードする。"""
    try:
        response = session.get(url, timeout=30)
        response.raise_for_status()

        content_type = response.headers.get(
            "Content-Type", ""
        ).split(";")[0]

        ext = Path(urlparse(url).path).suffix.lower()

        if not ext:
            ext = mimetypes.guess_extension(content_type) or ".img"

        digest = hashlib.sha256(url.encode()).hexdigest()[:16]
        filename = f"{digest}{ext}"

        image_dir.mkdir(parents=True, exist_ok=True)

        image_path = image_dir / filename

        if not image_path.exists():
            image_path.write_bytes(response.content)

        return filename

    except requests.RequestException as e:
        print(f"画像取得失敗: {url} ({e})")
        return None


def localize_images(
    body: str,
    session: requests.Session,
    image_dir: Path,
) -> str:
    """画像をローカル保存し、Markdownを相対パスに変更する。"""

    def replace(match: re.Match) -> str:
        prefix, url, title, suffix = match.groups()

        filename = download_image(session, url, image_dir)

        if filename is None:
            return match.group(0)

        return (
            f"{prefix}images/{filename}"
            f"{title or ''}{suffix}"
        )

    return IMAGE_PATTERN.sub(replace, body)


def save_article(
    article: dict,
    output: Path,
    mode: str,
    session: requests.Session,
):
    """記事を保存する。"""

    article_id = article["id"]
    body = article.get("body", "")
    frontmatter = make_frontmatter(article)

    if mode == "github":
        article_dir = output / "articles"
        article_dir.mkdir(parents=True, exist_ok=True)

        path = article_dir / f"{article_id}.md"

    else:
        article_dir = output / "articles" / article_id
        article_dir.mkdir(parents=True, exist_ok=True)

        image_dir = article_dir / "images"

        body = localize_images(
            body, session, image_dir
        )

        path = article_dir / "README.md"

    content = f"{frontmatter}\n\n{body}"
    path.write_text(content, encoding="utf-8")


def make_index(articles: list[dict], output: Path, mode: str):
    """記事一覧READMEを生成する。"""

    lines = [
        "# Qiita Articles",
        "",
        f"記事数: {len(articles)}",
        "",
        "| タイトル | 更新日 | タグ |",
        "|---|---|---|",
    ]

    for article in sorted(
        articles,
        key=lambda x: x["updated_at"],
        reverse=True,
    ):
        title = article["title"].replace("|", "\\|")
        tags = ", ".join(
            tag["name"] for tag in article.get("tags", [])
        )

        if mode == "github":
            link = f"articles/{article['id']}.md"
        else:
            link = f"articles/{article['id']}/README.md"

        lines.append(
            f"| [{title}]({link}) "
            f"| {article['updated_at'][:10]} "
            f"| {tags} |"
        )

    (output / "README.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def backup(
    articles: list[dict],
    output: Path,
    mode: str,
):
    output.mkdir(parents=True, exist_ok=True)

    manifest = load_manifest(output)

    session = requests.Session()

    new_count = 0
    update_count = 0
    skip_count = 0

    for article in articles:
        article_id = article["id"]
        updated_at = article["updated_at"]

        old = manifest.get(article_id)

        if mode == "github":
            path = output / "articles" / f"{article_id}.md"
        else:
            path = (
                output / "articles" / article_id / "README.md"
            )

        # 更新日時が同じでファイルも存在すればスキップ
        if (
            old
            and old.get("updated_at") == updated_at
            and path.exists()
        ):
            skip_count += 1
            continue

        is_new = old is None

        save_article(
            article,
            output,
            mode,
            session,
        )

        manifest[article_id] = {
            "updated_at": updated_at,
            "title": article["title"],
        }

        if is_new:
            new_count += 1
            print(f"[NEW] {article['title']}")
        else:
            update_count += 1
            print(f"[UPDATE] {article['title']}")

    save_manifest(output, manifest)

    # 記事一覧は毎回更新
    make_index(articles, output, mode)

    print(f"\n--- {mode} backup ---")
    print(f"新規: {new_count}")
    print(f"更新: {update_count}")
    print(f"スキップ: {skip_count}")
    print(f"合計: {len(articles)}")


def main():
    parser = argparse.ArgumentParser(
        description="Qiita記事バックアップ"
    )

    parser.add_argument(
        "--mode",
        choices=["github", "local"],
        required=True,
    )

    parser.add_argument(
        "--output",
        required=True,
    )

    args = parser.parse_args()

    token = os.environ.get("QIITA_ACCESS_TOKEN")

    if not token:
        raise RuntimeError(
            "QIITA_ACCESS_TOKEN が設定されていません"
        )

    print("Qiita記事取得中...")

    articles = get_articles(token)

    print(f"{len(articles)} 件取得しました")

    backup(
        articles,
        Path(args.output),
        args.mode,
    )


if __name__ == "__main__":
    main()
