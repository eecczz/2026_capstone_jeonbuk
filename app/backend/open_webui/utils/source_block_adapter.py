"""
13.0 Source Block Adapter + 13.3 Preserve Index Computation.

- text_blob_to_source_blocks: source text → source_blocks (debug-only)
- compute_preserve_indices: target_unit_plan → slot/attachment indices to preserve
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


def compute_preserve_indices(
    target_unit_plan: dict,
    idx_map: dict | None = None,
) -> tuple[set[int], dict]:
    """
    target_unit_plan에서 slot/attachment region의 paragraph indices를 추출.
    shallow route에서 assemble의 preserve_indices로 전달하여 보존.

    Args:
        target_unit_plan: structure["target_unit_plan"] (cached)
        idx_map: AI idx → real idx 변환 (있으면 적용)

    Returns:
        (preserve_set, debug_dict)
    """
    preserve = set()
    debug_regions = []

    regions = target_unit_plan.get("regions", [])
    if not regions:
        regions = target_unit_plan.get("ai_plan", {}).get("regions", [])

    shallow_indices = set()

    for r in regions:
        ut = r.get("unit_type", "")
        pi = r.get("paragraph_indices", [])

        if ut in ("slot", "attachment"):
            real_indices = set()
            for idx in pi:
                real_idx = idx_map.get(idx, idx) if idx_map else idx
                real_indices.add(real_idx)
            preserve |= real_indices
            debug_regions.append({
                "region_id": r.get("region_id"),
                "unit_type": ut,
                "paragraph_indices": sorted(real_indices),
            })
        elif ut == "shallow_block":
            for idx in pi:
                real_idx = idx_map.get(idx, idx) if idx_map else idx
                shallow_indices.add(real_idx)

    debug = {
        "preserve_source": "target_unit_plan",
        "preserved_regions": debug_regions,
        "preserve_indices": sorted(preserve),
        "shallow_region_indices": sorted(shallow_indices),
    }

    return preserve, debug
