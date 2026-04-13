"""
EPUB parser — forked from https://github.com/karpathy/reader3
Parses an EPUB file into a structured Book object with spine, TOC, and clean text.
"""

import os
import pickle
import shutil
from dataclasses import dataclass, field
from typing import List, Dict, Optional
from datetime import datetime
from urllib.parse import unquote

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup, Comment


@dataclass
class ChapterContent:
    id: str
    href: str
    title: str
    content: str
    text: str      # plain text — this is what we feed to Claude
    order: int


@dataclass
class TOCEntry:
    title: str
    href: str
    file_href: str
    anchor: str
    children: List['TOCEntry'] = field(default_factory=list)


@dataclass
class BookMetadata:
    title: str
    language: str
    authors: List[str] = field(default_factory=list)
    description: Optional[str] = None
    publisher: Optional[str] = None
    date: Optional[str] = None


@dataclass
class Book:
    metadata: BookMetadata
    spine: List[ChapterContent]
    toc: List[TOCEntry]
    images: Dict[str, str]
    source_file: str
    processed_at: str


def clean_html_content(soup: BeautifulSoup) -> BeautifulSoup:
    for tag in soup(['script', 'style', 'iframe', 'video', 'nav', 'form', 'button']):
        tag.decompose()
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()
    for tag in soup.find_all('input'):
        tag.decompose()
    return soup


def extract_plain_text(soup: BeautifulSoup) -> str:
    text = soup.get_text(separator=' ')
    return ' '.join(text.split())


def parse_toc_recursive(toc_list, depth=0) -> List[TOCEntry]:
    result = []
    for item in toc_list:
        if isinstance(item, tuple):
            section, children = item
            entry = TOCEntry(
                title=section.title,
                href=section.href,
                file_href=section.href.split('#')[0],
                anchor=section.href.split('#')[1] if '#' in section.href else "",
                children=parse_toc_recursive(children, depth + 1)
            )
            result.append(entry)
        elif isinstance(item, epub.Link):
            entry = TOCEntry(
                title=item.title,
                href=item.href,
                file_href=item.href.split('#')[0],
                anchor=item.href.split('#')[1] if '#' in item.href else ""
            )
            result.append(entry)
        elif isinstance(item, epub.Section):
            entry = TOCEntry(
                title=item.title,
                href=item.href,
                file_href=item.href.split('#')[0],
                anchor=item.href.split('#')[1] if '#' in item.href else ""
            )
            result.append(entry)
    return result


def get_fallback_toc(book_obj) -> List[TOCEntry]:
    toc = []
    for item in book_obj.get_items():
        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            name = item.get_name()
            title = name.replace('.html', '').replace('.xhtml', '').replace('_', ' ').title()
            toc.append(TOCEntry(title=title, href=name, file_href=name, anchor=""))
    return toc


def extract_metadata(book_obj) -> BookMetadata:
    def get_list(key):
        data = book_obj.get_metadata('DC', key)
        return [x[0] for x in data] if data else []

    def get_one(key):
        data = book_obj.get_metadata('DC', key)
        return data[0][0] if data else None

    return BookMetadata(
        title=get_one('title') or "Untitled",
        language=get_one('language') or "en",
        authors=get_list('creator'),
        description=get_one('description'),
        publisher=get_one('publisher'),
        date=get_one('date'),
    )


def process_epub(epub_path: str) -> Book:
    print(f"Loading {epub_path}...")
    book = epub.read_epub(epub_path)

    metadata = extract_metadata(book)

    # Extract images into temp map (we don't save to disk for CLI use)
    image_map = {}
    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_IMAGE:
            original_fname = os.path.basename(item.get_name())
            image_map[item.get_name()] = original_fname
            image_map[original_fname] = original_fname

    # Parse TOC
    toc_structure = parse_toc_recursive(book.toc)
    if not toc_structure:
        print("Warning: Empty TOC — building fallback from spine")
        toc_structure = get_fallback_toc(book)

    # Process spine
    spine_chapters = []
    for i, spine_item in enumerate(book.spine):
        item_id, linear = spine_item
        item = book.get_item_with_id(item_id)
        if not item or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue

        raw_content = item.get_content().decode('utf-8', errors='ignore')
        soup = BeautifulSoup(raw_content, 'html.parser')
        soup = clean_html_content(soup)

        chapter = ChapterContent(
            id=item_id,
            href=item.get_name(),
            title=f"Section {i+1}",
            content="",   # not needed for CLI
            text=extract_plain_text(soup),
            order=i
        )
        spine_chapters.append(chapter)

    return Book(
        metadata=metadata,
        spine=spine_chapters,
        toc=toc_structure,
        images=image_map,
        source_file=os.path.basename(epub_path),
        processed_at=datetime.now().isoformat()
    )
