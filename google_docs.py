"""
Google Docs integration — OAuth 2.0 flow + Docs API.

Flow:
  1. get_auth_url(book_id)  →  send user to Google consent screen
  2. exchange_code(code)    →  returns Credentials object
  3. sync_to_doc(creds, book_title, highlights)  →  appends highlights, returns doc URL
"""

import os
import streamlit as st
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]

REDIRECT_URI = "https://book-reader-pnq.streamlit.app/"


def _client_config() -> dict:
    try:
        client_id = st.secrets["GOOGLE_CLIENT_ID"]
        client_secret = st.secrets["GOOGLE_CLIENT_SECRET"]
    except Exception:
        client_id = os.getenv("GOOGLE_CLIENT_ID", "")
        client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uris": [REDIRECT_URI],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def get_auth_url(book_id: str = "") -> str:
    """Generate the Google OAuth consent URL. Pass book_id as state."""
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES)
    flow.redirect_uri = REDIRECT_URI
    auth_url, _ = flow.authorization_url(
        prompt="consent",
        access_type="offline",
        state=book_id,
    )
    return auth_url


def exchange_code(code: str) -> Credentials:
    """Exchange authorization code for Credentials."""
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES)
    flow.redirect_uri = REDIRECT_URI
    flow.fetch_token(code=code)
    return flow.credentials


def _get_or_create_doc(drive_svc, docs_svc, book_title: str) -> str:
    """Return doc_id of existing doc with this title, or create a new one."""
    safe = book_title.replace("'", "\\'")
    results = drive_svc.files().list(
        q=(
            f"name='{safe}' "
            "and mimeType='application/vnd.google-apps.document' "
            "and trashed=false"
        ),
        fields="files(id)",
        pageSize=1,
    ).execute()

    files = results.get("files", [])
    if files:
        return files[0]["id"]

    doc = docs_svc.documents().create(body={"title": book_title}).execute()
    return doc["documentId"]


def sync_to_doc(creds: Credentials, book_title: str, highlights: list) -> str:
    """
    Append highlights to the Google Doc for this book.
    Returns the URL to the doc.
    """
    drive_svc = build("drive", "v3", credentials=creds)
    docs_svc = build("docs", "v1", credentials=creds)

    doc_id = _get_or_create_doc(drive_svc, docs_svc, book_title)

    if highlights:
        # Group by chapter
        chapters: dict = {}
        for h in highlights:
            title = h.get("chapter_title") or h.get("chapter_key", "")
            chapters.setdefault(title, []).append(h)

        lines = ["\n"]
        for chapter_title, items in chapters.items():
            lines.append(f"📖 {book_title} — {chapter_title}\n")
            for h in items:
                lines.append(f"Highlight: \"{h['selected_text']}\"\n")
                if h.get("comment"):
                    lines.append(f"Comment: {h['comment']}\n")
                lines.append("\n")

        text = "".join(lines)

        # Insert at end of document
        doc = docs_svc.documents().get(documentId=doc_id).execute()
        end_index = doc["body"]["content"][-1]["endIndex"] - 1

        docs_svc.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": [
                {"insertText": {"location": {"index": end_index}, "text": text}}
            ]},
        ).execute()

    return f"https://docs.google.com/document/d/{doc_id}/edit"
