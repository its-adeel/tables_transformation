"""
table_to_yaml_converter.py

Converts your existing table data (raw HTML) into real nested YAML.
Colspan/rowspan is properly expanded into a full grid first, so merged
headers and merged cells are resolved before any structure is inferred.

Handles two table shapes:
  - "matrix" tables (e.g. ADU comparison table): row label -> {column: value}
  - "definition_list" tables (e.g. zoning regulation tables with a caption
    header and hierarchical row labels): caption -> section -> nested labels -> value

Usage:
    Same convention as table_repr_validation.py:
    cases/<id>_html.txt

    pip install pyyaml beautifulsoup4
    python table_to_yaml_converter.py

    Reads raw HTML (real colspan/rowspan structure). Output written to
    cases/<id>_yaml.txt.
"""

# --- Libraries this script needs ---
# os:        for talking to the filesystem (listing/opening files)
# re:        Python's regular-expression module, used here just to squash
#            extra whitespace in cell text
# BeautifulSoup: a library that reads messy HTML and lets us walk through
#            its tags (<table>, <tr>, <td>, ...) like a tree, instead of
#            us having to parse raw text ourselves
# yaml:      a library that takes a normal Python dictionary and prints it
#            out as properly indented YAML text
import os
import re
from bs4 import BeautifulSoup
import yaml
from yaml.events import ScalarEvent, AliasEvent

# The folder where input HTML files live and where output YAML files get
# written. Every table you want converted needs a file at
# cases/<some_id>_html.txt containing that table's raw HTML.
CASES_DIR = "cases"


# ---------------------------------------------------------------------------
# Shared HTML grid-expansion
#
# HTML tables aren't always a clean grid — a single cell can use colspan
# ("stretch across N columns") or rowspan ("stretch down N rows") to merge
# with its neighbors. Before we can understand a table's *meaning*, we
# first need to undo that merging: turn it into a plain grid where every
# row has a value in every column, with merged cells duplicated into every
# position they visually cover. That's what this whole section does.
# ---------------------------------------------------------------------------

def _cell_is_bold(cell) -> bool:
    """
    Was this cell's text marked as bold? Category/section rows in these
    tables are consistently bold (<b>, <strong>, or class="bold"), while
    ordinary data rows are not. That distinction is the ONLY reliable way
    to tell a category-description row apart from a legitimate value that
    happens to span every column (see divider check #2 in
    build_matrix_yaml) — structurally the two are identical HTML.
    """
    if cell.find(["b", "strong"]) is not None:
        return True
    for descendant in cell.find_all(True):
        classes = descendant.get("class") or []
        if any("bold" in c for c in classes):
            return True
    classes = cell.get("class") or []
    return any("bold" in c for c in classes)


def _cell_is_header_styled(cell) -> bool:
    """
    Does this cell carry an explicit "this is a header" style marker,
    even though it's a plain <td> rather than a real <th>?

    Some scraped sources (e.g. codehub-style tables) never emit <th> or
    <thead> at all — every cell, header or data, is a <td> — and instead
    mark header cells purely through an inner div's class, such as
    class="Table-Header-small-center" versus class="Table-Text-small"
    for ordinary data cells. Without checking for that marker, the
    header-row fallback in expand_html_grid has no signal at all for
    these tables and reports header_row_count=0, so multi-row colspan/
    rowspan header grids (zone codes, section labels, etc.) get treated
    as ordinary data rows and corrupt every row nested under them.

    Only a "Header" marker (case-insensitively) counts here — "bold" is
    intentionally excluded, since that's already handled by
    _cell_is_bold and means something different (category/divider rows
    within the DATA, not column headers).
    """
    classes = cell.get("class") or []
    if any("header" in c.lower() for c in classes):
        return True
    for descendant in cell.find_all(True):
        classes = descendant.get("class") or []
        if any("header" in c.lower() for c in classes):
            return True
    return False


def _cell_text(cell) -> str:
    r"""
    Given one HTML cell (a <td> or <th>), pull out just its visible text
    and clean it up.

    HTML source often has messy line breaks and indentation baked into it,
    e.g. a cell might literally contain:
        "Processing\n        time"
    get_text(separator=" ") turns any nested tags into text separated by
    spaces, and re.sub(r"\s+", " ", ...) collapses any run of whitespace
    (spaces, tabs, newlines) down to a single space. End result: clean,
    single-line text with no stray gaps.

    Some scraped documents also contain literal stray "undefined" text
    nodes sitting between tags — an artifact from whatever tool exported
    the page (a JavaScript template variable that resolved to undefined
    and got baked into the static HTML). In practice these wrap the real
    content, e.g. "undefined Type of Use undefined".

    IMPORTANT: only LEADING and TRAILING "undefined" tokens are stripped,
    never one appearing mid-sentence. Stripping it as a bare word
    anywhere would corrupt real text — zoning codes contain phrases like
    "Terms not otherwise undefined in this chapter", and removing the
    word there inverts the legal meaning.
    """
    text = cell.get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(?:undefined\b\s*)+", "", text, flags=re.I)
    text = re.sub(r"(?:\s*\bundefined)+$", "", text, flags=re.I)
    return text.strip()


def _find_all_content_tables(soup):
    """
    Some documents contain MULTIPLE real tables — e.g. a small legend
    table explaining codes like "P = Permitted" alongside the actual
    zoning-use data table. Both are real content (unlike the aria-hidden
    decoy pattern), so picking just the first one silently drops whichever
    table comes second.

    This returns EVERY table that looks like real content, in document
    order: skips aria-hidden decoys, and prefers tables with a <tbody>
    containing actual rows. If none match that filter (e.g. a table
    without an explicit <tbody> tag), falls back to returning every table
    found, so we still attempt something rather than nothing.
    """
    candidates = soup.find_all("table")
    if not candidates:
        return []

    real_tables = [
        t for t in candidates
        if t.get("aria-hidden") != "true"
        and t.find("tbody")
        and t.find("tbody").find("tr")
        and not _is_sticky_header_decoy(t)
    ]
    return real_tables or candidates


def _is_sticky_header_decoy(table) -> bool:
    """
    Is this table a "sticky header" decoy rather than real content?

    Some scrollable-table widgets (class "xsl-table--scroll") render the
    real table TWICE: once wrapped in a container tagged
    "xsl-table--header" that holds nothing but a copy of the header rows
    (kept fixed on screen while the body scrolls), and once wrapped in
    "xsl-table--body" holding the real header AND all the data. Both
    copies pass the aria-hidden/tbody-with-rows filter above — the
    header-only copy has real <tr> tags too, just no data underneath
    them — so without this check it gets treated as a second genuine
    table, producing a spurious "Table N" entry that's just the column
    headers with no data, and shifting the numbering of every table
    after it.
    """
    for ancestor in table.parents:
        if not hasattr(ancestor, "get"):
            continue
        classes = ancestor.get("class") or []
        if "xsl-table--header" in classes:
            return True
    return False


def expand_html_grid(table):
    """
    This is the core "undo the merging" step.

    Takes an already-selected <table> tag (a BeautifulSoup element — see
    _find_all_content_tables, which picks out the real tables from a
    document that may contain more than one).

    Returns a tuple: (header_row_count, grid)

      - header_row_count: how many of the leading rows are header rows
        (like column titles), as opposed to actual data rows.

      - grid: a list of rows. Each row is a Python dictionary that maps a
        column NUMBER (0, 1, 2, ...) to a 3-item package:
              (text, is_header, span_id, is_bold)
        where:
          text      = the cleaned text sitting in that column for this row
          is_header = True if this came from a <th> tag, False for <td>
          span_id   = a unique ID shared by every grid position that came
                      from the SAME original HTML cell (see explanation
                      below — this matters for telling apart "one cell
                      spanning two columns" from "two separate cells that
                      just happen to contain the same text").

    Why we need this at all:
    Imagine this HTML row, where one cell stretches across 2 columns:
        <tr>
          <td>Quantity (SFR)</td>
          <td colspan="2">1</td>
          <td>1</td>
        </tr>
    There are only 3 <td> tags here, but the table actually has 4 columns.
    Without expansion, we wouldn't know that the "1" belongs in BOTH
    column 1 and column 2. This function walks through and explicitly
    writes that same "1" into both column positions, so every later part
    of the code can just ask "what's in column 2?" and get a real answer,
    without needing to know anything about colspan/rowspan itself.
    """
    if table is None:
        return 0, []

    # Grab every <tr> (table row) tag inside this table, in document order.
    rows_html = table.find_all("tr")

    # Step 3: figure out how many of the leading rows are HEADER rows.
    # Prefer using <thead> if the HTML actually has one (it's the most
    # reliable signal — the page author explicitly marked these as
    # headers). If there's no <thead> tag, fall back to a heuristic:
    # count how many rows in a row are made ENTIRELY of <th> cells,
    # starting from the top, and stop at the first row that isn't.
    thead = table.find("thead")
    if thead:
        header_row_count = len(thead.find_all("tr"))
    else:
        header_row_count = 0
        for tr in rows_html:
            cells = tr.find_all(["td", "th"])
            if cells and all(
                c.name == "th" or _cell_is_header_styled(c) for c in cells
            ):
                header_row_count += 1
            else:
                break

    # Step 4: walk through every row and build the expanded grid.
    grid = []

    # row_spans keeps track of "debts" owed to FUTURE rows by rowspan.
    # If a cell has rowspan="3", it needs to also appear in the next 2
    # rows at the same column position, even though those rows won't
    # have a real <td>/<th> tag for it. Format:
    #   column_number -> [rows_still_owed, text, is_header, span_id, is_bold]
    row_spans = {}

    # A running counter so every individual physical HTML cell we process
    # gets its own unique ID. This is what span_id stores.
    next_span_id = 0

    for tr in rows_html:
        # All the actual <td>/<th> tags physically present in this <tr>.
        # Note: this list can be SHORTER than the number of columns in the
        # table, if any cell in this row (or a rowspan from above) is
        # covering extra columns.
        cells_html = tr.find_all(["td", "th"])

        row_cells = {}   # what we're building for this one row
        col_idx = 0       # "which column am I about to fill in next?"
        cell_ptr = 0       # "which real <td>/<th> tag am I about to read next?"

        # Keep filling in columns, left to right, until this row is done.
        while True:
            # Case A: does some earlier row's rowspan still owe this
            # column a value? If so, use that instead of reading a new
            # tag — because in the real HTML, there ISN'T a new tag here;
            # the browser just stretches the earlier cell down into this
            # row visually.
            if col_idx in row_spans and row_spans[col_idx][0] > 0:
                remaining, text, is_header, span_id, is_bold = row_spans[col_idx]
                row_cells[col_idx] = (text, is_header, span_id, is_bold)
                row_spans[col_idx][0] -= 1              # one row closer to done
                if row_spans[col_idx][0] == 0:
                    del row_spans[col_idx]                # fully paid off, forget it
                col_idx += 1
                continue

            # Case B: no more real cells left to read in this row — we're done.
            if cell_ptr >= len(cells_html):
                break

            # Case C: read the next real HTML cell and place its text.
            cell = cells_html[cell_ptr]
            cell_ptr += 1

            text = _cell_text(cell)
            is_header = cell.name == "th"
            is_bold = _cell_is_bold(cell)

            # colspan/rowspan default to 1 if the attribute isn't present.
            # `or 1` also guards against a blank attribute like colspan="".
            colspan = int(cell.get("colspan", 1) or 1)
            rowspan = int(cell.get("rowspan", 1) or 1)

            # Give this physical cell its own unique ID, then move on to
            # the next one for next time.
            span_id = next_span_id
            next_span_id += 1

            # Write this cell's text into every column it visually covers
            # (colspan), and if it ALSO covers future rows (rowspan),
            # register that debt in row_spans so later rows pay it off.
            for _ in range(colspan):
                row_cells[col_idx] = (text, is_header, span_id, is_bold)
                if rowspan > 1:
                    row_spans[col_idx] = [rowspan - 1, text, is_header, span_id, is_bold]
                col_idx += 1

        grid.append(row_cells)

    return header_row_count, grid


def build_header_paths(header_rows: list, total_cols: int) -> tuple:
    """
    Some tables have MULTIPLE stacked header rows — e.g. one row saying
    "Single-family Residential Zones" spanning 3 columns, and a second
    row underneath giving the real names "R-L-12", "R-L-8", "R-L-5".

    This walks down through the header rows for each column and joins
    whatever text it finds into one combined path, separated by " > ".
    So column 2 might end up with a path like:
        "Single-family Residential Zones > R-L-12"

    Returns (paths, note, dominant_group):
      paths          = dict: column_number -> combined header path ("" if
                       that column had no real header text at all)
      note           = "" normally, but if one header segment (e.g. a
                       legend line like "P - Permitted Use  S = Special
                       Review Use...") is shared by a large majority of
                       columns AS ONE UNDIVIDED block, it adds nothing to
                       help tell those columns apart — it's a table-wide
                       note, not a real header level. That text is pulled
                       out here (returned once, instead of silently
                       repeating itself into every single row's output
                       for every one of those columns) and stripped from
                       the affected columns' paths.
      dominant_group = "" normally, but when one header segment covers a
                       clear majority of columns AS A REAL GROUPING (not
                       stripped as a note — e.g. "Zoning District" over
                       "UMF"/"SMF"/...), this is that segment's text.
                       Useful as a fallback table name when the table has
                       no caption of its own: "Zoning District" is a far
                       more informative name than a generic "Table N" for
                       a table with no title, and it's real information
                       already present in the table rather than invented.
    """
    paths = {}
    for c in range(total_cols):
        parts = []
        for row in header_rows:
            if c in row:
                text = row[c][0].strip()   # row[c] = (text, is_header, span_id); [0] = text
                # Only add this piece if it's non-empty AND it's not just
                # a repeat of the last thing we added. Repeats happen when
                # rowspan duplicates the same header text into both header
                # rows for one column — without this check we'd get
                # "Specific Land Use Standards > Specific Land Use Standards".
                if text and (not parts or parts[-1] != text):
                    parts.append(text)
        paths[c] = " > ".join(parts)

    # Group columns by the FIRST segment of their path, then decide
    # whether that segment is a table-wide note or a real grouping.
    #
    # The distinguishing signal is what sits BELOW it. A legend sits above
    # a complete grouping level (legend > "Residential" > "RD" — three
    # segments), so removing it still leaves the columns fully grouped
    # and named. A real grouping IS that level (e.g. "Residential" > "R1"
    # — two segments), and removing it would destroy the only thing
    # marking those columns as residential.
    #
    # An earlier version used only a ">50% of columns" threshold, which
    # wrongly stripped legitimate groupings whenever one happened to
    # cover a majority of columns (e.g. "Residential" over 3 of 5).
    first_segment_cols = {}
    for c, path in paths.items():
        if not path:
            continue
        first = path.split(" > ", 1)[0]
        first_segment_cols.setdefault(first, []).append(c)

    note = ""
    dominant_group = ""
    non_empty_count = sum(1 for p in paths.values() if p)
    if first_segment_cols and non_empty_count > 0:
        dominant_text, dominant_cols = max(first_segment_cols.items(), key=lambda kv: len(kv[1]))
        is_majority = len(dominant_cols) >= 2 and len(dominant_cols) / non_empty_count > 0.5
        has_full_grouping_below = all(
            len(paths[c].split(" > ")) >= 3 for c in dominant_cols
        )
        if len(dominant_cols) >= 3 and is_majority and has_full_grouping_below:
            note = dominant_text
            for c in dominant_cols:
                remainder = paths[c][len(dominant_text):].lstrip(" >")
                paths[c] = remainder
        elif is_majority:
            # Not a note (doesn't have a full grouping level below it to
            # fall back on), but it's still a real, majority-covering
            # grouping — a good naming candidate for the caller.
            dominant_group = dominant_text

    return paths, note, dominant_group


def split_caption_rows(grid: list, header_row_count: int, total_cols: int):
    """
    Some tables have a leading row that ISN'T a real header at all — it's
    just a title or legend stretched across every column with one single
    piece of text, e.g. "Table 17.08.020 / Residential Zone Use Table".

    This looks at the leading header rows, one at a time from the top, and
    checks: "does this row cover every single column, and is the exact
    same text sitting in every one of them?" If yes, that's a caption —
    pull it out separately. It keeps doing this for as many leading rows
    as match (some tables have 2 caption rows stacked: a title AND a
    legend), and stops at the first row that has genuinely different text
    in different columns (a real header row).

    Returns (captions, offset):
      captions = list of caption strings found, in order
      offset   = how many leading rows were captions, so the caller knows
                 grid[offset:header_row_count] are the REAL header rows
    """
    captions = []
    offset = 0
    for i in range(header_row_count):
        row = grid[i]
        cols_present = sorted(row.keys())
        # Does this row have something in literally every column?
        if cols_present == list(range(total_cols)):
            texts = {row[c][0].strip() for c in cols_present}
            # Is it the SAME text in all of them, and not blank?
            if len(texts) == 1 and next(iter(texts)):
                captions.append(next(iter(texts)))
                offset += 1
                continue
        # First row that doesn't match the caption pattern — stop here.
        break
    return captions, offset


# ---------------------------------------------------------------------------
# Nested-dict builders
#
# Once we have a clean grid and know the header structure, we need to
# actually build the Python dictionary that will become the YAML output.
# There are two very different table "shapes" this can take (see the
# module docstring at the top), so there are two separate builder
# functions below, plus a couple of small shared helpers.
# ---------------------------------------------------------------------------

def _get_or_create_child(parent: dict, key: str) -> dict:
    """
    A small nesting helper: "give me the dictionary living under this key
    inside `parent` — and if it doesn't exist yet, create an empty one
    first." This is how new levels of nesting (a new section, a new
    sub-label) get built on the fly as we walk through rows.

    If the key already holds a plain VALUE (not a dict), that value is
    preserved under a "(General)" sub-key rather than being thrown away —
    this happens when a label first appears with a value of its own and
    later acts as a parent for sub-items. Silently replacing it with {}
    would delete real data with no warning. "(General)" is a deliberate
    choice over a plain list-of-mixed-content: the label itself tells
    both the embedding model (at retrieval time) and the answering model
    (at generation time) that this specific text is a provision for the
    WHOLE section, not for any one sub-item — a distinction that would
    otherwise only exist by position in a flat list, which is harder for
    a model to reliably infer from content alone.

    Example:
        result = {}
        node = _get_or_create_child(result, "Site Planning")
        # now result == {"Site Planning": {}}
        # and `node` is a direct reference to that inner {} dict, so
        # anything we add to `node` shows up inside result too.
    """
    existing = parent.get(key)
    if isinstance(existing, dict):
        return existing
    if key in parent and existing not in ("", None):
        parent[key] = {"(General)": existing}
    else:
        parent[key] = {}
    return parent[key]


def _insert_path(container: dict, path_parts: list, value: str):
    """
    Takes a LIST of nested labels, e.g.
        ["Site Planning", "Minimum lot dimensions", "Width"]
    and a value, e.g. "200 feet", and builds all the nesting needed to
    place that value at the bottom:
        {"Site Planning": {"Minimum lot dimensions": {"Width": "200 feet"}}}

    It walks through every label EXCEPT the last one, creating a nested
    dict at each step (using _get_or_create_child), then uses the very
    last label as the final key that actually holds the value.

    A common real case for a collision at the final key: a "catch-all"
    row (e.g. "When not listed above, the parking requirement for
    primary uses listed in this tier shall apply.") that applies to a
    whole section rather than one specific sub-item. Because of how
    rowspan carries a section's name down into its rows, such a row's
    label ends up being just the section name itself — the SAME key
    already holding that section's other real sub-entries as a dict.
    Rather than overwrite that whole dict with a bare string (destroying
    every sub-entry it holds), the value is filed under "(General)"
    inside it instead.

    A section can genuinely have MORE THAN ONE such general provision
    (e.g. an opening rate description before its specific sub-items, AND
    a closing catch-all clause after them — both belong directly to the
    section, neither is more specific than the other). Rather than
    inventing numbered key names ("(General)", "(General) (2)", ...) for
    this, they're collected into a YAML LIST under the one "(General)"
    key — the list itself already says "these all belong here" without
    needing invented labels to tell them apart.
    """
    node = container
    for part in path_parts[:-1]:
        node = _get_or_create_child(node, part)

    leaf = path_parts[-1]
    if leaf in node and node[leaf] != value:
        if isinstance(node[leaf], dict):
            existing_general = node[leaf].get("(General)")
            if existing_general is None:
                node[leaf]["(General)"] = value
            elif isinstance(existing_general, list):
                if value not in existing_general:
                    existing_general.append(value)
            elif existing_general != value:
                node[leaf]["(General)"] = [existing_general, value]
            return
        suffix = 2
        while f"{leaf} ({suffix})" in node:
            suffix += 1
        leaf = f"{leaf} ({suffix})"
    node[leaf] = value


def build_matrix_yaml(data_rows: list, header_paths: dict, total_cols: int) -> dict:
    """
    Handles "matrix" tables — ones with real, distinct column headers,
    like the ADU comparison table (columns: Attached ADU, Converted ADU,
    Detached ADU, JADU) or the residential use table (columns: R-L-12,
    R-L-8, R-M, ...).

    --- Step 1: group columns that share one header ---
    Sometimes two columns share the EXACT same header path — either
    because the header was genuinely blank for both, or because one
    header cell used colspan/rowspan to cover both of them (like "Land
    Uses" spanning an item-number column AND an item-name column). If we
    tried to use that shared header text as a dictionary key for BOTH
    columns, the second one would silently overwrite the first. So
    instead, adjacent columns with an identical header path get grouped
    together first.

    The FIRST group (leftmost columns) becomes the row's LABEL — e.g.
    "1 - Assisted living, skilled nursing, or hospice facility". Every
    other group becomes a real value field in that row's output.

    --- Step 2: detect section-divider rows ---
    Real tables often have bold "category" rows splitting the data into
    groups (e.g. "ACCESSORY USES", "RESIDENTIAL USES"). These aren't real
    data rows — they should become a new nesting level that following
    rows get placed inside. There are actually THREE different ways a
    divider row can show up in the HTML, and this function checks for
    all three, in order:

      1. Full-width divider: one single HTML cell, via colspan, stretches
         across the ENTIRE row (every column, including what would
         normally be "value" columns) — e.g. "ACCESSORY USES". Caught
         first, before any label/value logic runs.

      2. Category-description divider: a single HTML cell spans some (not
         all) of the value columns with descriptive text, e.g. one
         <td colspan="2"> holding "Minimum area and width required..."
         under two separately-named zone columns. This is checked using
         span_id (see expand_html_grid) rather than comparing text,
         because two zones can coincidentally require the exact same
         real value (e.g. both need "0 ft" setback, typed into two
         SEPARATE cells) — that must NOT be mistaken for a divider.

      3. Partial-label divider: the row's label is incomplete (e.g. the
         item-number column is blank, only the bold section text is
         present) and there's no real data in the value columns either.

    Whichever way a divider is found, it doesn't get its own entry —
    instead it opens a new nested "section" dictionary, and every row
    after it gets placed inside that section until the next divider (or
    the end of the table).
    """
    # --- Build the column groups described in Step 1 above ---
    groups = []  # list of (header_path, [col_idx, col_idx, ...])
    for col_idx in sorted(header_paths.keys()):
        path = header_paths[col_idx]
        # If the previous group has this SAME header path, and this
        # column is immediately next to it, extend that group instead of
        # starting a new one.
        if groups and groups[-1][0] == path and groups[-1][1][-1] == col_idx - 1:
            groups[-1][1].append(col_idx)
        else:
            groups.append((path, [col_idx]))

    if not groups:
        return {}

    # Some tables have MULTIPLE consecutive grouping columns before the
    # real per-row distinguishing name — e.g. a "Zoning District" code,
    # then a "Uses" category that ALSO repeats via rowspan across the
    # same rows, THEN the specific use name that changes every row (a
    # 3-level compound: "SF6" > "Single Family" > "Single Family
    # Detached", "Semi-Detached", "Duplex", ...). This can also combine
    # with the identical-header-text merge above: if the table's header
    # row itself uses colspan across the first two columns (e.g. one
    # <th colspan="2">Zoning District</th> covering what are semantically
    # a zone code AND a use category), those two columns are ALREADY one
    # group before this check even runs.

    base_label_cols = groups[0][1]
    anchor_col = base_label_cols[0]

    # --- Compute compound-label depth, per block ---
    #
    # This has to be decided PER BLOCK of rows (identified by which
    # physical cell anchors their label — their span_id at the first
    # label column), not once for the whole table. A column can
    # coincidentally share a rowspan with the label anchor in ONE
    # section purely because two rows happen to need the same value
    # (e.g. two rows under "OPI" both needing "10,000 SF" for Minimum
    # Required Area, saving a repeated cell) without that column being
    # part of the row's IDENTITY there — while genuinely being a real
    # grouping column in a DIFFERENT section. A single table-wide
    # decision can't represent both at once; checking each block's own
    # rows can.
    #
    # There's a second wrinkle: a block can contain an annotation/
    # footnote row that inherits a candidate column's value purely via
    # rowspan carryover (e.g. a footnote sitting between two real items,
    # inheriting "Commercial Uses" from the row above it without being a
    # second, independently distinguishable item). Counting that
    # footnote as "another row sharing this value" would wrongly signal
    # that the column needs even MORE specificity. To tell a genuine
    # repeat apart from a footnote's inherited copy, every cell's
    # "first-seen" row is tracked — only the row that ORIGINATES a given
    # physical cell counts as real evidence at each step; a row that's
    # purely carrying over an earlier row's cell there doesn't.
    first_seen_row = {}
    for i, row in enumerate(data_rows):
        for c, cell in row.items():
            sid = cell[2] if len(cell) > 2 else None
            if sid is not None and sid not in first_seen_row:
                first_seen_row[sid] = i

    block_rows = {}  # anchor span_id -> list of (row, row_index)
    for i, row in enumerate(data_rows):
        cols_present = sorted(row.keys())
        if cols_present == list(range(total_cols)):
            all_texts = {row[c][0].strip() for c in cols_present}
            if len(all_texts) == 1 and next(iter(all_texts)):
                continue  # full-width divider — not part of any block
        cell = row.get(anchor_col)
        if cell is None:
            continue
        span_id = cell[2] if len(cell) > 2 else None
        block_rows.setdefault(span_id, []).append((row, i))

    all_rows_with_idx = [(row, i) for i, row in enumerate(data_rows)]

    # --- Group value columns that share a nested "umbrella" header ---
    #
    # e.g. Front / Side / Rear all sit under one shared "Setback from
    # Property Line" header. Needed below to detect when a note spans
    # only THAT umbrella's own leaf columns (not the whole row) — see
    # "collapsed_umbrella_text" in the main loop.
    umbrella_members = {}
    for path, cols in groups[1:]:
        full_path = header_paths.get(cols[0], "")
        if " > " in full_path:
            parent_prefix = full_path.rsplit(" > ", 1)[0]
            umbrella_members.setdefault(parent_prefix, []).append((path, cols))
    umbrella_members = {k: v for k, v in umbrella_members.items() if len(v) >= 2}

    # --- Does each candidate column ever show GENUINE variation anywhere
    # in the table? ---
    #
    # The per-block absorption loop below already checks whether a
    # candidate column is nested under a shared umbrella header (if so,
    # it's a value field, never absorbed — e.g. "Front" under "Setback
    # from Property Line"). But a FLAT, standalone header can be
    # ambiguous even after that check: "Uses" and "Minimum Required
    # Area" look structurally identical — both flat headers that can be
    # uniformly rowspan-carried across a block — yet one is genuinely
    # part of a row's identity (different zoning codes pair with
    # different Uses) and the other is an ordinary value that happens to
    # get carried along because a DIFFERENT column in the same block
    # needed extra rows (e.g. a Setback note needing its own row,
    # dragging the uniformly-repeated "Minimum Required Area" along for
    # the ride).
    #
    # The two are told apart by looking at the WHOLE table rather than
    # one block in isolation: does this exact column ever show 2+
    # genuinely different, independently-originating values within any
    # single block anywhere? If some other zoning code's block shows
    # "Uses" varying (Commercial Uses vs Residential Uses on separate
    # physical rows), that's real evidence the column is part of every
    # row's identity — including in a block where THIS particular
    # occurrence happens to be uniform. If a column NEVER varies within
    # any block, anywhere, there's no evidence it's identity at all —
    # it's an ordinary value field, however it happens to be rowspan'd
    # in one specific spot.
    #
    # This deliberately checks only ONE column at a time (not a cascade
    # of absorption decisions across the whole table at once) — an
    # earlier attempt at table-wide evidence pooled decisions greedily
    # across multiple absorption levels at once and caused runaway
    # over-absorption on larger tables. Judging each column's own
    # variation independently, while leaving the existing per-block loop
    # structure otherwise untouched, avoids that failure mode.
    column_has_variation = {}
    for _, cols in groups[1:]:
        col = cols[0]
        has_variation = False
        for rows_with_idx in block_rows.values():
            if len(rows_with_idx) <= 1:
                continue
            spans = set()
            for row, idx in rows_with_idx:
                cell = row.get(col)
                if cell is None:
                    continue
                sid = cell[2] if len(cell) > 2 else None
                if sid is not None and first_seen_row.get(sid) == idx:
                    spans.add(sid)
            if len(spans) >= 2:
                has_variation = True
                break
        column_has_variation[col] = has_variation

    # One narrow, explicit carve-out: in a "Type of Property" /
    # "Length of Frontage" table (a road-frontage signage schedule),
    # "Length of Frontage" looks structurally identical to a genuine
    # identity column like "Uses" elsewhere — both are flat, standalone
    # headers that genuinely vary within a block — yet here it reads
    # better left as a plain field per physical row rather than folded
    # into the label. There's no general structural signal that tells
    # the two apart, so rather than keep patching heuristics for every
    # future table that looks like this one, this is a direct,
    # named exception for this specific column pairing.
    anchor_header = header_paths.get(anchor_col, "").strip().lower()
    absorption_disabled = anchor_header == "type of property"

    depth_by_anchor = {}
    for span_id, rows_with_idx in block_rows.items():
        depth = 0
        if absorption_disabled:
            depth_by_anchor[span_id] = depth
            continue
        current_label_cols = list(base_label_cols)
        remaining = list(groups[1:])
        # A block with only ONE row can never show repetition against
        # itself — there's nothing else in the block to compare it to.
        # A genuinely single-item category (e.g. "Wholesale Business",
        # with no rowspan at all since there's nothing to span) still
        # needs to match the SAME compound-label depth as every other
        # category at that column position, established by the table's
        # other, multi-item categories — otherwise it would be the only
        # entry inconsistently missing that level. Falling back to
        # evidence from the WHOLE table (rather than this one bare row)
        # captures that pattern.
        active_rows = rows_with_idx if len(rows_with_idx) > 1 else all_rows_with_idx
        while remaining:
            last_col = current_label_cols[-1]
            span_counts = {}
            for row, idx in active_rows:
                cell = row.get(last_col)
                if cell is None:
                    continue
                sid = cell[2] if len(cell) > 2 else None
                if sid is not None:
                    span_counts[sid] = span_counts.get(sid, 0) + 1
            multi_row_spans = sum(1 for c in span_counts.values() if c > 1)
            # Even ONE genuine multi-row grouping within this block is
            # strong evidence the current label isn't specific enough —
            # nobody applies rowspan by accident. BUT this signal alone
            # can't tell apart two cases that look IDENTICAL from inside
            # one block: a footnote/continuation row genuinely inheriting
            # an identity column (e.g. a zoning code's second row is just
            # a footnote, inheriting "Commercial Uses" from the row
            # above it — "Uses" stays part of the label) versus a plain
            # VALUE column that happens to get carried by rowspan across
            # the whole block only because a LATER column needed several
            # stacked rows (e.g. three height limits for Rural/Suburban/
            # Urban, dragging the uniform "Front Setback" along for the
            # ride). Both show the exact same "one span, whole block"
            # signature, yet need opposite answers.
            #
            # The two ARE told apart by the candidate column's own header:
            # a genuine identity/category column (like "Uses", "Use
            # Type") is conventionally a FLAT, standalone header with no
            # parent grouping. A genuine measurement/value column instead
            # sits NESTED under a shared umbrella header alongside sibling
            # value columns (e.g. "Front"/"Side"/"Rear"/"Corner" all under
            # "Setback from Property Line", or "C-N"/"C-R"/... all under
            # "Commercial Zones") — build_header_paths joins that nesting
            # with " > ". A nested header path is reliable evidence this
            # is one of a peer group of VALUE fields, not a row's
            # identity — nobody nests a category label under an umbrella
            # header shared with unrelated numeric measurements.
            #
            # That guard alone still isn't enough: a FLAT header can be
            # ambiguous too (see column_has_variation's docstring above,
            # e.g. "Minimum Required Area" — flat, but never actually
            # distinguishes anything). Requiring genuine variation
            # SOMEWHERE in the table (not just "not nested") is what
            # tells that apart from a real identity column like "Uses".
            candidate_col = remaining[0][1][0]
            candidate_header_is_nested = " > " in header_paths.get(candidate_col, "")
            candidate_has_variation = column_has_variation.get(candidate_col, False)
            if multi_row_spans > 0 and not candidate_header_is_nested and candidate_has_variation:
                _, absorbed_cols = remaining.pop(0)
                current_label_cols = current_label_cols + absorbed_cols
                depth += 1
                # From here on, only rows that genuinely ORIGINATE their
                # own content at the column just absorbed count as real
                # block members for any FURTHER check — a row purely
                # inheriting that value via rowspan carryover (like a
                # footnote continuing the row above it) isn't an
                # independently distinguishable item, so it shouldn't
                # count as evidence that we need to go even deeper.
                new_col = absorbed_cols[0]
                active_rows = [
                    (row, idx) for row, idx in active_rows
                    if row.get(new_col) is not None
                    and len(row[new_col]) > 2
                    and row[new_col][2] is not None
                    and first_seen_row.get(row[new_col][2]) == idx
                ]
            else:
                break
        depth_by_anchor[span_id] = depth

    # --- Count how many rows share each row's LEAF label span ---
    #
    # Divider checks #2b/#2c (below) need to know whether a row belongs
    # to a genuine multi-row group sharing one undifferentiated label —
    # but "sharing" has to be judged at the row's own LEAF level (after
    # compound-label absorption), not at the outer anchor. An anchor can
    # rowspan over several rows that each carry their OWN distinct
    # absorbed sub-label (e.g. one zoning code's rowspan covering a
    # "Residential Uses" row, an "Ag Bldg, Horses Only" row, and a
    # "Commercial Horse Facility" row — three genuinely different,
    # separately-identified items, each its own size-1 group at the
    # leaf level) — very different from several rows that share the
    # exact SAME leaf label with nothing distinguishing them (e.g.
    # "External buffer" rowspan-carried across 7 rows with no absorbed
    # sub-label at all, all 7 sharing one leaf span). Using the outer
    # anchor's block size for this would treat the first case as if it
    # were the second, and wrongly start swallowing "Residential Uses"'
    # correctly-standalone value as a sub-divider heading.
    leaf_block_size = {}
    for row in data_rows:
        anchor_cell = row.get(anchor_col)
        a_span = anchor_cell[2] if anchor_cell and len(anchor_cell) > 2 else None
        d = depth_by_anchor.get(a_span, 0)
        leaf_cols = list(base_label_cols)
        vgroups = list(groups[1:])
        for _ in range(d):
            if not vgroups:
                break
            _, absorbed = vgroups.pop(0)
            leaf_cols = absorbed
        cell = row.get(leaf_cols[0])
        if cell is None:
            continue
        sid = cell[2] if len(cell) > 2 else None
        if sid is None:
            continue
        leaf_block_size[sid] = leaf_block_size.get(sid, 0) + 1

    result = {}
    # section_node always points at "whichever dictionary new rows should
    # currently be added into" — starts as the top-level result, but gets
    # redirected into a nested dict whenever we hit a section divider.
    section_node = result
    row_counter = 0   # only used to disambiguate accidental duplicate labels
    # Tracks, per multi-row block (by anchor span_id), whichever
    # "OPTION 1"/"OPTION 2"-style local sub-divider is currently active
    # within that block — see divider checks #2b/#2c below.
    active_local_divider = {}

    for row in data_rows:
        cols_present = sorted(row.keys())

        # --- Divider check #1: full-width colspan (see docstring) ---
        if cols_present == list(range(total_cols)):
            all_texts = {row[c][0].strip() for c in cols_present}
            if len(all_texts) == 1 and next(iter(all_texts)):
                section_node = _get_or_create_child(result, next(iter(all_texts)))
                continue   # don't add this row itself, just move on

        # --- Determine THIS row's own label/value split ---
        # Different blocks in the same table can need a different
        # compound-label depth (see the per-block computation above) —
        # so this can't be a single value computed once for the whole
        # table. Look up which block this row belongs to (via its own
        # span_id at the anchor column) and build its label/value split
        # from that block's own depth.
        anchor_cell = row.get(anchor_col)
        anchor_span_id = anchor_cell[2] if anchor_cell and len(anchor_cell) > 2 else None
        depth = depth_by_anchor.get(anchor_span_id, 0)
        # Track each absorbed level as its own SEGMENT (rather than one
        # flat column list) so a genuine compound label — e.g. a zoning
        # code's "Uses" column, or this table's "Length of Frontage" —
        # becomes its own NESTED dict level in the output, the same way
        # "Type of Property" already does, instead of being flattened
        # into one hyphenated string. A row's true identity here is a
        # PATH (Freestanding parcel → Each additional 400 feet...), not
        # a single concatenated name.
        label_segments = [list(base_label_cols)]
        label_group_cols = list(base_label_cols)
        value_groups = list(groups[1:])
        for _ in range(depth):
            if not value_groups:
                break
            _, absorbed_cols = value_groups.pop(0)
            label_group_cols = label_group_cols + absorbed_cols
            label_segments.append(absorbed_cols)

        # Collect this row's label text (only the non-blank pieces).
        label_vals_all = [row.get(c, ("", False))[0].strip() for c in label_group_cols]
        label_vals = [v for v in label_vals_all if v]
        row_key = " - ".join(dict.fromkeys(label_vals))   # dict.fromkeys() removes duplicates while keeping order
        full_label = len(label_vals) == len(label_group_cols)  # was EVERY label column filled in?

        # Per-segment text, for nesting: one string per absorbed level
        # (a segment can itself span 2+ columns if header-text merging
        # already combined them — e.g. an item-number + item-name pair
        # sharing one header — so still join WITHIN a segment, just not
        # ACROSS segments).
        segment_texts = []
        for seg in label_segments:
            seg_vals = [v for v in (row.get(c, ("", False))[0].strip() for c in seg) if v]
            segment_texts.append(" - ".join(dict.fromkeys(seg_vals)))

        # --- Divider check #2: category-description colspan (see docstring) ---
        # A single physical cell spanning two or more value columns is
        # NOT sufficient on its own: a legitimate row-wide value looks
        # exactly the same in HTML (e.g. ADU "Processing time" with one
        # <td colspan="4">60 days...</td> — a real value for all four
        # types, not a category description). Requiring the LABEL to be
        # bold is what separates them: category rows in these tables are
        # consistently bold, ordinary data rows are not. Without this
        # guard the spanning value is silently deleted and every
        # following row is wrongly nested underneath it.
        label_is_bold = any(
            row.get(c, ("", False, None, False))[3]
            for c in label_group_cols
            if len(row.get(c, ("", False, None, False))) > 3
            and row.get(c, ("", False, None, False))[0].strip()
        )
        value_span_ids = []
        for path, cols in value_groups:
            cell = row.get(cols[0])
            if cell and cell[0].strip():
                value_span_ids.append(cell[2] if len(cell) > 2 else None)
        merged_single_cell = (
            len(value_span_ids) >= 2
            and len(set(value_span_ids)) == 1
            and value_span_ids[0] is not None
        )
        if label_is_bold and merged_single_cell:
            if row_key:
                section_node = _get_or_create_child(result, row_key)
            continue

        # `block_parent` is where an ordinary row (or the leaf label
        # itself) belongs — walk every absorbed label segment EXCEPT the
        # last as nesting, landing on the dict this whole block's
        # sibling rows share (the leaf segment then becomes that row's
        # own KEY within it, same as always).
        block_parent = section_node
        for seg_text in segment_texts[:-1]:
            if seg_text:
                block_parent = _get_or_create_child(block_parent, seg_text)
        leaf_text = segment_texts[-1] if segment_texts else ""
        # How many OTHER rows share this exact LEAF-level label span (see
        # leaf_block_size above) — used below to tell a genuinely
        # shared, undifferentiated label ("External buffer" rowspan
        # across 7 rows with no absorbed sub-label) apart from an outer
        # anchor that merely rowspans over several rows that each carry
        # their OWN distinct absorbed sub-label.
        leaf_cell = row.get(label_segments[-1][0])
        leaf_span_id = leaf_cell[2] if leaf_cell and len(leaf_cell) > 2 else None
        this_leaf_block_size = leaf_block_size.get(leaf_span_id, 1)

        # --- Divider check #2b: un-bolded local sub-divider within a
        # genuine multi-row block ---
        # Some tables mark a sub-heading purely by merging it across
        # every value column, with NO bold styling anywhere (unlike
        # check #2 above) — e.g. "OPTION 1" / "OPTION 2" headings inside
        # one "External buffer" row that rowspans all of them. The bold
        # test alone can't catch this, but there's a second signal
        # available here that check #2 doesn't have: this row belongs
        # to a genuine multi-row block sharing one undifferentiated leaf
        # label (this_leaf_block_size > 1),
        # and — critically — that SAME block also contains at least one
        # row with real, separately-celled per-column values elsewhere
        # (e.g. "6 / 6 / 30" under "Option 1"). A block that mixes real
        # column-aligned data with merged single-cell rows is strong
        # evidence the merged ones are sub-headings for the data around
        # them, not data themselves.
        #
        # A short label like "OPTION 1" is told apart from an ordinary
        # sentence describing that option (see check #2c) by whether the
        # text ends in sentence-final punctuation — headings don't,
        # prose does. This is a narrower, more exploratory heuristic
        # than the other divider checks, introduced for this specific
        # table pattern; it only fires when nothing else already claimed
        # the row, so it can't preempt a case an earlier check handles.
        merged_text = None
        if merged_single_cell:
            for path, cols in value_groups:
                cell = row.get(cols[0])
                if cell and cell[0].strip():
                    merged_text = cell[0].strip()
                    break
        looks_like_heading = bool(merged_text) and not merged_text.rstrip().endswith((".", ":", ";"))

        if merged_single_cell and this_leaf_block_size > 1 and looks_like_heading:
            # Only NOW — actually about to create a local sub-divider —
            # does the leaf label get promoted into a container (a
            # side-effecting change, so it must stay lazy: computing
            # this unconditionally for every row would plant an empty
            # placeholder under the leaf label even for tables that
            # never end up using it, corrupting the ordinary "Add this
            # row" path below for every other row in the table).
            block_own_node = _get_or_create_child(block_parent, leaf_text) if leaf_text else block_parent
            local_node = _get_or_create_child(block_own_node, merged_text)
            active_local_divider[leaf_span_id] = local_node
            continue

        # --- Divider check #2c: prose continuing the active local
        # sub-divider ---
        # A merged, sentence-like row (ends in '.', ':', or ';') right
        # after a local sub-divider (check #2b) reads as explanatory
        # text for that sub-divider, not a new heading or real data —
        # e.g. the two sentences describing what "Option 2" requires.
        # Filed the same way a purely-informational full-width row is
        # filed elsewhere in this module: as its own key with an empty
        # dict, since there's no further structure to hang under it.
        # Kept as a uniform dict-of-dicts (rather than collapsing to a
        # plain string/list) so every value in the output stays the
        # same shape — anything walking this structure can always
        # assume "this is a dict" without a type check, and a future
        # table where real data DOES turn up under a heading like this
        # needs no special-case migration to accommodate it.
        # This only fires once a local divider is already active for
        # this exact block — a merged sentence with no divider before it
        # (e.g. a genuine footnote continuing a real data row, like a
        # zoning table's "* If development ... acres.") falls through
        # to the ordinary row handling below, unchanged.
        if (
            merged_single_cell
            and this_leaf_block_size > 1
            and not looks_like_heading
            and leaf_span_id in active_local_divider
        ):
            local_node = active_local_divider[leaf_span_id]
            if merged_text not in local_node:
                local_node[merged_text] = {}
            continue

        # --- Build the value fields for this row ---
        # Two versions: `inner_check` only includes non-blank values, and
        # is used purely to detect whether this row has ANY real data at
        # all (needed for the divider checks below — a divider genuinely
        # has zero data anywhere). `inner_full` includes EVERY value
        # column explicitly, even blank ones, using "" for a blank cell —
        # because in these tables a blank cell is usually a real,
        # meaningful answer (e.g. "not permitted here"), not missing
        # information. Silently dropping it would make that fact
        # unretrievable later. inner_full is what actually gets stored,
        # once we've confirmed this row isn't a divider.
        #
        # First, check whether a NOTE spans exactly one nested umbrella
        # group's own leaf columns — e.g. "Side and Rear yards require a
        # planting screen..." spanning Front/Side/Rear under "Setback
        # from Property Line", while sibling columns outside that group
        # (Minimum Required Area, Maximum Height) stay independently
        # rowspan-carried. All of an umbrella's leaf columns sharing ONE
        # physical span_id is inherently NOT per-leaf data — genuine
        # data always uses separate cells (exactly like this same
        # table's real "25' / 30' / 30'" row two rows down). Checking
        # physical span_ids rather than the text itself keeps this
        # purely structural, so it applies broadly rather than needing
        # its own per-table carve-out.
        collapsed_umbrella_text = {}
        for parent_prefix, members in umbrella_members.items():
            leaf_cols = [cols[0] for _, cols in members]
            span_ids = set()
            merged_text = None
            all_present = True
            for c in leaf_cols:
                cell = row.get(c)
                if cell is None or not cell[0].strip():
                    all_present = False
                    break
                span_ids.add(cell[2] if len(cell) > 2 else None)
                merged_text = cell[0].strip()
            if all_present and len(span_ids) == 1 and next(iter(span_ids)) is not None:
                collapsed_umbrella_text[parent_prefix] = merged_text

        inner_check = {}
        inner_full = {}
        handled_umbrellas = set()
        for path, cols in value_groups:
            full_path = header_paths.get(cols[0], "")
            parent_prefix = full_path.rsplit(" > ", 1)[0] if " > " in full_path else None
            if parent_prefix in collapsed_umbrella_text:
                if parent_prefix in handled_umbrellas:
                    continue   # already added this umbrella's one combined entry
                handled_umbrellas.add(parent_prefix)
                key = parent_prefix
                if key in inner_full:
                    key = f"{key} (col {cols[0] + 1})"
                text = collapsed_umbrella_text[parent_prefix]
                inner_check[key] = text
                inner_full[key] = text
                continue
            vals = [row[c][0].strip() for c in cols if row.get(c, ("", False))[0].strip()]
            key = path or f"Column {cols[0] + 1}"   # fallback name if header was blank
            # Two NON-adjacent groups can share the exact same header text
            # (e.g. two separate "Notes" columns at different positions —
            # common in scraped tables). Grouping only merges ADJACENT
            # same-header columns, so these stay as separate groups, but
            # without disambiguation the second one would silently
            # overwrite the first in this dict. Append the column
            # position to any repeat so both survive.
            if key in inner_full:
                key = f"{key} (col {cols[0] + 1})"
            if vals:
                # Normally there's just one value per group; "; " join
                # only matters in the rare case multiple columns share
                # one header.
                inner_check[key] = "; ".join(dict.fromkeys(vals))
            inner_full[key] = inner_check.get(key, "")   # "" if this column was blank

        # --- Divider check #3: partial label, single spanned cell, or a
        # trailing-colon category label with no data ---
        if not inner_check:
            # Was the label built from ONE physical HTML cell spanning
            # every label column (e.g. one <td colspan="2">"Residential"</td>)
            # rather than genuinely separate cells (like a real row's blank
            # item-number cell + a separate name cell)? If so, even though
            # every label column technically has text, it's really one
            # category title, not a real multi-part label — treat it as a
            # divider, not real data.
            label_span_ids = [
                row.get(c, ("", False, None))[2]
                for c in label_group_cols
                if row.get(c, ("", False, None))[0].strip()
            ]
            label_is_one_spanned_cell = (
                len(label_group_cols) >= 2
                and len(label_span_ids) >= 2
                and len(set(label_span_ids)) == 1
                and label_span_ids[0] is not None
            )
            # Some tables mark a category label purely with a trailing
            # colon and no colspan/bold at all (e.g. "Group home:").
            # IMPORTANT: there is no reliable signal in these tables for
            # where such a category ENDS — no indentation, no distinct
            # styling — so once opened, this nests every row that follows
            # until the next real divider or the end of the table. This
            # is a deliberate, explicit choice (not a default): confirmed
            # against the source table that everything after the colon
            # label is intended to fall under it, treating the lack of a
            # closing marker as a gap in the source table's own
            # construction rather than something to work around here.
            label_ends_with_colon = row_key.rstrip().endswith(":")

            # Some tables mark a section heading with nothing but bold
            # styling on a SINGLE label column — no colspan merging it
            # across the row (label_is_one_spanned_cell only fires for a
            # 2+ column compound label) and no trailing colon. Structurally
            # that makes the label look "complete" (full_label = True,
            # since the table's whole label is just that one column), so
            # without this check a heading like "AGRICULTURAL" or
            # "RESIDENTIAL" — real section headings with every value
            # column blank — would fall through to the "real row, values
            # happen to be blank" branch below and sit as a flat sibling
            # of the rows it's meant to group, instead of opening a
            # nesting level for them.
            label_is_bold_divider = label_is_bold and row_key

            if row_key and (
                not full_label
                or label_is_one_spanned_cell
                or label_ends_with_colon
                or label_is_bold_divider
            ):
                section_node = _get_or_create_child(result, row_key.rstrip().rstrip(":").rstrip())
                continue
            elif row_key and full_label:
                # Complete, genuinely-separate label, but every value
                # column is blank — this is REAL information (e.g. "not
                # permitted in any zone"), not a divider. inner_full still
                # has every column explicitly listed as "", so this stays
                # just as informative as a row with real values.
                pass
            else:
                continue

        # --- Add this row into whichever section is currently active ---
        # Normally that's this row's own per-block parent (block_parent,
        # already computed above — walks every absorbed label segment
        # except the last as nesting, landing on the dict this block's
        # sibling rows share; the leaf label is then this row's own
        # KEY within it). But if a local sub-divider (check #2b) is
        # active for this exact block, the row nests ONE level deeper,
        # inside that sub-divider instead — e.g. the real "6 / 6 / 30"
        # data row lands inside "Option 1", not directly inside
        # "External buffer". A row nested under a local sub-divider has
        # no further identifying label of its own (its label IS the
        # block's shared anchor text, already used as the sub-divider's
        # parent key), so it falls to the same "Row N" naming used for
        # any row with a blank label.
        if leaf_span_id in active_local_divider:
            target_node = active_local_divider[leaf_span_id]
            row_counter += 1
            key = f"Row {row_counter}"
        else:
            target_node = block_parent
            row_counter += 1
            key = leaf_text or f"Row {row_counter}"
        if key in target_node:
            # Two physical rows collapsed to the exact same label — most
            # often a genuinely different row that just happens to reuse
            # the same name (e.g. two unrelated "Front" setback rows in
            # different sub-sections), which stays disambiguated with a
            # numbered suffix below. But there are two common special
            # cases worth reconciling instead of just duplicating:
            #
            # 1. A block like "Multi-family" whose rowspan carries every
            #    OTHER field identically across several physical rows,
            #    existing purely to stack multiple values for ONE
            #    trailing field (e.g. separate height limits for Rural/
            #    Suburban/Urban) — same key SET on both sides, exactly
            #    one shared field actually differs.
            # 2. Complementary rows that share every field they BOTH
            #    have, but each also contributes fields the OTHER
            #    doesn't — e.g. a merged note covering an umbrella
            #    group ("Setback from Property Line: <note>") followed
            #    by a real per-leaf-column data row ("Front"/"Side"/
            #    "Rear") for that same item. Different key sets, but
            #    nothing actually CONFLICTS between them — the second
            #    row is just filling in fields the first didn't have.
            #
            # Both are really the same underlying fact: these physical
            # rows were never separate ITEMS, just separate pieces of
            # information about the same one. Reconciling them keeps a
            # single entry instead of a numbered duplicate that repeats
            # every already-known field just to add one more.
            existing = target_node[key]
            if isinstance(existing, dict):
                shared_keys = set(existing.keys()) & set(inner_full.keys())
                new_keys = set(inner_full.keys()) - set(existing.keys())
                conflicting = [
                    field for field in shared_keys
                    if existing.get(field) != inner_full.get(field)
                ]
                if len(conflicting) <= 1 and (new_keys or conflicting):
                    for field in inner_full:   # stable insertion order, not set order
                        if field in new_keys:
                            existing[field] = inner_full[field]
                    if len(conflicting) == 1:
                        field = conflicting[0]
                        if isinstance(existing[field], list):
                            if inner_full[field] not in existing[field]:
                                existing[field].append(inner_full[field])
                        else:
                            existing[field] = [existing[field], inner_full[field]]
                    continue
            key = f"{key} ({row_counter})"
        target_node[key] = inner_full

    # Wrap every row under the label column's own header name (e.g. "Type
    # of Use", "Land Use", "USES") when that header had real text — this
    # makes explicit what the rows actually represent, instead of leaving
    # them as flat entries directly under the table's caption. If the
    # label column's header was blank (e.g. the ADU table's unlabeled
    # first column), there's no meaningful name to wrap with, so the rows
    # stay flat rather than introducing a made-up generic key.
    label_header = groups[0][0]
    if label_header:
        return {label_header: result}
    return result


def build_definition_list_yaml(data_rows: list, total_cols: int) -> dict:
    """
    Handles "definition_list" tables — ones with NO real per-column
    headers, just a caption on top and a hierarchy of labels leading to a
    single value, like a zoning regulation table:

        Site Planning
          Minimum lot dimensions
            Width: 200 feet
            Depth: 600 feet

    The key idea: the LAST column is always the value. Every column
    before it builds up a nested label. HTML represents "this row is a
    sub-item of the row above" by leaving the earlier column BLANK
    (relying on visual alignment) rather than repeating the text — so
    this function has to remember the most recent non-blank label at
    each column position and "inherit" it for blank cells.

    Returns the raw, un-wrapped result — the table's caption (if it has
    one) is handled entirely by the caller (see _convert_one_table and
    html_table_to_yaml_dict), the same way matrix mode already works.
    Keeping both modes symmetric like this means a definition-list
    table's own name gets promoted correctly wherever it's combined with
    other tables, instead of getting hidden inside a nested wrapper that
    the caller can't see and falling back to a meaningless "Table N".
    """
    root = {}
    if total_cols < 2:
        # Need at least a label column AND a value column for this to
        # make sense.
        return root

    top = root  # always build at the top level; caller wraps under caption

    # current_labels remembers, for each column position, the most recent
    # non-blank label seen there — this is what lets "Width"/"Depth" rows
    # correctly inherit "Minimum lot dimensions" from a row above them.
    current_labels = {}

    # section_node = whichever dict new label/value pairs should currently
    # be inserted into — changes when we hit a section-divider row.
    section_node = top

    for row in data_rows:
        cols_present = sorted(row.keys())
        if not cols_present:
            continue
        texts = {c: row[c][0].strip() for c in cols_present}

        # --- Section divider check ---
        # A true divider is ONE value duplicated across every column
        # (via colspan spanning the whole row width), e.g. "Site
        # Planning" or "Building". If found, open a new nested section
        # and reset any label hierarchy that was being built (a new
        # section starts fresh).
        all_texts_here = (
            {texts.get(c, "") for c in range(total_cols)}
            if cols_present == list(range(total_cols)) else None
        )
        if all_texts_here is not None and len(all_texts_here) == 1 and next(iter(all_texts_here)):
            section_text = next(iter(all_texts_here))
            section_node = _get_or_create_child(top, section_text)
            current_labels = {}
            continue

        # The value is whichever PHYSICAL cell sits rightmost in THIS
        # row — not always column (total_cols - 1). Some tables mix row
        # shapes: most rows use two colspan="2" cells reaching the full
        # width, but a sub-group might use three plain <td>s that only
        # reach column 2 of a 4-column table (never touching column 3
        # at all). Assuming every row reaches the table's overall
        # widest point would silently treat those narrower rows as
        # having no value (their real value sits before where we'd be
        # looking), or if a colspan value cell happens to also cover an
        # earlier column, part of that same value would get double-
        # counted as an extra label segment instead.
        #
        # Using span_id (see expand_html_grid) fixes both: it finds
        # every column belonging to the SAME physical cell as the
        # rightmost one, so a colspan="2" value cell is correctly
        # excluded from the label in full (not just its rightmost
        # column), and a narrower row's real last cell is used as its
        # value instead of a table-wide column index that row never
        # reaches.
        rightmost_col = cols_present[-1]
        rightmost_cell = row[rightmost_col]
        value_span_id = rightmost_cell[2] if len(rightmost_cell) > 2 else None
        if value_span_id is not None:
            value_cols = {
                c for c in cols_present
                if (row[c][2] if len(row[c]) > 2 else None) == value_span_id
            }
        else:
            value_cols = {rightmost_col}

        # If EVERY column in this row belongs to that same one physical
        # cell, there's no separate label portion at all — this row is
        # purely a label-establishing row with nothing to its right
        # (matches the "no value yet" case below), not an accidental
        # value.
        if value_cols == set(cols_present):
            value = ""
        else:
            value = texts.get(rightmost_col, "")

        # Walk every label column (everything except the columns that
        # belong to the value's own physical cell), left to right,
        # updating what we remember for each position.
        for c in sorted(c for c in cols_present if c not in value_cols):
            text = texts.get(c, "")
            if text:
                # A new non-blank label appears here — remember it, and
                # forget any deeper (further-right) labels that were
                # remembered before, since we're now on a new branch.
                current_labels[c] = text
                for deeper in [k for k in current_labels if k > c]:
                    del current_labels[deeper]
            # If text is blank, we deliberately do nothing — that's the
            # "inherit from the row above" behavior.

        # Build the final label path from whatever's currently remembered,
        # skipping any accidental exact repeats in a row.
        label_parts = []
        for c in sorted(current_labels.keys()):
            t = current_labels[c]
            if t and (not label_parts or label_parts[-1] != t):
                label_parts.append(t)

        if not value or not label_parts:
            # Either this row had no value (it was just establishing a
            # label for later rows to inherit, like "Minimum setbacks"
            # with nothing of its own), or somehow no label at all —
            # either way, nothing to actually record yet.
            continue

        _insert_path(section_node, label_parts, value)

    return root


def _compute_total_cols(grid: list, header_row_count: int) -> int:
    """
    How many columns does this table actually have?

    The naive answer — the widest row anywhere in the grid — is fragile:
    ONE malformed row (a stray extra cell from a scraping artifact) would
    inflate the count for the WHOLE table, and since every divider check
    in build_matrix_yaml/build_definition_list_yaml relies on comparing
    a row's column span against total_cols, one bad row could silently
    break divider detection for every other row too. Mode (most common
    width) guards against that.

    But mode alone has its own failure mode: a table can have a caption/
    divider row (e.g. "COMMERCIAL", one value spanning colspan="3") where
    every ORDINARY data row underneath it is a plain, unexpanded 2-cell
    label+value pair — narrower than the divider, and in the majority.
    The mode would then settle on the NARROWER width (2), and the
    divider — which genuinely needs 3 columns to be recognized as
    spanning "the whole table" — would silently stop being detected as a
    caption at all, since it would no longer appear to cover every
    column. A row deliberately styled to span the table's full width via
    colspan is a more authoritative signal of the table's true intended
    column count than how many columns ordinary data happens to need, so
    it's checked FIRST, before falling back to the mode.
    """
    from collections import Counter

    if grid:
        first_row = grid[0]
        cols_present = sorted(first_row.keys())
        if cols_present and cols_present == list(range(len(cols_present))):
            texts = {first_row[c][0].strip() for c in cols_present}
            if len(cols_present) >= 2 and len(texts) == 1 and next(iter(texts)):
                return len(cols_present)

    header_rows = grid[:header_row_count]
    widths = [max(row.keys()) + 1 for row in header_rows if row]
    if not widths:
        widths = [max(row.keys()) + 1 for row in grid if row]
    if not widths:
        return 0
    return Counter(widths).most_common(1)[0][0]


def _convert_one_table(table) -> tuple:
    """
    Runs the full per-table pipeline (grid expansion, caption splitting,
    header analysis, matrix-vs-definition-list decision, and dict
    building) for ONE already-selected <table> tag.

    Returns (caption, data_dict, dominant_group):
      caption        = "" if the table had no title/legend row.
      dominant_group = a real grouping from the table's OWN header
                        structure (e.g. "Zoning District"), used ONLY as
                        a last-resort name — see html_table_to_yaml_dict,
                        where it replaces a generic "Table N" fallback
                        specifically for an uncaptioned table sharing a
                        title with others. It's deliberately NOT folded
                        into `caption` itself: doing so caused two bad
                        side effects when tried — it got redundantly
                        appended onto a table that already had a real
                        external title, and it added an unwanted extra
                        wrapping layer on an untitled single table that
                        was already reasonably named via its own label
                        column. Keeping it separate lets the caller use
                        it only in the one situation it's meant for.

    Both modes now return their caption the SAME way (as a plain string
    for the caller to handle), rather than definition-list mode secretly
    wrapping its caption inside the returned dict. This is what lets a
    definition-list table's own real name (e.g. "Table 20-2 | Permitted
    and Specially Permitted Uses") get used correctly when combined with
    other tables, instead of falling back to a meaningless "Table N".
    """
    header_row_count, grid = expand_html_grid(table)
    if not grid:
        return "", {}, ""

    total_cols = _compute_total_cols(grid, header_row_count)

    captions, offset = split_caption_rows(grid, header_row_count, total_cols)
    real_header_rows = grid[offset:header_row_count]
    header_paths, header_note, dominant_group = build_header_paths(real_header_rows, total_cols)
    data_rows = grid[header_row_count:]

    # Some tables have NO <thead> at all (no header rows for
    # split_caption_rows to even look at), yet the very FIRST row can
    # still visually BE a caption: one single value spanning every
    # column (e.g. "OFF-STREET PARKING REQUIREMENTS"), styled just like
    # a real title, just missing the markup that would normally mark it
    # as a header row. Without this, that row gets misread as an
    # ordinary section divider — which nests new sections under the
    # table's ROOT, not under whichever section is currently active, so
    # it ends up as an empty {} sibling of the very first real
    # subsection instead of correctly wrapping the whole table.
    #
    # Only the single leading row is taken this way (never a run of
    # them): a second full-width row right after it (like "RESIDENTIAL"
    # here) is far more likely to be a genuine subsection divider than
    # a second caption line, and greedily consuming a run of them would
    # wrongly swallow real subsections in tables that have several
    # (this pattern already exists and is handled correctly elsewhere —
    # see the divider checks in build_matrix_yaml / build_definition_
    # list_yaml — so this only needs to cover the ONE leading case that
    # those checks can't distinguish from a real divider on their own).
    leading_caption = ""
    if header_row_count == 0 and data_rows:
        first_row = data_rows[0]
        cols_present = sorted(first_row.keys())
        if cols_present == list(range(total_cols)):
            all_texts = {first_row[c][0].strip() for c in cols_present}
            if len(all_texts) == 1 and next(iter(all_texts)):
                leading_caption = next(iter(all_texts))
                data_rows = data_rows[1:]

    caption = " | ".join(c for c in (leading_caption, *captions, header_note) if c)
    distinct_headers = {p for p in header_paths.values() if p}

    if not real_header_rows or len(distinct_headers) <= 1:
        fallback_caption = caption or next(iter(distinct_headers), "")
        return fallback_caption, build_definition_list_yaml(data_rows, total_cols), dominant_group
    else:
        matrix = build_matrix_yaml(data_rows, header_paths, total_cols)
        return caption, matrix, dominant_group


def _find_local_title(table) -> str:
    """
    Finds the title that actually belongs to THIS specific table, rather
    than assuming one title applies to the whole document. Real-world
    documents commonly contain MANY independently-titled sections in a
    row (e.g. "Primary residential uses.", "Accessory and incidental
    uses.", "Vibration.", ... each with their own table(s)) — using only
    the first title in the document would silently apply the wrong name
    to every section after the first, or lose the rest entirely.

    Walks backward from the table ONE element at a time — table.find_all_
    previous() already visits elements nearest-first — looking for two
    tiers of candidate:

      STRONG: <figcaption>, <div class="title">/"chunk-title", or a
      "pure bold" <p> caption (a <p> tag whose entire text content is
      just one <span class="bold">...</span>, e.g.
      <p><span class="bold">Table 7.24-1<br>Allowed Uses</span></p>) —
      these are deliberate, explicit structural markers, so the FIRST
      one found (nearest) wins immediately and the scan stops.

      WEAK: a short (<=150 char), plain paragraph with no special
      markup at all — used only as a fallback when no strong candidate
      exists anywhere in the document, since it's a much less reliable
      signal (see below).

    These two tiers are NOT treated as one combined "nearest wins"
    search: a weak match never stops the scan, and a strong match found
    LATER (farther back) always overrides a weak match found earlier
    (closer). This matters because closeness alone doesn't imply
    correctness — a table is often preceded by an ordinary introductory
    sentence (e.g. "The following is the schedule of uses for the LDR,
    MDR and MDR-X Zone Districts:") that happens to be short enough to
    pass the weak check, while the table's REAL title (e.g. a
    <div class="chunk-title">) sits a bit further back. If the scan
    stopped at the first (weak) match, that narrative sentence would
    win purely by being closer, even though the structural marker is a
    far more reliable signal of the table's actual name.
    """
    strong_match = None
    weak_match = None

    for el in table.find_all_previous():
        if strong_match is not None:
            break  # already found the most reliable possible signal

        name = getattr(el, "name", None)
        if name is None:
            continue  # skip NavigableString / comment nodes

        if name == "figcaption":
            strong_match = _cell_text(el)
            continue

        if name == "div":
            classes = el.get("class") or []
            if "title" in classes or "chunk-title" in classes:
                strong_match = _cell_text(el)
            continue

        if name == "p":
            # A <p> sitting INSIDE any table's cell (this table's own, or
            # a completely different one, like the aria-hidden decoy
            # table some sites duplicate for a sticky-header effect) is
            # ordinary cell content, never a standalone table label —
            # e.g. a decoy header cell like <th><p>Industrial</p></th>
            # would otherwise get mistaken for this table's title simply
            # because it's short and sits nearby in document order,
            # ahead of the REAL label (a <figcaption>) further back.
            if el.find_parent("table") is not None:
                continue

            bold_span = el.find("span", class_="bold")
            if bold_span is not None:
                full_text = _cell_text(el)
                if full_text and full_text == _cell_text(bold_span):
                    strong_match = full_text
                    continue

            # Some documents label a table with a plain paragraph and NO
            # special styling at all — e.g. "1. Supplemental off-street
            # parking requirements specific to districts" sitting right
            # before the table, with no bold span, no div.title, nothing
            # else to go on. A short, non-empty paragraph close to the
            # table is a REASONABLE (but weak) signal it's meant as that
            # table's label — only kept as a fallback if no strong
            # candidate ever turns up (see docstring above for why a
            # nearby weak match can't be trusted over a farther strong
            # one). Two further safeguards keep this narrow:
            #   - blank paragraphs (a lone <br>, used as visual spacing
            #     between the label and the table) are skipped rather
            #     than treated as "nothing here" — the scan keeps
            #     looking past them instead of giving up too early.
            #   - a LONG paragraph is essentially always narrative body
            #     text, not a label (e.g. multi-sentence regulatory
            #     text that happens to sit right before a table) — it's
            #     deliberately NOT treated as a match at all.
            if weak_match is None:
                plain_text = _cell_text(el)
                if plain_text and len(plain_text) <= 150:
                    weak_match = plain_text

    return strong_match or weak_match or ""

    return ""


def html_table_to_yaml_dict(html: str) -> dict:
    """
    The "decision maker": takes raw HTML — which may contain ONE table,
    a few tables under one shared title (e.g. a legend plus the actual
    data table), or MANY independently-titled sections each with their
    own table(s) (a large document like a full zoning use-schedule
    chapter) — and returns the finished nested Python dictionary.

    Every real table found gets converted independently through the same
    matrix-vs-definition-list pipeline, then paired with its own LOCAL
    title (the nearest preceding title, not one global title for the
    whole document — see _find_local_title). Tables that share the same
    local title (e.g. two small tables both following one "Vibration."
    heading with nothing in between) are grouped together under that one
    title. Tables with genuinely no title anywhere before them sit as
    their own top-level entries.
    """
    soup = BeautifulSoup(html, "html.parser")
    tables = _find_all_content_tables(soup)
    if not tables:
        return {}

    # Convert every table first, and note each one's own local title.
    converted = []  # list of (local_title, caption, data_dict, dominant_group)
    for table in tables:
        caption, data, dominant_group = _convert_one_table(table)
        if not data:
            continue
        local_title = _find_local_title(table)
        converted.append((local_title, caption, data, dominant_group))

    if not converted:
        return {}

    # Group tables that share the same local title, preserving the order
    # each title first appears in.
    groups = {}  # local_title -> list of (caption, data, dominant_group)
    order = []
    for local_title, caption, data, dominant_group in converted:
        if local_title not in groups:
            groups[local_title] = []
            order.append(local_title)
        groups[local_title].append((caption, data, dominant_group))

    result = {}
    for local_title in order:
        items = groups[local_title]

        if len(items) == 1:
            caption, data, _dominant_group = items[0]
            # Fold any in-table caption into the title itself, e.g.
            # "Doc Title | Table 4.12" — matches prior single-table
            # behavior when a table also carried its own caption row.
            # (dominant_group is intentionally NOT used here — see
            # _convert_one_table's docstring for why.)
            key = " | ".join(c for c in [local_title, caption] if c)
        else:
            # Multiple tables share this one title (e.g. two small
            # tables both under "Vibration." with nothing between them)
            # — combine them, using each table's own caption when it has
            # one, then a real grouping from its OWN header structure
            # (e.g. "Zoning District") when it doesn't, and only falling
            # back to a generic "Table N" when neither is available.
            combined = {}
            for idx, (caption, data, dominant_group) in enumerate(items, start=1):
                sub_key = caption or dominant_group or f"Table {idx}"
                if sub_key in combined:
                    sub_key = f"{sub_key} ({idx})"
                combined[sub_key] = data
            data = combined
            key = local_title

        if not key:
            # No title anywhere for this table — merge its data straight
            # into the top level rather than inventing a wrapper key.
            result.update(data)
            continue

        if key in result:
            key = f"{key} (2)"
        result[key] = data

    return result


# ---------------------------------------------------------------------------
# Batch conversion over cases/ folder
# ---------------------------------------------------------------------------

class _WideKeyDumper(yaml.Dumper):
    """
    PyYAML has a built-in rule: a mapping key over 128 characters can't
    use the normal, familiar "key: value" form — it's forced into a more
    awkward explicit-block style ("? key" on one line, ": value" on the
    next). This is purely a PyYAML formatting preference, not a real YAML
    limitation — long simple keys are perfectly valid YAML.

    These zoning tables regularly have row labels and section headers well
    over 128 characters (e.g. "Wireless communications facilities
    (subject to use-specific standards and application procedures in
    Sections 16-19-010—16-19-080)"), so without this override, any long
    label silently switches to that harder-to-read "? / :" format — easy
    to misread as a missing or broken section rather than a normal entry.
    This subclass raises that threshold generously (8192 chars) so
    ordinary long labels stay in the familiar "key: value" form, while
    still correctly falling back to the safe explicit style for scalars
    that are genuinely empty or span multiple lines.
    """
    def check_simple_key(self):
        length = 0
        if self.analysis is None and isinstance(self.event, ScalarEvent):
            self.analysis = self.analyze_scalar(self.event.value)
        if isinstance(self.event, AliasEvent):
            length += len(self.alias_key)
        elif isinstance(self.event, ScalarEvent) and self.event.value is not None:
            length += len(self.analysis.scalar)
        return (
            length < 8192
            and (
                isinstance(self.event, AliasEvent)
                or (
                    isinstance(self.event, ScalarEvent)
                    and not self.analysis.empty
                    and not self.analysis.multiline
                )
                or self.check_empty_sequence()
                or self.check_empty_mapping()
            )
        )


def _verify_wide_key_dumper():
    """
    _WideKeyDumper overrides a private PyYAML emitter method
    (check_simple_key) by copying its internal logic and changing one
    constant. That's inherently fragile: if a future PyYAML release
    changes that method's internals, this override could silently stop
    working — long keys would quietly fall back to the confusing
    "? key" / ": value" format again, with no error, no warning, just a
    slow drift back into output that looks broken.

    This is a small self-check that catches that immediately: dump a
    dict with a 200-character key and assert it comes out as a normal
    "key: value" line. Called once, right after the class is defined —
    if PyYAML's internals ever shift under us, this fails loudly at
    import time instead of letting the problem surface later as
    "confusing YAML output" in some review months from now.
    """
    long_key = "x" * 200
    output = yaml.dump(
        {long_key: "value"}, Dumper=_WideKeyDumper, sort_keys=False,
        allow_unicode=True, default_flow_style=False, width=1000, indent=2,
    )
    expected_start = f"{long_key}:"
    if not output.startswith(expected_start):
        raise AssertionError(
            "_WideKeyDumper self-check failed: a 200-character key did not "
            "render as plain 'key: value'. This likely means the installed "
            "PyYAML version changed internals that check_simple_key() "
            "depends on — long table row labels and section names will "
            "silently render in the confusing '? key' / ': value' format "
            f"again. Got:\n{output[:200]!r}"
        )


_verify_wide_key_dumper()


def to_yaml_string(data: dict) -> str:
    """
    Hands our finished Python dictionary to the `yaml` library, which
    knows how to print it out as properly indented YAML text.

      Dumper=_WideKeyDumper  -> keep long labels/section names in normal
                                "key: value" form instead of PyYAML's
                                awkward "? key" / ": value" block style
                                (see _WideKeyDumper's docstring)
      sort_keys=False       -> keep our own ordering (e.g. table row
                                order), don't alphabetize everything
      allow_unicode=True    -> let special characters (like ½ or —)
                                print as themselves, not escape codes
      default_flow_style=False -> use the multi-line indented style
                                (what you're used to seeing), not the
                                compact {a: 1, b: 2} inline style
      width=1000             -> don't wrap long regulation sentences onto
                                multiple lines
      indent=2                -> 2 spaces per nesting level
    """
    return yaml.dump(
        data,
        Dumper=_WideKeyDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=1000,
        indent=2,
    )


def main():
    """
    The actual script entry point: look through the cases/ folder for
    every file matching <id>_html.txt, convert each one, and save the
    result next to it as <id>_yaml.txt.

    Each case is converted inside its own try/except: with hundreds of
    tables in a real corpus, some are going to have HTML malformed enough
    to raise an outright exception (not just convert to the wrong thing —
    an actual crash), and one such file must not abort the whole batch
    and silently lose every case that would have come after it
    alphabetically. Failures are collected and summarized at the end
    instead, so a bad file is visible but doesn't block the rest.
    """
    if not os.path.isdir(CASES_DIR):
        raise FileNotFoundError(f"'{CASES_DIR}/' not found.")

    # Find every file ending in "_html.txt" and strip that suffix off to
    # get just the case's ID, e.g. "adu_table_html.txt" -> "adu_table".
    html_files = {f[: -len("_html.txt")] for f in os.listdir(CASES_DIR) if f.endswith("_html.txt")}
    case_ids = sorted(html_files)

    if not case_ids:
        print(f"No cases found in '{CASES_DIR}/' (looking for <id>_html.txt).")
        return

    errors = []
    for case_id in case_ids:
        html_path = os.path.join(CASES_DIR, f"{case_id}_html.txt")
        try:
            with open(html_path, encoding="utf-8") as f:
                data = html_table_to_yaml_dict(f.read())

            if not data:
                print(f"  {case_id}: no rows converted (check table structure)")
                continue

            yaml_text = to_yaml_string(data)
            out_path = os.path.join(CASES_DIR, f"{case_id}_yaml.txt")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(yaml_text)

            print(f"  {case_id}: converted -> {out_path}")
        except Exception as e:
            errors.append((case_id, e))
            print(f"  {case_id}: FAILED — {type(e).__name__}: {e}")

    if errors:
        print(f"\n{len(errors)} case(s) failed and were skipped: "
              f"{', '.join(cid for cid, _ in errors)}")


# This just means "only run main() if this file is being run directly
# (python table_to_yaml_converter.py), not if it's imported by another
# script." Standard Python convention.
if __name__ == "__main__":
    main()