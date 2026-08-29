# HTML zoning tables to YAML

This project converts real-world HTML tables into structured, readable YAML. It
is designed around zoning and land-use documents, where tables are rarely simple
rectangular datasets: headers span multiple columns, labels span multiple rows,
section dividers are mixed with data, and one HTML document may contain several
independently titled tables.

The converter first expands the HTML into a complete logical grid, resolving
`rowspan` and `colspan`. It then infers the table's structure and produces nested
YAML while preserving the source order and visible hierarchy.

The repository also includes a golden-file regression suite. Every bug fix is
tested against the complete collection of real HTML cases so that improving one
table does not silently corrupt another.

## What it handles

- Multi-row and multi-column header grids
- `rowspan` and `colspan` expansion
- Matrix tables with row labels and distinct data columns
- Definition-list tables with hierarchical labels and values
- Compound and indented row labels
- Section headings, subsection dividers, legends, and note rows
- Titles and captions associated with their local table
- Multiple content tables in one HTML document
- Scraped HTML that uses CSS classes instead of semantic `<th>` elements
- Unicode text and long zoning regulation labels
- Decorative or duplicated sticky-header tables that should not become data

Because zoning documents vary considerably, structure recognition is
heuristic. The regression corpus is the source of truth for supported
real-world patterns.

## Project structure

```text
.
├── html_to_yaml.py          # HTML parsing, grid expansion, inference, and YAML output
├── regression_test.py       # Byte-for-byte golden-file regression harness
├── requirements.txt         # Python dependencies
├── table links.csv          # Source URL and table name for each tested case
└── cases/
    ├── t_001_html.txt       # Raw HTML input
    ├── t_001_yaml.txt       # Current converter output
    └── golden/
        └── t_001_yaml.txt   # Reviewed expected output
```

Each input file must follow this naming convention:

```text
cases/<case_id>_html.txt
```

Running the converter writes:

```text
cases/<case_id>_yaml.txt
```

## Requirements

- Python 3
- [Beautiful Soup](https://www.crummy.com/software/BeautifulSoup/)
- [PyYAML](https://pyyaml.org/)

Install the declared dependencies:

```bash
python -m pip install -r requirements.txt
```

## Convert the tables

Place one or more raw HTML inputs in `cases/`, then run:

```bash
python html_to_yaml.py
```

The script discovers every `*_html.txt` file, converts each case independently,
and writes the corresponding `*_yaml.txt` file. A malformed case is reported
without preventing later cases from being processed.

The module can also be used from Python:

```python
from html_to_yaml import html_table_to_yaml_dict, to_yaml_string

html = """
<table>
  <tr><th>Use</th><th>R-1</th><th>R-2</th></tr>
  <tr><td>Community garden</td><td>P</td><td>P</td></tr>
</table>
"""

data = html_table_to_yaml_dict(html)
print(to_yaml_string(data))
```

`html_table_to_yaml_dict()` returns a normal Python dictionary.
`to_yaml_string()` serializes it as ordered, Unicode-friendly YAML.

## Run the regression suite

Check every current conversion against its reviewed golden file:

```bash
python regression_test.py
```

The harness compares output byte-for-byte. If a case changes, it prints a
unified diff showing the expected and actual YAML. Empty conversion output is
also reported as a failure signal instead of being silently ignored.

At the time of writing, the suite contains 50 real-world cases.

## Tested table sources

[table links.csv](table%20links.csv) is the index of source tables represented
by the regression corpus. Each completed row records:

- the case ID used by files in `cases/`, such as `t_001`;
- the original web page containing the table; and
- the table name or heading on that page.

This mapping makes it possible to trace a generated YAML structure back to the
real table it represents. The currently populated entries cover `t_001` through
`t_050`.

When adding a new regression case, add its source URL and table name to this CSV
using the same case ID as its `<case_id>_html.txt` and golden YAML files. If a
source page contains multiple tables, record the specific table title rather
than only the page title.

## Fixing a table edge case

The expected workflow is deliberately conservative:

1. Add or identify the smallest real HTML case that demonstrates the bug.
2. Run `python regression_test.py` and inspect the failure.
3. Trace the source HTML through grid expansion and structure inference.
4. Make a focused fix in `html_to_yaml.py`.
5. Run the full regression suite, not only the affected case.
6. Review every YAML diff against the original HTML.
7. Accept new goldens only after the changed output is confirmed correct.

To re-freeze reviewed output:

```bash
python regression_test.py --accept
```

Do **not** use `--accept` merely to make a failing test pass. Golden files
protect content, not just YAML syntax; accepting an incorrect diff turns silent
data loss into expected behavior.

After accepting, run the suite once more:

```bash
python regression_test.py
```

It should report zero failed cases and zero empty outputs.

## Design overview

The conversion pipeline has four broad stages:

1. **Find content tables** — ignore known presentation duplicates and locate
   the meaningful tables in the document.
2. **Expand the grid** — resolve merged cells into their logical row and column
   positions while retaining metadata used during inference.
3. **Infer structure** — distinguish matrix tables from definition-list tables,
   construct header paths, and interpret labels, dividers, legends, and notes.
4. **Serialize YAML** — preserve document order, Unicode characters, and long
   labels in a readable nested representation.

The implementation favors evidence present in the HTML over assumptions tied to
one municipality or one table. When two visual constructs share the same HTML
shape, formatting and surrounding rows may be used to distinguish their roles.

