"""
Book Reader CLI
Flow: load epub → pick chapter → summary → Q&A loop
"""
import os
import sys
from dotenv import load_dotenv
import anthropic
from epub_parser import process_epub, Book

load_dotenv()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# ── Prompts ────────────────────────────────────────────────────────────────

SUMMARY_SYSTEM = """You are a reading companion helping someone deeply understand a book.
Produce a structured chapter summary. Be thorough but readable — aim for 2 to 4 pages.
Mix prose paragraphs with bullets where it helps. Do not over-bullet."""

SUMMARY_PROMPT = """Here is a chapter from "{book_title}" by {authors}.
Chapter: {chapter_title}

---
{chapter_text}
---

Summarize this chapter using the following framework:

## 1. Core Argument
In 2-3 sentences: what is the single main thing this chapter is saying?

## 2. Key Ideas
For each key idea (3-5 total):
- State the idea clearly
- What story, example, or evidence does the author use to illustrate it?

## 3. What This Means For You
What is the practical implication? What would someone do differently after reading this?

## 4. Questions To Reflect On
Give 3 questions that make the reader think deeper, challenge their assumptions, or apply the ideas to their own life. Number them 1, 2, 3."""


QA_SYSTEM = """You are a reading companion helping someone understand a book chapter deeply.

You have the full chapter text and the summary already provided to the reader.

Rules:
- Answer questions strictly from the chapter text. Quote directly where it helps.
- If a question is outside this chapter's scope, say so clearly — do not speculate or use outside knowledge.
- Ask one reflection question at a time. Do not bombard.
- When the reader answers your question, engage with their answer genuinely before moving on.
- Keep responses focused and conversational — this is a dialogue, not a lecture.
- If the reader types 'next chapter' or 'done', acknowledge and wrap up warmly."""


# ── Chapter Building ───────────────────────────────────────────────────────

# TOC entries to skip (front/back matter)
SKIP_TITLES = {
    "cover", "copyright", "table of contents", "dedication",
    "further reading", "resources", "acknowledgments", "acknowledgements",
    "index", "front cover flap", "back cover flap", "back cover material",
    "foreword", "about the author"
}

def build_chapter_list(book: Book):
    """
    Build a flat list of readable chapters from the TOC.
    Each entry: {title, display_title, hrefs, parent}
    """
    chapters = []

    # Map href → word count for filtering
    word_counts = {s.href: len(s.text.split()) for s in book.spine}

    def get_hrefs(entry):
        hrefs = {entry.file_href}
        for child in entry.children:
            hrefs.update(get_hrefs(child))
        return hrefs

    def total_words(entry):
        return sum(word_counts.get(h, 0) for h in get_hrefs(entry))

    for top in book.toc:
        if top.title.lower().strip() in SKIP_TITLES:
            continue

        if top.children:
            # Show each child as its own chapter
            for child in top.children:
                if child.title.lower().strip() in SKIP_TITLES:
                    continue
                hrefs = get_hrefs(child)
                words = sum(word_counts.get(h, 0) for h in hrefs)
                if words < 200:
                    continue
                chapters.append({
                    "title": child.title,
                    "display": f"{child.title}  [{top.title}]",
                    "hrefs": hrefs,
                    "words": words,
                })
        else:
            hrefs = get_hrefs(top)
            words = total_words(top)
            if words < 200:
                continue
            chapters.append({
                "title": top.title,
                "display": top.title,
                "hrefs": hrefs,
                "words": words,
            })

    return chapters


def get_chapter_text(book: Book, hrefs: set) -> str:
    text = ""
    for section in book.spine:
        if section.href in hrefs and section.text.strip():
            text += section.text + "\n\n"
    return text.strip()


# ── Summary ────────────────────────────────────────────────────────────────

def print_summary(book: Book, chapter: dict) -> str:
    chapter_text = get_chapter_text(book, chapter["hrefs"])
    authors = ", ".join(book.metadata.authors)

    prompt = SUMMARY_PROMPT.format(
        book_title=book.metadata.title,
        authors=authors,
        chapter_title=chapter["title"],
        chapter_text=chapter_text,
    )

    print("\n" + "─" * 60)
    print(f"  {chapter['title']}")
    print("─" * 60 + "\n")

    full_summary = ""
    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=SUMMARY_SYSTEM,
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
            full_summary += text

    print("\n")
    return full_summary, chapter_text


# ── Q&A Loop ───────────────────────────────────────────────────────────────

def qa_loop(book: Book, chapter: dict, summary: str, chapter_text: str):
    authors = ", ".join(book.metadata.authors)

    # Seed the conversation: give Claude the chapter + summary as context
    context_message = f"""Book: "{book.metadata.title}" by {authors}
Chapter: {chapter['title']}

--- FULL CHAPTER TEXT ---
{chapter_text}

--- SUMMARY ALREADY SHOWN TO READER ---
{summary}
"""

    conversation = [
        {"role": "user", "content": context_message},
        {"role": "assistant", "content": "I have read the chapter and the summary. I'm ready to discuss it with you."},
    ]

    # Claude asks the first reflection question
    conversation.append({
        "role": "user",
        "content": "Please ask me the first reflection question from the summary to get us started."
    })

    print("─" * 60)
    print("  Q&A  —  ask anything, or answer the questions above.")
    print("  Type 'done' to finish.\n")
    print("─" * 60 + "\n")

    # Get Claude's opening question
    opening = ""
    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=QA_SYSTEM,
        messages=conversation,
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
            opening += text
    print("\n")

    conversation.append({"role": "assistant", "content": opening})

    # Main loop
    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\nSession ended.")
            break

        if not user_input:
            continue

        if user_input.lower() in ("done", "exit", "quit", "next chapter"):
            print("\nGreat session! See you in the next chapter.\n")
            break

        conversation.append({"role": "user", "content": user_input})

        reply = ""
        print("\nAssistant: ", end="", flush=True)
        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=QA_SYSTEM,
            messages=conversation,
        ) as stream:
            for text in stream.text_stream:
                print(text, end="", flush=True)
                reply += text
        print("\n")

        conversation.append({"role": "assistant", "content": reply})


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    # Get epub path from argument or prompt
    if len(sys.argv) >= 2:
        epub_path = sys.argv[1]
    else:
        epub_path = input("Path to epub file: ").strip().strip('"')

    if not os.path.exists(epub_path):
        print(f"File not found: {epub_path}")
        sys.exit(1)

    print("\nLoading book...")
    book = process_epub(epub_path)
    authors = ", ".join(book.metadata.authors)

    print(f"\n{'═' * 60}")
    print(f"  {book.metadata.title}")
    print(f"  {authors}")
    print(f"{'═' * 60}\n")

    chapters = build_chapter_list(book)

    if not chapters:
        print("No readable chapters found.")
        sys.exit(1)

    # Show chapter list
    print("Chapters:\n")
    for i, ch in enumerate(chapters, 1):
        words = ch["words"]
        pages = round(words / 250)  # ~250 words per page
        print(f"  [{i:2}] {ch['display']:<50} (~{pages} pages)")

    print()

    # Pick a chapter
    while True:
        try:
            choice = input("Pick a chapter number: ").strip()
            idx = int(choice) - 1
            if 0 <= idx < len(chapters):
                break
            print(f"Please enter a number between 1 and {len(chapters)}")
        except ValueError:
            print("Please enter a number")

    selected = chapters[idx]
    print(f"\nGenerating summary for: {selected['title']}...\n")

    summary, chapter_text = print_summary(book, selected)
    qa_loop(book, selected, summary, chapter_text)


if __name__ == "__main__":
    main()
