"""
Google Docs integration — OAuth 2.0 flow + Docs API.

Flow:
  1. get_auth_url(book_id)       →  send user to Google consent screen
  2. exchange_code(code, state)  →  returns (Credentials, book_id)
  3. sync_to_doc(creds, ...)     →  appends highlights, returns doc URL
"""

import os
import json
import hashlib
import base64
import secrets

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


def _make_pkce() -> tuple:
    """Generate (code_verifier, code_challenge) for PKCE."""
    verifier  = base64.urlsafe_b64encode(secrets.token_bytes(40)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    return verifier, challenge


def get_auth_url(book_id: str = "") -> str:
    """Generate Google OAuth URL. Packs book_id + code_verifier into state."""
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES)
    flow.redirect_uri = REDIRECT_URI

    verifier, challenge = _make_pkce()

    # Encode book_id and verifier into state so they survive the redirect
    state_blob = base64.urlsafe_b64encode(
        json.dumps({"b": book_id, "v": verifier}).encode()
    ).decode().rstrip("=")

    auth_url, _ = flow.authorization_url(
        prompt="consent",
        access_type="offline",
        state=state_blob,
        code_challenge=challenge,
        code_challenge_method="S256",
    )
    return auth_url


def exchange_code(code: str, state: str = ""):
    """Exchange auth code for Credentials. Returns (Credentials, book_id)."""
    verifier = ""
    book_id  = ""

    # Decode state to recover verifier + book_id
    try:
        padding = (4 - len(state) % 4) % 4
        decoded = json.loads(base64.urlsafe_b64decode(state + "=" * padding))
        verifier = decoded.get("v", "")
        book_id  = decoded.get("b", "")
    except Exception:
        pass

    flow = Flow.from_client_config(_client_config(), scopes=SCOPES)
    flow.redirect_uri = REDIRECT_URI
    flow.fetch_token(code=code, code_verifier=verifier)
    return flow.credentials, book_id


def _get_or_create_doc(drive_svc, docs_svc, book_title: str) -> str:
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
    """Append highlights to the Google Doc for this book. Returns doc URL."""
    drive_svc = build("drive", "v3", credentials=creds)
    docs_svc  = build("docs",  "v1", credentials=creds)

    doc_id = _get_or_create_doc(drive_svc, docs_svc, book_title)

    if highlights:
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
        doc  = docs_svc.documents().get(documentId=doc_id).execute()
        end  = doc["body"]["content"][-1]["endIndex"] - 1

        docs_svc.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": [
                {"insertText": {"location": {"index": end}, "text": text}}
            ]},
        ).execute()

    return f"https://docs.google.com/document/d/{doc_id}/edit"
