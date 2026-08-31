from html_to_yaml import html_table_to_yaml_dict
from chunk_table_yaml import chunk_table

TABLE_ID = 't_034'   # <-- change this
html = open(f'cases/{TABLE_ID}_html.txt').read()
data = html_table_to_yaml_dict(html)
chunks = chunk_table(data, table_id=TABLE_ID)

print(f'{TABLE_ID}: {len(chunks)} chunks')
for i, c in enumerate(chunks):
    print(f'--- chunk {i}  ({c.approx_tokens} tokens, {len(c.records)} records) ---')
    print(c.render())
    print()