"""
Persistence layer — Supabase via REST API (no supabase-py package needed).

Uses plain HTTP requests to Supabase's PostgREST (database) and
Storage APIs. This avoids all Python package dependency issues.

Tables:
  books    — one row per uploaded EPUB
  chapters — one row per chapter per book (holds summary text)
  messages — one row per chat message

Storage bucket:
  epubs    — raw EPUB files
"""

import os
import re
from datetime import datetime, timezone
from typing import Optional

import requests

try:
    import streamlit as st
    def _creds():
        try:
            return st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"]
        except Exception:
            return os.getenv("SUPABASE_URL", ""), os.getenv("SUPABASE_KEY", "")
except ImportError:
    def _creds():
        return os.getenv("SUPABASE_URL", ""), os.getenv("SUPABASE_KEY", "")


def _headers(extra: dict = None) -> dict:
    _, key = _creds()
    h = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def _db_url(table: str) -> str:
    url, _ = _creds()
    return f"{url}/rest/v1/{table}"


def _storage_url(path: str) -> str:
    url, _ = _creds()
    return f"{url}/storage/v1/object/epubs/{path}"


def _raise(resp: requests.Response, context: str):
    if not resp.ok:
        raise RuntimeError(f"{context} failed ({resp.status_code}): {resp.text}")


# ── Key helpers ────────────────────────────────────────────────────────────

def make_book_id(title: str, authors: list) -> str:
    raw = f"{title}-{'-'.join(authors)}"
    return re.sub(r'[^a-z0-9-]', '-', raw.lower()).strip('-')[:80]


def make_chapter_key(section: str, title: str) -> str:
    raw = f"{section}-{title}" if section else title
    return re.sub(r'[^a-z0-9-]', '-', raw.lower()).strip('-')[:120]


# ── Books ──────────────────────────────────────────────────────────────────

def list_books() -> list:
    """Return all saved books, newest first."""
    resp = requests.get(
        _db_url("books"),
        headers=_headers({"Accept": "application/json"}),
        params={"order": "created_at.desc"},
    )
    _raise(resp, "list_books")
    return resp.json()


def get_book_record(book_id: str) -> Optional[dict]:
    resp = requests.get(
        _db_url("books"),
        headers=_headers({"Accept": "application/json"}),
        params={"id": f"eq.{book_id}"},
    )
    _raise(resp, "get_book_record")
    data = resp.json()
    return data[0] if data else None


def save_book(book, epub_bytes: bytes, epub_filename: str) -> str:
    """Upload EPUB to storage and upsert book metadata. Returns book_id."""
    book_id = make_book_id(book.metadata.title, book.metadata.authors)
    epub_path = f"{book_id}/{epub_filename}"

    # Upload EPUB file to storage (upsert = overwrite if exists)
    storage_headers = {
        "apikey": _headers()["apikey"],
        "Authorization": _headers()["Authorization"],
        "Content-Type": "application/octet-stream",
        "x-upsert": "true",
    }
    resp = requests.post(_storage_url(epub_path), headers=storage_headers, data=epub_bytes)
    _raise(resp, "upload epub")

    # Upsert book metadata
    resp = requests.post(
        _db_url("books"),
        headers=_headers({"Prefer": "resolution=merge-duplicates"}),
        json={
            "id": book_id,
            "title": book.metadata.title,
            "authors": ", ".join(book.metadata.authors),
            "epub_path": epub_path,
        },
    )
    _raise(resp, "save_book metadata")
    return book_id


def download_epub(epub_path: str) -> bytes:
    """Download EPUB bytes from storage."""
    resp = requests.get(_storage_url(epub_path), headers=_headers())
    _raise(resp, "download_epub")
    return resp.content


# ── Summaries ──────────────────────────────────────────────────────────────

def save_summary(
    book_id: str,
    chapter_key: str,
    title: str,
    section: str,
    summary: str,
) -> None:
    resp = requests.post(
        _db_url("chapters"),
        headers=_headers({"Prefer": "resolution=merge-duplicates"}),
        json={
            "book_id": book_id,
            "chapter_key": chapter_key,
            "title": title,
            "section": section or "",
            "summary": summary,
            "summary_created_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    _raise(resp, "save_summary")


def load_summary(book_id: str, chapter_key: str) -> Optional[str]:
    resp = requests.get(
        _db_url("chapters"),
        headers=_headers({"Accept": "application/json"}),
        params={"book_id": f"eq.{book_id}", "chapter_key": f"eq.{chapter_key}"},
    )
    _raise(resp, "load_summary")
    data = resp.json()
    if data and data[0].get("summary"):
        return data[0]["summary"]
    return None


# ── Chat messages ──────────────────────────────────────────────────────────

def save_message(book_id: str, chapter_key: str, role: str, content: str) -> None:
    resp = requests.post(
        _db_url("messages"),
        headers=_headers(),
        json={
            "book_id": book_id,
            "chapter_key": chapter_key,
            "role": role,
            "content": content,
        },
    )
    _raise(resp, "save_message")


def load_messages(book_id: str, chapter_key: str) -> list:
    resp = requests.get(
        _db_url("messages"),
        headers=_headers({"Accept": "application/json"}),
        params={
            "book_id": f"eq.{book_id}",
            "chapter_key": f"eq.{chapter_key}",
            "order": "id",
            "select": "role,content",
        },
    )
    _raise(resp, "load_messages")
    return resp.json()
