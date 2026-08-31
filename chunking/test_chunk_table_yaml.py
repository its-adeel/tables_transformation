import glob
import os
import unittest

from chunk_table_yaml import chunk_table
from html_to_yaml import html_table_to_yaml_dict


HERE = os.path.dirname(os.path.abspath(__file__))


def scalar_paths(value, prefix=()):
    if isinstance(value, dict):
        paths = []
        for key, child in value.items():
            paths.extend(scalar_paths(child, (*prefix, str(key))))
        return paths
    return [prefix]


def scalar_facts(value, prefix=()):
    if isinstance(value, dict):
        facts = {}
        for key, child in value.items():
            facts.update(scalar_facts(child, (*prefix, str(key))))
        return facts
    return {prefix: value}


def observed_facts(chunks):
    facts = {}
    for chunk in chunks:
        for key, value in chunk.records:
            prefix = (chunk.caption, *chunk.breadcrumb, str(key))
            for path, scalar in scalar_facts(value, prefix).items():
                facts.setdefault(path, []).append(scalar)
    return facts


class ChunkTableYamlTests(unittest.TestCase):
    def test_processes_every_top_level_table(self):
        data = {
            "First table": {"Row A": {"Zone": "P"}},
            "Second table": {"Row B": {"Zone": "C"}},
        }
        chunks = chunk_table(data, "doc")
        captions = {chunk.caption for chunk in chunks if chunk.chunk_type == "record"}
        self.assertEqual(captions, {"First table", "Second table"})

    def test_mixed_scalar_and_mapping_node_is_lossless(self):
        data = {
            "District": {
                "RU-1": {
                    "Lot Width": "150",
                    "Lot Area": "43,560",
                    "Area Per Family": {
                        "Single Family": "43,560",
                        "Two Family": "NA",
                    },
                }
            }
        }
        chunks = chunk_table(data, "mixed")
        observed = {path for chunk in chunks for path in chunk.source_paths}
        self.assertEqual(observed, set(scalar_paths(data)))

    def test_chunk_ids_are_deterministic(self):
        data = {"Uses": {"Residential": {"Duplex": {"R1": "C", "R2": "P"}}}}
        first = [chunk.chunk_id for chunk in chunk_table(data, "stable")]
        second = [chunk.chunk_id for chunk in chunk_table(data, "stable")]
        self.assertEqual(first, second)

    def test_large_legend_has_resolvable_reference(self):
        legend = {f"CODE-{index}": "meaning " * 15 for index in range(20)}
        data = {"Legend": legend, "Uses": {"Duplex": {"R1": "P"}}}
        chunks = chunk_table(data, "legend", legend_inline_max_chars=20)
        legend_ids = {chunk.chunk_id for chunk in chunks if chunk.chunk_type == "legend"}
        record_chunks = [chunk for chunk in chunks if chunk.chunk_type == "record"]
        self.assertTrue(legend_ids)
        self.assertTrue(record_chunks)
        self.assertTrue(record_chunks[0].legend_refs)
        self.assertTrue(set(record_chunks[0].legend_refs) <= legend_ids)

    def test_long_empty_mapping_is_rendered_as_a_note(self):
        note = (
            "* Standard ranges for width provided. Minimum right-of-way widths "
            "may vary based on the Streetscape Zone and adopted plans."
        )
        chunks = chunk_table({"Street Standards": {note: {}}}, "notes")
        self.assertEqual(len(chunks), 1)
        rendered = chunks[0].render()
        self.assertIn("Notes:", rendered)
        self.assertIn(note, rendered)
        self.assertNotIn("\n? ", rendered)
        self.assertNotIn("\n: {}", rendered)

    def test_all_real_cases_preserve_every_scalar_path(self):
        html_files = sorted(glob.glob(os.path.join(HERE, "cases", "t_*_html.txt")))
        self.assertEqual(len(html_files), 50)
        for html_path in html_files:
            case_id = os.path.basename(html_path).removesuffix("_html.txt")
            with self.subTest(case_id=case_id):
                with open(html_path, encoding="utf-8") as source:
                    data = html_table_to_yaml_dict(source.read())
                chunks = chunk_table(data, case_id)
                expected = set(scalar_paths(data))
                observed = {path for chunk in chunks for path in chunk.source_paths}
                self.assertEqual(observed, expected)
                expected_facts = scalar_facts(data)
                actual_facts = observed_facts(chunks)
                for path, expected_value in expected_facts.items():
                    fragments = actual_facts[path]
                    if len(fragments) == 1:
                        self.assertEqual(fragments[0], expected_value)
                    else:
                        self.assertIsInstance(expected_value, str)
                        self.assertTrue(all(isinstance(part, str) for part in fragments))
                        self.assertEqual("".join(fragments), expected_value)
                self.assertTrue(chunks)
                self.assertTrue(all(
                    chunk.approx_tokens <= 350 or chunk.oversized
                    for chunk in chunks
                ))
                self.assertTrue(all("\n? " not in chunk.render() for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
