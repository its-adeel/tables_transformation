"""
clean_html.py

Strips a raw scraped HTML page/document down to just the parts the table
converter actually cares about — dramatically smaller, and much easier to
paste or upload without hitting size limits.

KEPT (because table_to_yaml_converter.py depends on it):
  - <table>, <thead>, <tbody>, <tr>, <td>, <th> structure
  - colspan / rowspan attributes (critical — these encode merged cells)
  - all visible text content
  - <figcaption> elements (used for table titles outside <table>)
  - <div class="title"> and <div class="chunk-title"> (same reason)
  - the aria-hidden attribute (used to detect decoy "sticky header" tables)

STRIPPED (noise the converter never looks at):
  - <script>, <style>, <svg>, <button>, <input>, <select>, HTML comments
  - known non-content wrapper blocks: checkboxes, icons, "notes",
    "questions", "history", "footnotes", "schemeIcons" containers
  - every other attribute: id, style, data-*, align, valign, width,
    cellpadding, cellspacing, border, rules, scope, startcol, tabstyle,
    class (except the two values above), and so on

Usage:
    python clean_html.py input.html > cleaned.html
    # or pipe it in:
    cat input.html | python clean_html.py > cleaned.html
"""

import sys
from bs4 import BeautifulSoup, Comment

# Tags to remove entirely (including their contents)
REMOVE_TAGS = ["script", "style", "svg", "button", "input", "select", "noscript"]

# If an element's class list contains any of these tokens, remove the
# WHOLE element (it's UI chrome or known-empty bookkeeping, not content).
REMOVE_CLASS_TOKENS = {
    "checkbox", "MuiButtonBase-root", "MuiSvgIcon-root", "schemeIcons",
    "notes", "questions", "history", "footnotes", "titleLinkAndFootnotes",
}

# class values worth KEEPING as-is, because the converter's title
# extractors specifically look for them
KEEP_CLASS_VALUES = {"title", "chunk-title"}

# Attributes worth keeping on ANY tag (everything else gets stripped)
KEEP_ATTRS_ANY_TAG = {"aria-hidden"}

# Attributes worth keeping specifically on table cells (structure-critical)
KEEP_ATTRS_TABLE_CELLS = {"colspan", "rowspan"}


def clean_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    # Drop HTML comments entirely
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()

    # Drop non-content tags entirely
    for tag_name in REMOVE_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    # Drop known UI-chrome / bookkeeping elements by class token
    for tag in list(soup.find_all(True)):
        if getattr(tag, "decomposed", False):
            continue  # already removed as part of an earlier parent's decompose()
        classes = tag.get("class") or []
        if any(token in classes for token in REMOVE_CLASS_TOKENS):
            tag.decompose()

    # Strip attributes down to only what the converter actually uses
    for tag in list(soup.find_all(True)):
        if getattr(tag, "decomposed", False):
            continue
        if not tag.attrs:
            continue

        new_attrs = {}
        for attr, value in tag.attrs.items():
            if attr in KEEP_ATTRS_ANY_TAG:
                new_attrs[attr] = value
            elif attr == "class":
                kept = [c for c in (value if isinstance(value, list) else [value]) if c in KEEP_CLASS_VALUES]
                if kept:
                    new_attrs["class"] = kept
            elif attr in KEEP_ATTRS_TABLE_CELLS and tag.name in ("td", "th"):
                new_attrs[attr] = value
        tag.attrs = new_attrs

    # Drop now-empty elements that carry no useful structure — but NEVER
    # touch table cells, since a blank <td></td> is meaningful data (an
    # explicit "no value here"), not noise.
    for tag in list(soup.find_all(["div", "span", "p", "a", "h1", "h2", "h3", "header"])):
        if getattr(tag, "decomposed", False):
            continue
        if tag.name in ("td", "th"):
            continue
        if not tag.get_text(strip=True) and not tag.find(["table", "figcaption"]):
            tag.decompose()

    return str(soup)


def main():
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as f:
            html = f.read()
    else:
        html = sys.stdin.read()

    cleaned = clean_html(html)
    print(cleaned)

    original_size = len(html)
    cleaned_size = len(cleaned)
    reduction = 100 * (1 - cleaned_size / original_size) if original_size else 0
    print(
        f"\n<!-- cleaned: {original_size:,} chars -> {cleaned_size:,} chars "
        f"({reduction:.0f}% smaller) -->",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()