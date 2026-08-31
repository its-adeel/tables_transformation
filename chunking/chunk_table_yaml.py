"""Structure-aware chunks for YAML produced by ``html_to_yaml.py``.

The converter has already recovered the meaning of an HTML table.  This
module therefore splits the resulting Python tree only at mapping boundaries;
it never applies a character window to rendered YAML.  Every chunk retains a
complete breadcrumb and the original structured evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
from typing import Any, Iterable

import yaml


LEGEND_INLINE_MAX_CHARS = 600


def _approx_tokens(text: str) -> int:
    """Conservative dependency-free estimate used only for chunk sizing."""
    return max(1, (len(text) + 3) // 4)


def _slug(text: str, maximum: int = 42) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (value or "chunk")[:maximum].rstrip("-")


def _leaf_paths(value: Any, prefix: tuple[str, ...]) -> list[tuple[str, ...]]:
    if isinstance(value, dict):
        paths: list[tuple[str, ...]] = []
        for key, child in value.items():
            paths.extend(_leaf_paths(child, (*prefix, str(key))))
        return paths
    return [prefix]


def _needs_labeled_entries(value: dict[Any, Any]) -> bool:
    """Would PyYAML render one of this mapping's keys in explicit form?"""
    return any(
        not isinstance(key, (str, int, float, bool))
        or len(str(key)) >= 120
        or "\n" in str(key)
        for key in value
    )


def _retrieval_mapping(value: dict[Any, Any]) -> Any:
    if _needs_labeled_entries(value):
        return [
            _retrieval_entry(child_label, child_value)
            for child_label, child_value in value.items()
        ]
    return {
        key: _retrieval_mapping(child) if isinstance(child, dict) else child
        for key, child in value.items()
    }


def _retrieval_entry(label: Any, value: Any) -> dict[str, Any]:
    """Represent a record's label as a value rather than a YAML key."""
    entry: dict[str, Any] = {"label": label}
    if isinstance(value, dict):
        entry["fields"] = _retrieval_mapping(value)
    else:
        entry["value"] = value
    return entry


def _retrieval_legend(legend: dict[str, Any]) -> list[dict[str, Any]]:
    entries = []
    for code, meaning in legend.items():
        if isinstance(meaning, dict) and set(meaning) == {"="}:
            meaning = meaning["="]
        entries.append({"code": code, "meaning": meaning})
    return entries


@dataclass
class Chunk:
    chunk_id: str
    document_id: str
    table_id: str
    chunk_type: str
    caption: str
    breadcrumb: list[str]
    records: list[tuple[str, Any]]
    legend_inline: dict[str, Any] | None = None
    legend_refs: list[str] = field(default_factory=list)
    source_paths: list[tuple[str, ...]] = field(default_factory=list)
    approx_tokens: int = 0
    oversized: bool = False
    part_index: int | None = None
    part_count: int | None = None

    @property
    def path(self) -> list[str]:
        return [self.caption, *self.breadcrumb]

    def render(self) -> str:
        """Return safe, self-contained text suitable for embedding or prompting."""
        lines = [f"Table: {self.caption}"]
        if self.breadcrumb:
            lines.append(f"Path: {' > '.join(self.breadcrumb)}")
        if self.part_index is not None and self.part_count is not None:
            lines.append(f"Part: {self.part_index} of {self.part_count}")
        if self.legend_inline:
            legend_yaml = yaml.safe_dump(
                _retrieval_legend(self.legend_inline),
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
                width=1000,
            ).rstrip()
            lines.extend(["", "Legend:", legend_yaml])
        elif self.legend_refs:
            lines.append(f"Legend references: {', '.join(self.legend_refs)}")

        rendered_records: list[dict[str, Any]] = []
        notes: list[str] = []
        for key, value in self.records:
            # Footer notes are represented by the converter as long labels with
            # empty mappings.  Making those labels YAML keys causes PyYAML to use
            # its visually confusing explicit-key form ("? key" / ": {}").
            if value == {}:
                notes.append(str(key))
            else:
                rendered_records.append(_retrieval_entry(key, value))
        evidence: dict[str, Any] = {}
        if rendered_records:
            evidence["Records"] = rendered_records
        if notes:
            evidence["Notes"] = notes
        evidence_yaml = yaml.safe_dump(
            evidence,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
            width=1000,
        ).rstrip()
        lines.extend(["", "Evidence:", evidence_yaml])
        return "\n".join(lines)


def _legend_mapping(caption: str, node: Any) -> dict[str, Any] | None:
    """Recognise explicit legends and conservative code-to-meaning tables."""
    if not isinstance(node, dict) or not node:
        return None

    title_signal = bool(re.search(r"\b(legend|key to symbols?)\b", caption, re.I))
    compact: dict[str, Any] = {}
    compact_shape = len(node) <= 30
    equality_shape = True
    for key, value in node.items():
        if isinstance(value, dict) and set(value) == {"="}:
            meaning = value["="]
        elif not isinstance(value, dict):
            meaning = value
            equality_shape = False
        else:
            compact_shape = False
            equality_shape = False
            continue
        compact[str(key)] = meaning
        if len(str(key)) > 24:
            compact_shape = False

    if title_signal:
        return node
    if equality_shape and compact_shape and len(compact) >= 2 and len(compact) == len(node):
        return node
    return None


class _Builder:
    def __init__(
        self,
        document_id: str,
        token_budget: int,
        legend_inline_max_chars: int,
    ) -> None:
        if token_budget < 32:
            raise ValueError("token_budget must be at least 32")
        self.document_id = document_id
        self.token_budget = token_budget
        self.legend_inline_max_chars = legend_inline_max_chars
        self.chunks: list[Chunk] = []
        self._ids: set[str] = set()

    def _new_id(
        self,
        table_id: str,
        chunk_type: str,
        caption: str,
        breadcrumb: list[str],
        records: list[tuple[str, Any]],
        part_index: int | None,
    ) -> str:
        labels = "|".join(str(key) for key, _ in records)
        identity = "\x1f".join(
            [table_id, chunk_type, caption, *breadcrumb, labels, str(part_index or "")]
        )
        digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:10]
        human = _slug(breadcrumb[-1] if breadcrumb else (labels or caption))
        candidate = f"{table_id}__{chunk_type}__{human}__{digest}"
        if candidate not in self._ids:
            self._ids.add(candidate)
            return candidate
        suffix = 2
        while f"{candidate}-{suffix}" in self._ids:
            suffix += 1
        candidate = f"{candidate}-{suffix}"
        self._ids.add(candidate)
        return candidate

    def make_chunk(
        self,
        *,
        table_id: str,
        chunk_type: str,
        caption: str,
        breadcrumb: list[str],
        records: list[tuple[str, Any]],
        legend_inline: dict[str, Any] | None,
        legend_refs: list[str],
        part_index: int | None = None,
        part_count: int | None = None,
        force_oversized: bool = False,
    ) -> Chunk:
        paths: list[tuple[str, ...]] = []
        for key, value in records:
            paths.extend(_leaf_paths(value, (caption, *breadcrumb, str(key))))
        chunk = Chunk(
            chunk_id=self._new_id(
                table_id, chunk_type, caption, breadcrumb, records, part_index
            ),
            document_id=self.document_id,
            table_id=table_id,
            chunk_type=chunk_type,
            caption=caption,
            breadcrumb=list(breadcrumb),
            records=list(records),
            legend_inline=legend_inline,
            legend_refs=list(legend_refs),
            source_paths=paths,
            part_index=part_index,
            part_count=part_count,
        )
        chunk.approx_tokens = _approx_tokens(chunk.render())
        chunk.oversized = force_oversized or chunk.approx_tokens > self.token_budget
        return chunk

    def fits(self, **kwargs: Any) -> bool:
        chunk = self.make_chunk(**kwargs)
        self._ids.remove(chunk.chunk_id)
        return chunk.approx_tokens <= self.token_budget

    def emit_packed(
        self,
        *,
        table_id: str,
        caption: str,
        breadcrumb: list[str],
        records: Iterable[tuple[str, Any]],
        legend_inline: dict[str, Any] | None,
        legend_refs: list[str],
        chunk_type: str = "record",
    ) -> None:
        pending: list[tuple[str, Any]] = []
        common = dict(
            table_id=table_id,
            chunk_type=chunk_type,
            caption=caption,
            breadcrumb=breadcrumb,
            legend_inline=legend_inline,
            legend_refs=legend_refs,
        )
        for record in records:
            candidate = [*pending, record]
            if pending and not self.fits(records=candidate, **common):
                self.chunks.append(self.make_chunk(records=pending, **common))
                pending = []
            if not pending and not self.fits(records=[record], **common):
                self.emit_oversized_record(records=[record], **common)
            else:
                pending.append(record)
        if pending:
            self.chunks.append(self.make_chunk(records=pending, **common))

    def emit_oversized_record(self, *, records: list[tuple[str, Any]], **common: Any) -> None:
        """Split an oversized mapping by fields, then long scalar text by words."""
        record_key, value = records[0]
        if not isinstance(value, dict) or not value:
            self._emit_long_scalar(record_key, value, None, **common)
            return

        fields: list[tuple[str, Any]] = list(value.items())
        pending: dict[str, Any] = {}
        parts: list[tuple[str, Any]] = []
        for field_key, field_value in fields:
            candidate = {**pending, field_key: field_value}
            if pending and not self.fits(records=[(record_key, candidate)], **common):
                parts.append((record_key, pending))
                pending = {}
            if not pending and not self.fits(
                records=[(record_key, {field_key: field_value})],
                part_index=999,
                part_count=999,
                **common,
            ):
                self._emit_long_scalar(record_key, field_value, field_key, **common)
            else:
                pending[field_key] = field_value
        if pending:
            parts.append((record_key, pending))

        part_count = len(parts)
        for index, part in enumerate(parts, start=1):
            self.chunks.append(
                self.make_chunk(
                    records=[part],
                    part_index=index if part_count > 1 else None,
                    part_count=part_count if part_count > 1 else None,
                    **common,
                )
            )

    def _emit_long_scalar(
        self,
        record_key: str,
        value: Any,
        field_key: str | None,
        **common: Any,
    ) -> None:
        if not isinstance(value, str) or not value.strip():
            self.chunks.append(
                self.make_chunk(
                    records=[(record_key, {field_key: value} if field_key else value)],
                    force_oversized=True,
                    **common,
                )
            )
            return

        pieces = re.findall(r"\S+\s*", value)
        segments: list[str] = []
        current = ""
        for piece in pieces:
            candidate = current + piece
            candidate_value: Any = {field_key: candidate} if field_key else candidate
            if current and not self.fits(
                records=[(record_key, candidate_value)],
                part_index=999,
                part_count=999,
                **common,
            ):
                segments.append(current)
                current = piece
            else:
                current = candidate
        if current:
            segments.append(current)

        count = len(segments)
        for index, segment in enumerate(segments, start=1):
            segment_value: Any = {field_key: segment} if field_key else segment
            self.chunks.append(
                self.make_chunk(
                    records=[(record_key, segment_value)],
                    part_index=index if count > 1 else None,
                    part_count=count if count > 1 else None,
                    force_oversized=False,
                    **common,
                )
            )

    def walk(
        self,
        *,
        table_id: str,
        caption: str,
        node: dict[str, Any],
        breadcrumb: list[str],
        legend_inline: dict[str, Any] | None,
        legend_refs: list[str],
    ) -> None:
        common = dict(
            table_id=table_id,
            caption=caption,
            breadcrumb=breadcrumb,
            legend_inline=legend_inline,
            legend_refs=legend_refs,
        )

        fitted_records = (
            list(node.items())
            if not breadcrumb
            else [(breadcrumb[-1], node)]
        )
        record_breadcrumb = breadcrumb[:-1] if breadcrumb else []
        fitted_common = {
            **common,
            "breadcrumb": record_breadcrumb,
            "chunk_type": "record",
        }
        if self.fits(records=fitted_records, **fitted_common):
            self.emit_packed(
                table_id=table_id,
                caption=caption,
                breadcrumb=record_breadcrumb,
                records=fitted_records,
                legend_inline=legend_inline,
                legend_refs=legend_refs,
            )
            return

        direct_fields: dict[str, Any] = {}
        child_mappings: list[tuple[str, dict[str, Any]]] = []
        for key, value in node.items():
            if isinstance(value, dict):
                child_mappings.append((str(key), value))
            else:
                direct_fields[str(key)] = value

        if direct_fields:
            direct_records = (
                [(breadcrumb[-1], direct_fields)]
                if breadcrumb
                else list(direct_fields.items())
            )
            record_breadcrumb = breadcrumb[:-1] if breadcrumb else []
            self.emit_packed(
                table_id=table_id,
                caption=caption,
                breadcrumb=record_breadcrumb,
                records=direct_records,
                legend_inline=legend_inline,
                legend_refs=legend_refs,
            )

        pending_children: list[tuple[str, Any]] = []
        for key, value in child_mappings:
            child_common = dict(
                table_id=table_id,
                chunk_type="record",
                caption=caption,
                breadcrumb=breadcrumb,
                legend_inline=legend_inline,
                legend_refs=legend_refs,
            )
            if self.fits(records=[(key, value)], **child_common):
                pending_children.append((key, value))
                continue
            if pending_children:
                self.emit_packed(records=pending_children, **common)
                pending_children = []
            self.walk(
                table_id=table_id,
                caption=caption,
                node=value,
                breadcrumb=[*breadcrumb, key],
                legend_inline=legend_inline,
                legend_refs=legend_refs,
            )
        if pending_children:
            self.emit_packed(records=pending_children, **common)


def chunk_table(
    data: dict[str, Any],
    table_id: str,
    token_budget: int = 350,
    legend_inline_max_chars: int = LEGEND_INLINE_MAX_CHARS,
) -> list[Chunk]:
    """Chunk every top-level table/section in one converted YAML document."""
    if not isinstance(data, dict) or not data:
        return []

    builder = _Builder(table_id, token_budget, legend_inline_max_chars)
    entries = [(str(caption), root) for caption, root in data.items()]

    legends: list[tuple[str, dict[str, Any]]] = []
    data_entries: list[tuple[str, Any]] = []
    for caption, root in entries:
        legend = _legend_mapping(caption, root)
        if legend is not None:
            legends.append((caption, legend))
        else:
            data_entries.append((caption, root))

    legend_refs: list[str] = []
    combined_inline: dict[str, Any] = {}
    for index, (caption, legend) in enumerate(legends, start=1):
        context_table_id = f"{table_id}__legend_{index:03d}"
        first_context_index = len(builder.chunks)
        builder.emit_packed(
            table_id=context_table_id,
            chunk_type="legend",
            caption=caption,
            breadcrumb=[],
            records=legend.items(),
            legend_inline=None,
            legend_refs=[],
        )
        legend_refs.extend(
            chunk.chunk_id for chunk in builder.chunks[first_context_index:]
        )
        combined_inline.update(legend)

    inline_text_length = len(
        yaml.safe_dump(combined_inline, allow_unicode=True, sort_keys=False)
    ) if combined_inline else 0
    legend_inline = (
        combined_inline
        if combined_inline and inline_text_length <= legend_inline_max_chars
        else None
    )
    data_legend_refs = [] if legend_inline else legend_refs

    for index, (caption, root) in enumerate(data_entries, start=1):
        subtable_id = table_id if len(data_entries) == 1 else f"{table_id}__table_{index:03d}"
        if isinstance(root, dict):
            nested_legend = root.get("Legend")
            body = root
            local_inline = legend_inline
            local_refs = list(data_legend_refs)
            if isinstance(nested_legend, dict):
                body = {key: value for key, value in root.items() if key != "Legend"}
                nested_id = f"{subtable_id}__legend"
                first_nested_index = len(builder.chunks)
                builder.emit_packed(
                    table_id=nested_id,
                    chunk_type="legend",
                    caption=caption,
                    breadcrumb=[],
                    records=[("Legend", nested_legend)],
                    legend_inline=None,
                    legend_refs=[],
                )
                nested_refs = [
                    chunk.chunk_id for chunk in builder.chunks[first_nested_index:]
                ]
                nested_length = len(yaml.safe_dump(nested_legend, allow_unicode=True))
                if nested_length <= legend_inline_max_chars:
                    local_inline = {**(local_inline or {}), **nested_legend}
                else:
                    local_refs.extend(nested_refs)
            if body:
                builder.walk(
                    table_id=subtable_id,
                    caption=caption,
                    node=body,
                    breadcrumb=[],
                    legend_inline=local_inline,
                    legend_refs=local_refs,
                )
        else:
            builder.emit_packed(
                table_id=subtable_id,
                caption=caption,
                breadcrumb=[],
                records=[(caption, root)],
                legend_inline=legend_inline,
                legend_refs=data_legend_refs,
            )

    return builder.chunks


def chunk_manifest(chunks: Iterable[Chunk]) -> list[dict[str, Any]]:
    """Return serialisable chunk metadata plus embedding/evidence text."""
    manifest = []
    for chunk in chunks:
        manifest.append(
            {
                "chunk_id": chunk.chunk_id,
                "document_id": chunk.document_id,
                "table_id": chunk.table_id,
                "chunk_type": chunk.chunk_type,
                "caption": chunk.caption,
                "breadcrumb": chunk.breadcrumb,
                "legend_refs": chunk.legend_refs,
                "source_paths": [list(path) for path in chunk.source_paths],
                "approx_tokens": chunk.approx_tokens,
                "oversized": chunk.oversized,
                "part_index": chunk.part_index,
                "part_count": chunk.part_count,
                "records": [{key: value} for key, value in chunk.records],
                "text": chunk.render(),
            }
        )
    return manifest
