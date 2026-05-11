"""
13.0 Source Block Adapter — debug-only source structure observation.

Converts a source text blob into a list of source_blocks (minimal dict format).
Does NOT affect generation output. Results are written to 16_source_blocks.json.
"""

import re
import logging

log = logging.getLogger(__name__)

# Heading patterns ordered by specificity
_HEADING_PATTERNS = [
    # Markdown headings
    re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE),
    # Korean roman numerals: Ⅰ. Ⅱ. etc.
    re.compile(r"^([ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ][.．])\s*(.+)$", re.MULTILINE),
    # Arabic with dot: 1. 2. 3. (only at line start, short lines)
    re.compile(r"^(\d+[.．])\s+(.{2,50})$", re.MULTILINE),
]


def text_blob_to_source_blocks(text: str) -> list[dict]:
    """
    Split source text into source_blocks.

    Strategy:
    1. Try heading-based split (markdown, roman, arabic patterns)
    2. If no headings found → single broad block

    Returns:
        list of dicts with keys: source_block_id, content, order_index, heading_path
    """
    if not text or not text.strip():
        return []

    # Try each heading pattern
    for pattern in _HEADING_PATTERNS:
        blocks = _split_by_pattern(text, pattern)
        if blocks and len(blocks) > 1:
            log.info(
                f"source_block_adapter: split into {len(blocks)} blocks "
                f"using pattern {pattern.pattern[:30]}"
            )
            return blocks

    # No headings found → single broad block
    log.info("source_block_adapter: no headings found, returning single broad block")
    return [
        {
            "source_block_id": "sb_000",
            "content": text.strip(),
            "order_index": 0,
            "heading_path": [],
        }
    ]


def _split_by_pattern(text: str, pattern: re.Pattern) -> list[dict]:
    """Split text by a heading pattern. Returns blocks or empty list if < 2 matches."""
    matches = list(pattern.finditer(text))
    if len(matches) < 2:
        return []

    blocks = []

    # Content before first heading (if any)
    pre_content = text[: matches[0].start()].strip()
    if pre_content:
        blocks.append(
            {
                "source_block_id": f"sb_{len(blocks):03d}",
                "content": pre_content,
                "order_index": len(blocks),
                "heading_path": [],
            }
        )

    # Each heading → next heading
    for i, match in enumerate(matches):
        heading_text = match.group(2).strip() if match.lastindex >= 2 else match.group(0).strip()
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[start:end].strip()

        blocks.append(
            {
                "source_block_id": f"sb_{len(blocks):03d}",
                "content": content,
                "order_index": len(blocks),
                "heading_path": [heading_text],
            }
        )

    return blocks
