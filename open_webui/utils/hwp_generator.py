"""
HWPX 문서 생성 모듈

1) generate_hwpx()         — 기존 방식: 하드코딩 템플릿 (도청동향보고서)
2) generate_hwpx_dynamic() — 신규 방식: AI 명령 JSON 기반 동적 생성

python-hwpx 라이브러리를 사용하여 문단 스타일을 올바르게 적용합니다.
"""

import io
import os
import logging
from datetime import datetime
from typing import Optional

from hwpx import HwpxDocument
from open_webui.env import GLOBAL_LOG_LEVEL

log = logging.getLogger(__name__)
log.setLevel(GLOBAL_LOG_LEVEL)

# 템플릿 디렉토리
TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "hwpx_templates")

# 본문 스타일 매핑 (템플릿에서 분석한 값)
STYLES = {
    "heading": {"paraPrIDRef": "0", "charPrIDRef": "12"},       # □ 대제목 (첫 번째)
    "heading_cont": {"paraPrIDRef": "39", "charPrIDRef": "12"}, # □ 대제목 (이후)
    "item": {"paraPrIDRef": "40", "charPrIDRef": "24"},         # ○ 중항목
    "subitem": {"paraPrIDRef": "46", "charPrIDRef": "24"},      # - 소항목
    "body": {"paraPrIDRef": "40", "charPrIDRef": "24"},         # 일반 본문
    "blank": {"paraPrIDRef": "48", "charPrIDRef": "41"},        # 빈 줄 (항목 간 구분)
}

# 본문 시작 문단 인덱스 (템플릿 구조 기반)
BODY_START_INDEX = 15


def _get_today_str() -> str:
    """오늘 날짜를 도청 양식에 맞게 반환"""
    now = datetime.now()
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    wd = weekdays[now.weekday()]
    return f"'{now.strftime('%y')}. {now.month:2d}. {now.day:2d}.({wd})"


def _detect_line_style(line: str, heading_count: int) -> tuple[dict, str, int]:
    """줄 내용에 따라 스타일 자동 감지. (style_dict, formatted_text, heading_count) 반환"""
    stripped = line.strip()
    if stripped.startswith("□"):
        heading_count += 1
        if heading_count == 1:
            return STYLES["heading"], stripped, heading_count
        return STYLES["heading_cont"], stripped, heading_count
    elif stripped.startswith("○") or stripped.startswith("ㅇ"):
        return STYLES["item"], f" {stripped}", heading_count
    elif stripped.startswith("-") or stripped.startswith("·"):
        return STYLES["subitem"], f"   {stripped}", heading_count
    else:
        return STYLES["body"], f"   {stripped}", heading_count


def generate_hwpx(
    content: str,
    doc_title: str,
    template_type: str = "default",
    date_str: Optional[str] = None,
) -> bytes:
    """
    HWPX 문서를 생성하여 bytes로 반환합니다.

    Args:
        content: 본문 텍스트 (□○- 기호 포함)
        doc_title: 문서 제목
        template_type: 템플릿 종류 (default)
        date_str: 날짜 문자열 (None이면 오늘 날짜)

    Returns:
        HWPX 파일의 bytes
    """
    template_path = os.path.join(TEMPLATE_DIR, f"{template_type}.hwpx")
    if not os.path.exists(template_path):
        log.warning(f"템플릿 {template_type} 없음, default 사용")
        template_path = os.path.join(TEMPLATE_DIR, "default.hwpx")

    if not os.path.exists(template_path):
        raise FileNotFoundError(f"템플릿 파일을 찾을 수 없습니다: {template_path}")

    doc = HwpxDocument.open(template_path)

    # 1) 날짜 교체 — 표[0] cell(0,1)
    if date_str is None:
        date_str = _get_today_str()
    try:
        date_table = doc.paragraphs[0].tables[0]
        date_table.set_cell_text(0, 1, date_str)
    except (IndexError, AttributeError) as e:
        log.warning(f"날짜 교체 실패: {e}")

    # 2) 제목 교체 — 표[2] cell(1,0)
    try:
        title_table = doc.paragraphs[2].tables[0]
        title_table.set_cell_text(1, 0, doc_title)
    except (IndexError, AttributeError) as e:
        log.warning(f"제목 교체 실패: {e}")

    # 3) 기존 본문 삭제 (BODY_START_INDEX부터 끝까지)
    body_paras = list(doc.paragraphs[BODY_START_INDEX:])
    for p in reversed(body_paras):
        p.remove()

    # 4) 새 본문 추가
    heading_count = 0

    for line in content.split("\n"):
        if not line.strip():
            continue

        is_heading = line.strip().startswith("□")
        style, formatted_text, heading_count = _detect_line_style(line, heading_count)

        # □ 대제목 앞에 빈 줄 삽입 (첫 번째 제외)
        if is_heading and heading_count > 1:
            doc.add_paragraph(
                "",
                para_pr_id_ref=STYLES["blank"]["paraPrIDRef"],
                char_pr_id_ref=STYLES["blank"]["charPrIDRef"],
            )

        doc.add_paragraph(
            formatted_text,
            para_pr_id_ref=style["paraPrIDRef"],
            char_pr_id_ref=style["charPrIDRef"],
        )

    # 5) bytes로 반환
    buf = io.BytesIO()
    doc.save_to_stream(buf)
    return buf.getvalue()


# ============================================================
# 동적 HWPX 생성 (AI 명령 JSON 기반)
# ============================================================

def _execute_set_cell(doc, action: dict):
    """표 셀 텍스트 교체"""
    table_idx = action["table"]
    row = action["row"]
    col = action["col"]
    text = action["text"]

    # 표가 포함된 문단을 찾아서 table_idx번째 표를 가져옴
    tables_found = []
    for p in doc.paragraphs:
        tables_found.extend(p.tables)

    if table_idx >= len(tables_found):
        log.warning(f"표 인덱스 {table_idx} 없음 (전체 {len(tables_found)}개)")
        return

    tables_found[table_idx].set_cell_text(row, col, text)


def _execute_clear_body(doc, action: dict):
    """지정 문단부터 끝까지 삭제"""
    from_para = action.get("from_paragraph", 0)
    body_paras = list(doc.paragraphs[from_para:])
    for p in reversed(body_paras):
        p.remove()


def _execute_add_paragraph(doc, action: dict):
    """문단 추가"""
    text = action.get("text", "")
    para_pr = action.get("paraPrIDRef", "0")
    char_pr = action.get("charPrIDRef", "0")

    doc.add_paragraph(
        text,
        para_pr_id_ref=str(para_pr),
        char_pr_id_ref=str(char_pr),
    )


def _execute_add_table(doc, action: dict):
    """표 추가"""
    rows = action.get("rows", 2)
    cols = action.get("cols", 2)
    cells = action.get("cells", [])
    border_fill = action.get("borderFillIDRef", "3")

    table = doc.add_table(
        rows=rows,
        cols=cols,
        border_fill_id_ref=str(border_fill),
    )

    # 셀 내용 채우기
    for r_idx, row_data in enumerate(cells):
        for c_idx, cell_text in enumerate(row_data):
            if r_idx < rows and c_idx < cols:
                try:
                    table.set_cell_text(r_idx, c_idx, str(cell_text))
                except Exception as e:
                    log.warning(f"셀({r_idx},{c_idx}) 설정 실패: {e}")

    return table


def _execute_remove_paragraph(doc, action: dict):
    """특정 문단 삭제"""
    idx = action.get("index", -1)
    if 0 <= idx < len(doc.paragraphs):
        doc.paragraphs[idx].remove()
    else:
        log.warning(f"문단 인덱스 {idx} 범위 초과")


# 명령 타입 → 실행 함수 매핑
ACTION_HANDLERS = {
    "set_cell": _execute_set_cell,
    "clear_body": _execute_clear_body,
    "add_paragraph": _execute_add_paragraph,
    "add_table": _execute_add_table,
    "remove_paragraph": _execute_remove_paragraph,
}


def generate_hwpx_dynamic(
    template_source,
    actions: list[dict],
) -> bytes:
    """
    AI가 생성한 명령 리스트를 기반으로 HWPX 문서를 동적 생성합니다.

    Args:
        template_source: 양식 HWPX 파일 경로(str) 또는 bytes
        actions: AI가 출력한 명령 리스트
            [
                {"type": "set_cell", "table": 0, "row": 0, "col": 1, "text": "..."},
                {"type": "clear_body", "from_paragraph": 15},
                {"type": "add_paragraph", "paraPrIDRef": "0", "charPrIDRef": "12", "text": "..."},
                {"type": "add_table", "rows": 3, "cols": 2, "cells": [["a","b"],...]},
                {"type": "remove_paragraph", "index": 20},
            ]

    Returns:
        완성된 HWPX 파일의 bytes
    """
    if isinstance(template_source, str):
        doc = HwpxDocument.open(template_source)
    elif isinstance(template_source, bytes):
        doc = HwpxDocument.open(io.BytesIO(template_source))
    else:
        doc = HwpxDocument.open(template_source)

    # 명령 순서대로 실행
    for i, action in enumerate(actions):
        action_type = action.get("type")
        handler = ACTION_HANDLERS.get(action_type)

        if handler is None:
            log.warning(f"알 수 없는 명령 타입: {action_type} (#{i})")
            continue

        try:
            handler(doc, action)
            log.debug(f"명령 #{i} 실행 완료: {action_type}")
        except Exception as e:
            log.warning(f"명령 #{i} 실행 실패: {action_type} - {e}")

    # bytes로 반환
    buf = io.BytesIO()
    doc.save_to_stream(buf)
    return buf.getvalue()
