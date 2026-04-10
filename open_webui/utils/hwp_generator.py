"""
HWPX 문서 생성 모듈

도청동향보고서 템플릿을 기반으로 HWPX 문서를 생성합니다.
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
