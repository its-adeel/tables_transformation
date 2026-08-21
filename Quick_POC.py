"""
table_repr_validation.py

Quick POC to validate: does keeping raw (cleaned) HTML, or converting to
structured YAML, beat markdown-converted text for retrieval + answer
correctness, on the specific "hard" tables that markdown conversion
currently mangles?

This is intentionally minimal — no Milvus, no pgvector, no production
pipeline. Just enough to get a defensible comparison table.

Setup:
    pip install groq sentence-transformers beautifulsoup4 pyyaml

    export GROQ_API_KEY=gsk_...
    (first run will download the local embedding model, ~80MB, no key needed)

Usage:
    1. For each hard table, drop matching plain .txt files into cases/
       using the same base name with a _html, _md, and/or _yaml suffix:
         cases/table_001_html.txt   (raw scraped HTML, paste as-is)
         cases/table_001_md.txt     (your current markdown-converted version)
         cases/table_001_yaml.txt   (output from html_to_yaml.py)
       _html.txt is required per case; _md.txt and _yaml.txt are each
       optional — a case missing one just skips that representation for
       itself rather than being dropped entirely, so you can add YAML
       incrementally without needing every format for every case.
    2. Fill in `questions.json` with test questions per case (see schema below).
    3. Run: python table_repr_validation.py
    4. Read results.csv or open validation.db in a SQLite viewer
"""

import os
import json
import csv
import re
import sqlite3
from dataclasses import dataclass, field
from bs4 import BeautifulSoup
from groq import Groq
from sentence_transformers import SentenceTransformer
import numpy as np
import yaml as pyyaml

# Reuses the SAME YAML serialization as the actual converter (including
# the wide-key formatting fix) when re-chunking YAML by row-groups, so
# chunked output looks identical in style to a real converter run rather
# than introducing a second, slightly different YAML formatter.
from html_to_yaml import to_yaml_string as _yaml_dump
from dotenv import load_dotenv

load_dotenv()

groq_client = Groq()  # reads GROQ_API_KEY from env
embed_model = SentenceTransformer("all-MiniLM-L6-v2")  # local, runs on CPU

CHAT_MODEL = "llama-3.3-70b-versatile"  # swap for whichever Groq-hosted Llama you want

# Which representations to test. Edit this list to run a subset — e.g.
# ["markdown", "yaml"] to skip HTML — without touching anything else.
REPRESENTATIONS = ["markdown", "html", "yaml"]

# Chunking config
CHUNK_MODE = "whole"     # "whole" = one chunk per table, "rows" = split into row groups
ROWS_PER_CHUNK = 5       # only used when CHUNK_MODE == "rows"
TOP_K = 1                # how many chunks to retrieve and pass as context per question

# ---------------------------------------------------------------------------
# 1. INPUT SCHEMA
# ---------------------------------------------------------------------------
#
# Drop matching .txt files into cases/, using the same base name with a
# _html, _md, and/or _yaml suffix — no subfolders, no special extensions.
#
# e.g.:
#   cases/table_001_html.txt   <- paste raw HTML directly, no escaping needed
#   cases/table_001_md.txt     <- paste your markdown-converted version
#   cases/table_001_yaml.txt   <- paste html_to_yaml.py's output
#   cases/table_002_html.txt
#   ...
#
# The base name (e.g. "table_001") becomes the case_id — use that same id
# in questions.json. _html.txt is required; _md.txt / _yaml.txt are each
# optional per case.
#
# questions.json (this one stays JSON since it's just short plain text):
# [
#   {
#     "case_id": "table_001",
#     "question": "What is the front yard setback for zone R-1?",
#     "expected_answer": "25 ft"
#   },
#   ...
# ]

def load_cases(cases_dir: str) -> list[dict]:
    cases = []
    if not os.path.isdir(cases_dir):
        raise FileNotFoundError(
            f"'{cases_dir}/' not found. Create cases/<case_id>_html.txt "
            f"for each hard table (and optionally _md.txt / _yaml.txt)."
        )

    html_files = sorted(f for f in os.listdir(cases_dir) if f.endswith("_html.txt"))

    for html_file in html_files:
        case_id = html_file[: -len("_html.txt")]
        html_path = os.path.join(cases_dir, html_file)

        with open(html_path, encoding="utf-8") as f:
            html = f.read()

        case = {"id": case_id, "html": html}

        missing = []
        for rep, suffix in (("markdown", "_md.txt"), ("yaml", "_yaml.txt")):
            if rep not in REPRESENTATIONS:
                continue
            path = os.path.join(cases_dir, case_id + suffix)
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as f:
                    case[rep] = f.read()
            else:
                missing.append(rep)

        if missing:
            print(f"  '{case_id}': no {'/'.join(missing)} file(s) — that "
                  f"representation will be skipped for this case only.")

        cases.append(case)

    return cases

CASES_DIR = "cases"
QUESTIONS_PATH = "questions.json"
RESULTS_PATH = "results.csv"
DB_PATH = "validation.db"  # open this in DB Browser for SQLite / TablePlus / etc.


# ---------------------------------------------------------------------------
# 2. CLEANING (Set B: raw HTML, lightly stripped)
# ---------------------------------------------------------------------------

def clean_html(raw_html: str) -> str:
    """Strip scripts/styles/attrs but keep table structure tags intact."""
    soup = BeautifulSoup(raw_html, "html.parser")

    for tag in soup(["script", "style"]):
        tag.decompose()

    # strip attributes (class, style, id, etc.) but keep the tags themselves
    for tag in soup.find_all(True):
        tag.attrs = {}

    text = str(soup)
    text = re.sub(r"\n\s*\n", "\n", text).strip()
    return text


# ---------------------------------------------------------------------------
# 3. CHUNKING — table-level (whole) or row-group (optional)
# ---------------------------------------------------------------------------

@dataclass
class Chunk:
    case_id: str
    representation: str  # "markdown" or "html"
    text: str
    chunk_index: int = 0
    embedding: list = field(default=None, repr=False)


def chunk_markdown_table(md: str, rows_per_chunk: int) -> list[str]:
    """Split a markdown table into row groups, repeating header+separator in each chunk."""
    lines = [l for l in md.strip().split("\n") if l.strip()]
    if len(lines) <= 2:
        return [md]  # just header/separator, or not a real table — nothing to split

    header_lines = lines[:2]  # header row + markdown separator row (e.g. |---|---|)
    data_lines = lines[2:]

    chunks = []
    for i in range(0, len(data_lines), rows_per_chunk):
        group = data_lines[i:i + rows_per_chunk]
        chunks.append("\n".join(header_lines + group))
    return chunks or [md]


def chunk_html_table(html: str, rows_per_chunk: int) -> list[str]:
    """Split an HTML table into row groups, repeating the header row(s) in each chunk."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        return [html]

    thead = table.find("thead")
    if thead:
        header_rows = thead.find_all("tr")
        tbody = table.find("tbody")
        body_rows = tbody.find_all("tr") if tbody else [
            tr for tr in table.find_all("tr") if tr not in header_rows
        ]
    else:
        all_trs = table.find_all("tr")
        if all_trs and all_trs[0].find("th"):
            header_rows, body_rows = [all_trs[0]], all_trs[1:]
        else:
            header_rows, body_rows = [], all_trs

    if not body_rows:
        return [str(table)]

    header_html = "".join(str(r) for r in header_rows)

    chunks = []
    for i in range(0, len(body_rows), rows_per_chunk):
        group = body_rows[i:i + rows_per_chunk]
        body_html = "".join(str(r) for r in group)
        chunks.append(f"<table><thead>{header_html}</thead><tbody>{body_html}</tbody></table>")
    return chunks


def chunk_yaml_table(yaml_text: str, rows_per_chunk: int) -> list[str]:
    """
    Split a YAML table's TOP-LEVEL keys into row-groups, re-serializing
    each group back to YAML text (using the same dumper as the actual
    converter, so formatting stays consistent).

    Unlike markdown/HTML, html_to_yaml.py produces a NESTED
    structure, not flat rows — there's no universal notion of "a row"
    once data is nested several levels deep (a divider section, its
    sub-items, and their values are all just nested dict levels). This
    treats each TOP-LEVEL key as one unit ("row" in spirit) and groups
    rows_per_chunk of them together, same as the markdown/HTML row
    chunking does with actual table rows. If the YAML has a single
    top-level wrapper key (e.g. a table caption wrapping everything),
    that key's own children are used instead, so chunking happens at a
    level that actually has multiple items to split.
    """
    try:
        data = pyyaml.safe_load(yaml_text)
    except Exception:
        return [yaml_text]  # not parseable — fall back to one whole chunk

    if not isinstance(data, dict) or not data:
        return [yaml_text]

    # If there's a single top-level key (e.g. a caption wrapping
    # everything), descend into it — and keep descending through any
    # CHAIN of such single-key wrappers (e.g. caption > section > the
    # actual rows), stopping as soon as we reach a level with multiple
    # keys, since that's where the real rows to split are.
    while isinstance(data, dict) and len(data) == 1:
        only_value = next(iter(data.values()))
        if isinstance(only_value, dict):
            data = only_value
        else:
            break

    if not isinstance(data, dict) or len(data) <= 1:
        return [yaml_text]  # nothing meaningful to split

    items = list(data.items())
    chunks = []
    for i in range(0, len(items), rows_per_chunk):
        group = dict(items[i:i + rows_per_chunk])
        chunks.append(_yaml_dump(group))
    return chunks or [yaml_text]


def build_chunks(cases: list[dict], chunk_mode: str = CHUNK_MODE, rows_per_chunk: int = ROWS_PER_CHUNK) -> list[Chunk]:
    chunks = []
    for case in cases:
        for rep in REPRESENTATIONS:
            if rep not in case:
                continue  # this case has no file for this representation — skip it only here

            if rep == "markdown":
                parts = chunk_markdown_table(case["markdown"], rows_per_chunk) if chunk_mode == "rows" else [case["markdown"]]
            elif rep == "html":
                parts = [clean_html(h) for h in chunk_html_table(case["html"], rows_per_chunk)] if chunk_mode == "rows" else [clean_html(case["html"])]
            elif rep == "yaml":
                parts = chunk_yaml_table(case["yaml"], rows_per_chunk) if chunk_mode == "rows" else [case["yaml"]]
            else:
                continue

            for idx, part in enumerate(parts):
                chunks.append(Chunk(case["id"], rep, part, chunk_index=idx))

    return chunks


# ---------------------------------------------------------------------------
# 4. EMBEDDING + simple in-memory index (no vector DB needed for this scale)
# ---------------------------------------------------------------------------

def embed(texts: list[str]) -> np.ndarray:
    return embed_model.encode(texts, convert_to_numpy=True)


def embed_chunks(chunks: list[Chunk]):
    vectors = embed([c.text for c in chunks])
    for c, v in zip(chunks, vectors):
        c.embedding = v


def retrieve(query: str, chunks: list[Chunk], representation: str, top_k: int = 1):
    """Cosine similarity search restricted to one representation set."""
    pool = [c for c in chunks if c.representation == representation]
    q_vec = embed([query])[0]

    def cos_sim(a, b):
        return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

    scored = sorted(pool, key=lambda c: cos_sim(q_vec, c.embedding), reverse=True)
    return scored[:top_k]


# ---------------------------------------------------------------------------
# 5. ANSWER GENERATION + crude correctness check
# ---------------------------------------------------------------------------

def answer_from_context(question: str, context_text: str) -> str:
    resp = groq_client.chat.completions.create(
        model=CHAT_MODEL,
        messages=[
            {
                "role": "system",
                "content": "Answer ONLY using the provided table content. "
                            "Be precise with numbers and units. "
                            "If the answer isn't in the content, say 'NOT FOUND'.",
            },
            {"role": "user", "content": f"Table content:\n{context_text}\n\nQuestion: {question}"},
        ],
        temperature=0,
    )
    return resp.choices[0].message.content.strip()


def loose_match(expected: str, actual: str) -> bool:
    """Very rough correctness check — normalize and check substring."""
    norm = lambda s: re.sub(r"[^a-z0-9.]", "", s.lower())
    return norm(expected) in norm(actual)


# ---------------------------------------------------------------------------
# 6. SQLITE PERSISTENCE — open validation.db in any DB viewer to inspect
# ---------------------------------------------------------------------------

def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE IF EXISTS chunks")
    conn.execute("DROP TABLE IF EXISTS results")

    conn.execute("""
        CREATE TABLE chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id TEXT,
            representation TEXT,
            chunk_index INTEGER,
            text TEXT,
            chunk_chars INTEGER
        )
    """)

    # Per-representation result columns are generated from REPRESENTATIONS
    # instead of being hardcoded, so adding/removing a representation (as
    # already done here for "yaml") doesn't require hand-editing this SQL
    # and the INSERT statement below to stay in sync.
    per_rep_columns = []
    for rep in REPRESENTATIONS:
        per_rep_columns += [
            f"{rep}_retrieved_right_table INTEGER",
            f"{rep}_num_chunks_retrieved INTEGER",
            f"{rep}_retrieved_chunk_indices TEXT",
            f"{rep}_answer TEXT",
            f"{rep}_correct INTEGER",
            f"{rep}_chunk_chars INTEGER",
        ]

    conn.execute(f"""
        CREATE TABLE results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id TEXT,
            question TEXT,
            expected TEXT,
            {", ".join(per_rep_columns)}
        )
    """)
    conn.commit()
    return conn


def save_chunks(conn: sqlite3.Connection, chunks: list[Chunk]):
    conn.executemany(
        "INSERT INTO chunks (case_id, representation, chunk_index, text, chunk_chars) VALUES (?, ?, ?, ?, ?)",
        [(c.case_id, c.representation, c.chunk_index, c.text, len(c.text)) for c in chunks],
    )
    conn.commit()


def save_results(conn: sqlite3.Connection, rows: list[dict]):
    per_rep_fields = []
    for rep in REPRESENTATIONS:
        per_rep_fields += [
            f"{rep}_retrieved_right_table", f"{rep}_num_chunks_retrieved",
            f"{rep}_retrieved_chunk_indices", f"{rep}_answer",
            f"{rep}_correct", f"{rep}_chunk_chars",
        ]
    all_fields = ["case_id", "question", "expected"] + per_rep_fields
    conn.executemany(
        f"""
        INSERT INTO results ({", ".join(all_fields)})
        VALUES ({", ".join(":" + f for f in all_fields)})
        """,
        rows,
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 7. MAIN EVAL LOOP
# ---------------------------------------------------------------------------

def main():
    cases = load_cases(CASES_DIR)
    with open(QUESTIONS_PATH) as f:
        questions = json.load(f)

    print(f"Loaded {len(cases)} cases, {len(questions)} questions.")
    print(f"Representations: {', '.join(REPRESENTATIONS)}")
    print(f"Chunk mode: {CHUNK_MODE}" + (f" ({ROWS_PER_CHUNK} rows/chunk)" if CHUNK_MODE == "rows" else ""))
    print(f"Top-K retrieval: {TOP_K} chunk(s) per question")

    chunks = build_chunks(cases, chunk_mode=CHUNK_MODE, rows_per_chunk=ROWS_PER_CHUNK)
    embed_chunks(chunks)

    conn = init_db(DB_PATH)
    save_chunks(conn, chunks)

    cases_by_id = {c["id"]: c for c in cases}

    rows = []
    for q in questions:
        row = {"case_id": q["case_id"], "question": q["question"], "expected": q["expected_answer"]}
        case = cases_by_id.get(q["case_id"], {})

        for rep in REPRESENTATIONS:
            if rep not in case:
                # This case has no file for this representation — leave
                # its columns blank rather than guessing, so it's visibly
                # distinguishable from a genuine retrieval failure (0
                # chunks, empty answer) when you're reading results.csv.
                row[f"{rep}_retrieved_right_table"] = None
                row[f"{rep}_num_chunks_retrieved"] = None
                row[f"{rep}_retrieved_chunk_indices"] = "SKIPPED (no file)"
                row[f"{rep}_answer"] = "SKIPPED (no file)"
                row[f"{rep}_correct"] = None
                row[f"{rep}_chunk_chars"] = None
                continue

            top = retrieve(q["question"], chunks, representation=rep, top_k=TOP_K)
            retrieved_correct_case = any(c.case_id == q["case_id"] for c in top)
            context_text = "\n\n---\n\n".join(c.text for c in top) if top else ""
            answer = answer_from_context(q["question"], context_text) if top else "NO CHUNK RETRIEVED"
            correct = loose_match(q["expected_answer"], answer)

            row[f"{rep}_retrieved_right_table"] = int(retrieved_correct_case)
            row[f"{rep}_num_chunks_retrieved"] = len(top)
            row[f"{rep}_retrieved_chunk_indices"] = ",".join(str(c.chunk_index) for c in top)
            row[f"{rep}_answer"] = answer
            row[f"{rep}_correct"] = int(correct)
            row[f"{rep}_chunk_chars"] = sum(len(c.text) for c in top)

        rows.append(row)
        print(f"  done: {q['case_id']} - {q['question'][:50]}...")

    save_results(conn, rows)
    conn.close()

    fieldnames = list(rows[0].keys())
    with open(RESULTS_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    # quick summary — only counts questions where that representation
    # was actually available (skipped ones are None, not 0, so they
    # won't silently drag the score down)
    print()
    for rep in REPRESENTATIONS:
        scored = [r[f"{rep}_correct"] for r in rows if r[f"{rep}_correct"] is not None]
        avg_chars = [r[f"{rep}_chunk_chars"] for r in rows if r[f"{rep}_chunk_chars"] is not None]
        if scored:
            correct_n = sum(scored)
            avg_c = sum(avg_chars) / len(avg_chars) if avg_chars else 0
            print(f"  {rep}: {correct_n}/{len(scored)} correct, avg {avg_c:.0f} chars/chunk retrieved")
        else:
            print(f"  {rep}: no scored questions (no files found for this representation)")

    print(f"\nResults written to {RESULTS_PATH} and {DB_PATH}")
    print(f"Open {DB_PATH} in DB Browser for SQLite (or any SQLite client) to inspect chunks + results.")


if __name__ == "__main__":
    main()