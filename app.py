import os
import tempfile
import anthropic
import streamlit as st
from dotenv import load_dotenv

from epub_parser import process_epub
from prompts import SUMMARY_SYSTEM, SUMMARY_PROMPT, QA_SYSTEM
import storage

load_dotenv()
try:
    api_key = st.secrets["ANTHROPIC_API_KEY"]
except Exception:
    api_key = os.getenv("ANTHROPIC_API_KEY")
client = anthropic.Anthropic(api_key=api_key)

# ── Page config ────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Book Reader",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    .chapter-btn { text-align: left; }
    .summary-box { border-left: 3px solid #4a90d9; padding-left: 1rem; }
    [data-testid="stSidebar"] { min-width: 280px; max-width: 320px; }
</style>
""", unsafe_allow_html=True)

# ── Session state defaults ─────────────────────────────────────────────────
for key, default in {
    "book": None,
    "book_id": None,
    "chapters": [],
    "selected_idx": None,
    "summaries": {},      # idx → summary text
    "chats": {},          # idx → list of {role, content}
    "epub_name": "",
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

# ── Helpers ────────────────────────────────────────────────────────────────
SKIP_TITLES = {
    "cover", "copyright", "table of contents", "dedication",
    "further reading", "resources", "acknowledgments", "acknowledgements",
    "index", "front cover flap", "back cover flap", "back cover material",
    "foreword", "about the author"
}

def build_chapter_list(book):
    chapters = []
    word_counts = {s.href: len(s.text.split()) for s in book.spine}

    def get_hrefs(entry):
        hrefs = {entry.file_href}
        for child in entry.children:
            hrefs.update(get_hrefs(child))
        return hrefs

    for top in book.toc:
        if top.title.lower().strip() in SKIP_TITLES:
            continue
        if top.children:
            for child in top.children:
                if child.title.lower().strip() in SKIP_TITLES:
                    continue
                hrefs = get_hrefs(child)
                words = sum(word_counts.get(h, 0) for h in hrefs)
                if words < 200:
                    continue
                chapters.append({
                    "title": child.title,
                    "section": top.title,
                    "hrefs": hrefs,
                    "words": words,
                })
        else:
            hrefs = get_hrefs(top)
            words = sum(word_counts.get(h, 0) for h in hrefs)
            if words < 200:
                continue
            chapters.append({
                "title": top.title,
                "section": "",
                "hrefs": hrefs,
                "words": words,
            })
    return chapters


def get_chapter_text(book, hrefs):
    text = ""
    for section in book.spine:
        if section.href in hrefs and section.text.strip():
            text += section.text + "\n\n"
    return text.strip()


def stream_summary(book, chapter):
    authors = ", ".join(book.metadata.authors)
    chapter_text = get_chapter_text(book, chapter["hrefs"])
    prompt = SUMMARY_PROMPT.format(
        book_title=book.metadata.title,
        authors=authors,
        chapter_title=chapter["title"],
        chapter_text=chapter_text,
    )
    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=SUMMARY_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for text in stream.text_stream:
            yield text


def get_qa_response(book, chapter, summary, chat_history):
    authors = ", ".join(book.metadata.authors)
    chapter_text = get_chapter_text(book, chapter["hrefs"])

    context = f"""Book: "{book.metadata.title}" by {authors}
Chapter: {chapter['title']}

--- FULL CHAPTER TEXT ---
{chapter_text}

--- SUMMARY SHOWN TO READER ---
{summary}"""

    messages = [
        {"role": "user", "content": context},
        {"role": "assistant", "content": "I have read the chapter and the summary. I'm ready to discuss it with you."},
    ] + chat_history

    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=QA_SYSTEM,
        messages=messages,
    ) as stream:
        for text in stream.text_stream:
            yield text


def load_book_from_storage(book_record: dict):
    """Download EPUB from Supabase, parse and return (book, chapters)."""
    epub_bytes = storage.download_epub(book_record["epub_path"])
    with tempfile.NamedTemporaryFile(delete=False, suffix=".epub") as tmp:
        tmp.write(epub_bytes)
        tmp_path = tmp.name
    book = process_epub(tmp_path)
    os.unlink(tmp_path)
    return book, build_chapter_list(book)


# ── Sidebar ────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("📚 Book Reader")

    # -- Library: previously saved books --
    try:
        saved_books = storage.list_books()
    except Exception:
        saved_books = []

    if saved_books:
        st.markdown("**Library**")
        for b in saved_books:
            is_active = (st.session_state.book_id == b["id"])
            if st.button(
                b["title"],
                key=f"lib_{b['id']}",
                use_container_width=True,
                type="primary" if is_active else "secondary",
            ):
                if not is_active:
                    with st.spinner(f"Loading {b['title']}..."):
                        book, chapters = load_book_from_storage(b)
                    st.session_state.book = book
                    st.session_state.book_id = b["id"]
                    st.session_state.chapters = chapters
                    st.session_state.epub_name = b["id"]
                    st.session_state.selected_idx = None
                    st.session_state.summaries = {}
                    st.session_state.chats = {}
                    st.rerun()
        st.divider()

    # -- Upload new book --
    st.markdown("**Upload new book**")
    uploaded = st.file_uploader("EPUB file", type=["epub"], label_visibility="collapsed")

    if uploaded:
        if uploaded.name != st.session_state.epub_name:
            with st.spinner("Loading book..."):
                epub_bytes = uploaded.read()
                with tempfile.NamedTemporaryFile(delete=False, suffix=".epub") as tmp:
                    tmp.write(epub_bytes)
                    tmp_path = tmp.name
                book = process_epub(tmp_path)
                os.unlink(tmp_path)

            book_id = storage.make_book_id(book.metadata.title, book.metadata.authors)

            # Save to Supabase if not already saved
            try:
                if not storage.get_book_record(book_id):
                    with st.spinner("Saving to library..."):
                        storage.save_book(book, epub_bytes, uploaded.name)
            except Exception:
                pass

            st.session_state.book = book
            st.session_state.book_id = book_id
            st.session_state.chapters = build_chapter_list(book)
            st.session_state.epub_name = uploaded.name
            st.session_state.selected_idx = None
            st.session_state.summaries = {}
            st.session_state.chats = {}

    book = st.session_state.book
    if book:
        authors = ", ".join(book.metadata.authors)
        st.markdown(f"**{book.metadata.title}**  \n*{authors}*")
        st.divider()

        chapters = st.session_state.chapters
        current_section = ""

        for i, ch in enumerate(chapters):
            if ch["section"] and ch["section"] != current_section:
                st.markdown(f"<small style='color:gray'>{ch['section']}</small>",
                            unsafe_allow_html=True)
                current_section = ch["section"]

            pages = round(ch["words"] / 250)
            label = f"{ch['title']}  ·  ~{pages}p"
            is_selected = (st.session_state.selected_idx == i)

            if st.button(
                label,
                key=f"ch_{i}",
                use_container_width=True,
                type="primary" if is_selected else "secondary",
            ):
                st.session_state.selected_idx = i
                st.rerun()


# ── Main panel ─────────────────────────────────────────────────────────────
idx = st.session_state.selected_idx
book = st.session_state.book
chapters = st.session_state.chapters

if book is None:
    st.markdown("## Welcome")
    st.markdown("Upload an EPUB file from the sidebar to get started.")
    st.stop()

if idx is None:
    st.markdown(f"## {book.metadata.title}")
    st.markdown(f"*{', '.join(book.metadata.authors)}*")
    st.markdown("---")
    st.markdown("Pick a chapter from the sidebar to get a summary and start a discussion.")
    st.stop()

chapter = chapters[idx]
book_id = st.session_state.book_id
chapter_key = storage.make_chapter_key(chapter["section"], chapter["title"])

# Chapter header
st.markdown(f"## {chapter['title']}")
if chapter["section"]:
    st.markdown(f"*{chapter['section']}*")
st.divider()

# ── Summary ────────────────────────────────────────────────────────────────
if idx not in st.session_state.summaries:
    # Check Supabase before calling Claude
    cached_summary = None
    if book_id:
        try:
            cached_summary = storage.load_summary(book_id, chapter_key)
        except Exception:
            pass

    if cached_summary:
        # Load summary and chat from Supabase — no API calls needed
        st.session_state.summaries[idx] = cached_summary
        if idx not in st.session_state.chats:
            try:
                saved_msgs = storage.load_messages(book_id, chapter_key)
                if saved_msgs:
                    st.session_state.chats[idx] = saved_msgs
            except Exception:
                pass
        st.rerun()

    else:
        # Generate summary via Claude
        with st.spinner("Generating summary..."):
            summary_text = ""
            summary_placeholder = st.empty()
            for chunk in stream_summary(book, chapter):
                summary_text += chunk
                summary_placeholder.markdown(summary_text)

        st.session_state.summaries[idx] = summary_text

        # Persist summary
        if book_id:
            try:
                storage.save_summary(
                    book_id, chapter_key,
                    chapter["title"], chapter["section"],
                    summary_text,
                )
            except Exception:
                pass

        # Generate opening reflection question
        opening = ""
        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=256,
            system=QA_SYSTEM,
            messages=[
                {"role": "user", "content": f"Chapter: {chapter['title']}\nSummary:\n{summary_text}"},
                {"role": "assistant", "content": "I have read the chapter and summary. Ready to discuss."},
                {"role": "user", "content": "Please ask me the first reflection question to get us started."},
            ],
        ) as stream:
            for text in stream.text_stream:
                opening += text

        st.session_state.chats[idx] = [{"role": "assistant", "content": opening}]

        # Persist opening message
        if book_id:
            try:
                storage.save_message(book_id, chapter_key, "assistant", opening)
            except Exception:
                pass

        st.rerun()

else:
    st.markdown(st.session_state.summaries[idx])

st.divider()
st.markdown("### Discussion")

# ── Chat ───────────────────────────────────────────────────────────────────
chat_history = st.session_state.chats.get(idx, [])

for msg in chat_history:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

user_input = st.chat_input("Ask a question or answer above...")

if user_input:
    chat_history.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    # Persist user message
    if book_id:
        try:
            storage.save_message(book_id, chapter_key, "user", user_input)
        except Exception:
            pass

    with st.chat_message("assistant"):
        reply = ""
        placeholder = st.empty()
        for chunk in get_qa_response(
            book, chapter,
            st.session_state.summaries[idx],
            chat_history,
        ):
            reply += chunk
            placeholder.markdown(reply)

    chat_history.append({"role": "assistant", "content": reply})
    st.session_state.chats[idx] = chat_history

    # Persist assistant reply
    if book_id:
        try:
            storage.save_message(book_id, chapter_key, "assistant", reply)
        except Exception:
            pass
