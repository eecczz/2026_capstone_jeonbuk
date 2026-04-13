"""
HWPX 문서 생성 모듈 — AI 명령 JSON 기반 동적 생성

python-hwpx 라이브러리를 사용하여 양식 기반으로 문서를 생성합니다.
"""

import io
import logging
from dataclasses import dataclass, field

from hwpx import HwpxDocument
from open_webui.env import GLOBAL_LOG_LEVEL

log = logging.getLogger(__name__)
log.setLevel(GLOBAL_LOG_LEVEL)


@dataclass
class HwpxResult:
    """HWPX 생성 결과"""
    data: bytes
    success_count: int = 0
    fail_count: int = 0
    errors: list[str] = field(default_factory=list)

def _execute_set_cell(doc, action: dict):
    """표 셀 텍스트 교체 (행/열 부족 시 자동 확장)"""
    from copy import deepcopy

    table_idx = action["table"]
    row = action["row"]
    col = action["col"]
    text = action["text"]

    tables_found = []
    for p in doc.paragraphs:
        tables_found.extend(p.tables)

    if table_idx >= len(tables_found):
        raise IndexError(f"표 인덱스 {table_idx} 없음 (전체 {len(tables_found)}개)")

    tbl = tables_found[table_idx]
    NS = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"

    # 행 부족 시 자동 확장
    current_rows = int(tbl.element.get("rowCnt", "1"))
    if row >= current_rows:
        tr_elements = tbl.element.findall(f"{NS}tr")
        if not tr_elements:
            raise ValueError(f"표 {table_idx}에 행이 없음")
        last_row_elem = tr_elements[-1]
        for i in range(current_rows, row + 1):
            new_row_elem = deepcopy(last_row_elem)
            for tc in new_row_elem.iter(f"{NS}tc"):
                for addr in tc.iter(f"{NS}cellAddr"):
                    addr.set("rowAddr", str(i))
                for span in tc.iter(f"{NS}cellSpan"):
                    span.set("rowSpan", "1")
                    span.set("colSpan", "1")
                for t_elem in tc.iter(f"{NS}t"):
                    t_elem.text = ""
            tbl.element.append(new_row_elem)
        tbl.element.set("rowCnt", str(row + 1))
        log.info(f"표 {table_idx} 행 확장: {current_rows} → {row + 1}")

    # 열 부족 시 자동 확장
    current_cols = int(tbl.element.get("colCnt", "1"))
    if col >= current_cols:
        _adjust_table_columns(tbl.element, col + 1)

    try:
        tbl.set_cell_text(row, col, text)
    except TypeError:
        # lxml/stdlib SubElement 호환 문제 우회: 직접 XML 레벨로 텍스트 교체
        tr_elements = tbl.element.findall(f"{NS}tr")
        if row < len(tr_elements):
            tcs = tr_elements[row].findall(f"{NS}tc")
            if col < len(tcs):
                for t_elem in tcs[col].iter(f"{NS}t"):
                    t_elem.text = str(text)
                    break
            else:
                raise IndexError(f"열 {col} 범위 초과 (표 {table_idx})")
        else:
            raise IndexError(f"행 {row} 범위 초과 (표 {table_idx})")


def _execute_clear_body(doc, action: dict):
    """지정 문단부터 끝까지 삭제"""
    from_para = action.get("from_paragraph", 0)
    body_paras = list(doc.paragraphs[from_para:])
    for p in reversed(body_paras):
        p.remove()


def _execute_set_paragraph_text(doc, action: dict):
    """기존 문단의 텍스트만 교체 (서식 완전 보존)"""
    idx = action.get("index")
    text = action.get("text", "")

    if idx is None:
        raise ValueError("set_paragraph_text에 index 필수")
    if idx < 0 or idx >= len(doc.paragraphs):
        raise IndexError(
            f"문단 인덱스 {idx} 범위 초과 (전체 {len(doc.paragraphs)}개)"
        )

    doc.paragraphs[idx].text = text


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
        raise IndexError(f"문단 인덱스 {idx} 범위 초과 (전체 {len(doc.paragraphs)}개)")


def _adjust_table_columns(tbl_elem, target_cols: int):
    """
    테이블의 컬럼 수를 target_cols에 맞게 동적 조절합니다.
    - target_cols > 현재 → 마지막 셀을 복제하여 열 추가 (모든 기존 행)
    - target_cols < 현재 → 초과 셀을 제거 (모든 기존 행)
    """
    from copy import deepcopy

    NS = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
    current_cols = int(tbl_elem.get("colCnt", "1"))

    if current_cols == target_cols:
        return

    for tr in tbl_elem.findall(f"{NS}tr"):
        tcs = tr.findall(f"{NS}tc")
        if not tcs:
            continue

        if target_cols > current_cols:
            last_tc = tcs[-1]
            for i in range(target_cols - current_cols):
                new_tc = deepcopy(last_tc)
                new_col = current_cols + i
                for addr in new_tc.iter(f"{NS}cellAddr"):
                    addr.set("colAddr", str(new_col))
                for span in new_tc.iter(f"{NS}cellSpan"):
                    span.set("colSpan", "1")
                    span.set("rowSpan", "1")
                for t_elem in new_tc.iter(f"{NS}t"):
                    t_elem.text = ""
                tr.append(new_tc)
        else:
            for tc in reversed(tcs[target_cols:]):
                tr.remove(tc)

    tbl_elem.set("colCnt", str(target_cols))
    log.info(f"테이블 컬럼 조절: {current_cols} → {target_cols}")


def _execute_add_row(doc, action: dict):
    """기존 표에 행 추가 (마지막 행을 복제하여 추가, 컬럼 수 자동 조절)"""
    from copy import deepcopy

    table_idx = action["table"]
    count = action.get("count", 1)
    cells = action.get("cells", None)  # [["셀1","셀2",...]] 행별 셀 내용

    tables_found = []
    for p in doc.paragraphs:
        tables_found.extend(p.tables)

    if table_idx >= len(tables_found):
        raise IndexError(f"표 인덱스 {table_idx} 없음 (전체 {len(tables_found)}개)")

    tbl = tables_found[table_idx]
    NS = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"

    if cells:
        # 데이터 컬럼 수와 테이블 컬럼 수가 다르면 자동 조절
        data_cols = max(len(row) for row in cells)
        current_cols = int(tbl.element.get("colCnt", "1"))
        if data_cols != current_cols:
            _adjust_table_columns(tbl.element, data_cols)
        # cells 행 수가 count보다 많으면 cells 기준으로
        if len(cells) > count:
            count = len(cells)

    # 컬럼 조절 이후의 마지막 행을 복제 대상으로 사용
    tr_elements = tbl.element.findall(f"{NS}tr")
    if not tr_elements:
        raise ValueError(f"표 {table_idx}에 행이 없음")
    last_row_elem = tr_elements[-1]

    for r in range(count):
        new_row_elem = deepcopy(last_row_elem)

        # 새 행의 rowAddr 업데이트 + 셀 병합 초기화
        cur_row_cnt = int(tbl.element.get("rowCnt", "0"))
        for tc in new_row_elem.iter(f"{NS}tc"):
            # rowAddr 갱신
            for addr in tc.iter(f"{NS}cellAddr"):
                addr.set("rowAddr", str(cur_row_cnt))
            # 셀 병합 초기화 (rowSpan=1, colSpan=1)
            for span in tc.iter(f"{NS}cellSpan"):
                span.set("rowSpan", "1")
                span.set("colSpan", "1")

        tbl.element.append(new_row_elem)

        # rowCnt 업데이트
        tbl.element.set("rowCnt", str(cur_row_cnt + 1))

        # 셀 내용 직접 설정 (XML 레벨)
        if cells and r < len(cells):
            tcs = list(new_row_elem.iter(f"{NS}tc"))
            for c_idx, cell_text in enumerate(cells[r]):
                if c_idx < len(tcs):
                    # 첫 번째 <hp:t> 요소의 텍스트를 교체
                    for t_elem in tcs[c_idx].iter(f"{NS}t"):
                        t_elem.text = str(cell_text)
                        break


def _execute_remove_table(doc, action: dict):
    """표 삭제 (표를 포함하는 문단을 삭제)"""
    table_idx = action["table"]

    tables_found = []
    table_para_map = {}  # table → paragraph index
    for i, p in enumerate(doc.paragraphs):
        for t in p.tables:
            table_para_map[len(tables_found)] = i
            tables_found.append(t)

    if table_idx >= len(tables_found):
        raise IndexError(f"표 인덱스 {table_idx} 없음 (전체 {len(tables_found)}개)")

    para_idx = table_para_map[table_idx]
    doc.paragraphs[para_idx].remove()


def _execute_insert_paragraph(doc, action: dict):
    """특정 위치에 문단 삽입 (index 번째 문단 앞에 삽입)"""
    from copy import deepcopy

    idx = action.get("index", 0)
    text = action.get("text", "")
    para_pr = str(action.get("paraPrIDRef", "0"))
    char_pr = str(action.get("charPrIDRef", "0"))

    if idx < 0 or idx >= len(doc.paragraphs):
        raise IndexError(f"삽입 위치 {idx} 범위 초과 (전체 {len(doc.paragraphs)}개)")

    target = doc.paragraphs[idx]
    section_elem = target.element.getparent()
    target_pos = list(section_elem).index(target.element)

    # 대상 문단 복제 후 내용/스타일 교체
    new_elem = deepcopy(target.element)
    new_elem.set("paraPrIDRef", para_pr)

    # 텍스트 설정
    NS = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
    for run in new_elem.iter(f"{NS}run"):
        run.set("charPrIDRef", char_pr)
        for t in run.iter(f"{NS}t"):
            t.text = text
        break  # 첫 번째 run만 사용
    else:
        # run이 없으면 생성하지 않고 add_paragraph 방식 사용
        section_elem.insert(target_pos, new_elem)
        return

    section_elem.insert(target_pos, new_elem)


def _execute_clone_paragraph(doc, action: dict):
    """기존 문단을 복제하여 바로 뒤에 삽입 (표/텍스트 상자 포함 전체 구조 보존).
    복제 후 텍스트를 교체합니다."""
    from copy import deepcopy

    source_idx = action.get("source")
    text = action.get("text", "")

    if source_idx is None:
        raise ValueError("clone_paragraph에 source 필수")
    if source_idx < 0 or source_idx >= len(doc.paragraphs):
        raise IndexError(
            f"복제 원본 인덱스 {source_idx} 범위 초과 (전체 {len(doc.paragraphs)}개)"
        )

    source = doc.paragraphs[source_idx]
    section_elem = source.element.getparent()
    source_pos = list(section_elem).index(source.element)

    # 문단 전체 복제 (표, 텍스트 상자, 서식 모두 포함)
    new_elem = deepcopy(source.element)

    # 텍스트 교체
    NS = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
    if text:
        # 표가 있는 문단이면 표 안의 첫 번째 셀 텍스트 교체
        tbl = new_elem.find(f".//{NS}tbl")
        if tbl is not None:
            for t_elem in tbl.iter(f"{NS}t"):
                if t_elem.text is not None or t_elem.getparent().tag.endswith("}run"):
                    t_elem.text = text
                    break
        else:
            # 일반 문단이면 첫 번째 run의 텍스트 교체
            for t_elem in new_elem.iter(f"{NS}t"):
                t_elem.text = text
                break

    # 원본 바로 뒤에 삽입
    section_elem.insert(source_pos + 1, new_elem)
    log.info(f"문단 {source_idx} 복제 완료 (텍스트: {text[:50]}...)")


# 명령 타입 → 실행 함수 매핑
ACTION_HANDLERS = {
    "set_cell": _execute_set_cell,
    "set_paragraph_text": _execute_set_paragraph_text,
    "clear_body": _execute_clear_body,
    "add_paragraph": _execute_add_paragraph,
    "add_table": _execute_add_table,
    "remove_paragraph": _execute_remove_paragraph,
    "add_row": _execute_add_row,
    "remove_table": _execute_remove_table,
    "clone_paragraph": _execute_clone_paragraph,
    "insert_paragraph": _execute_insert_paragraph,
}

# 비구조적 액션 (문단 인덱스 변경 없음 → 먼저 실행)
_NONSTRUCTURAL = {"set_cell", "set_paragraph_text", "add_row"}
# 구조 삭제 (높은 인덱스부터 → 시프트 방지)
_STRUCTURAL_REMOVE = {"remove_paragraph", "remove_table"}
# 구조 삽입 (높은 인덱스부터 → 시프트 방지)
_STRUCTURAL_INSERT = {"insert_paragraph", "clone_paragraph"}
# 범위 삭제
_STRUCTURAL_CLEAR = {"clear_body"}
# 문서 끝 추가 (마지막에 실행)
_APPEND = {"add_paragraph", "add_table"}


def _get_action_index(action: dict) -> int:
    """액션에서 정렬용 인덱스 추출"""
    return action.get("index", action.get("table", action.get("from_paragraph", 0)))


def _sort_actions(actions: list[dict]) -> list[tuple[int, dict]]:
    """
    액션을 안전한 실행 순서로 정렬합니다.
    인덱스 시프트를 방지하기 위해:

    Phase 1: 비구조적 (set_cell, set_paragraph_text, add_row)
             → 인덱스 변경 없으므로 원래 순서 유지
    Phase 2: 삭제 (remove_paragraph, remove_table)
             → 높은 인덱스부터 처리하여 시프트 방지
    Phase 3: 삽입 (insert_paragraph)
             → 높은 인덱스부터 처리하여 시프트 방지
    Phase 4: 범위 삭제 (clear_body)
    Phase 5: 추가 (add_paragraph, add_table)
             → 문서 끝에 붙으므로 원래 순서 유지

    Returns:
        [(원래_번호, action)] 리스트
    """
    p1, p2, p3, p4, p5 = [], [], [], [], []

    for i, a in enumerate(actions):
        t = a.get("type")
        if t in _NONSTRUCTURAL:
            p1.append((i, a))
        elif t in _STRUCTURAL_REMOVE:
            p2.append((i, a))
        elif t in _STRUCTURAL_INSERT:
            p3.append((i, a))
        elif t in _STRUCTURAL_CLEAR:
            p4.append((i, a))
        elif t in _APPEND:
            p5.append((i, a))
        else:
            p1.append((i, a))  # 알 수 없는 타입 → 비구조적 취급

    p2.sort(key=lambda x: _get_action_index(x[1]), reverse=True)
    p3.sort(key=lambda x: _get_action_index(x[1]), reverse=True)

    return p1 + p2 + p3 + p4 + p5


def _clear_unmodified_fields(doc, structure, modified_paragraphs, modified_cells):
    """구조 분석 결과를 기반으로 미수정 editable 필드를 비웁니다."""
    cleared = 0

    # 문단 클리어
    for p_info in structure.get("paragraphs", []):
        idx = p_info.get("idx")
        desc = p_info.get("description", "")

        if "고정 텍스트" in desc or "수정 불필요" in desc:
            continue

        if idx is not None and idx not in modified_paragraphs:
            if 0 <= idx < len(doc.paragraphs):
                try:
                    doc.paragraphs[idx].text = ""
                    cleared += 1
                except Exception:
                    pass

    # 표 셀 클리어
    tables_found = []
    for p in doc.paragraphs:
        tables_found.extend(p.tables)

    for t_info in structure.get("tables", []):
        table_idx = t_info.get("table")
        for vc in t_info.get("value_cells", []):
            r, c = vc.get("row"), vc.get("col")
            if (table_idx, r, c) not in modified_cells:
                if table_idx is not None and table_idx < len(tables_found):
                    try:
                        tables_found[table_idx].set_cell_text(r, c, "")
                        cleared += 1
                    except Exception:
                        pass

    if cleared > 0:
        log.info(f"미수정 필드 {cleared}개 클리어")


def generate_hwpx_dynamic(
    template_source,
    actions: list[dict],
    structure: dict = None,
    removed_indices: list[int] = None,
) -> HwpxResult:
    """
    AI가 생성한 명령 리스트를 기반으로 HWPX 문서를 동적 생성합니다.

    Args:
        template_source: 양식 HWPX 파일 경로(str) 또는 bytes
        actions: AI가 출력한 명령 리스트
        structure: 1차 구조 분석 결과 (미수정 필드 자동 클리어용)
        removed_indices: truncate_xml()이 제거한 원본 _idx 목록.
            전달 시 실제 문서에서도 해당 문단을 제거하여
            LLM이 본 축소 XML과 동일한 구조로 맞춤.

    Returns:
        HwpxResult(data=bytes, success_count, fail_count, errors)
    """
    if isinstance(template_source, str):
        doc = HwpxDocument.open(template_source)
    elif isinstance(template_source, bytes):
        doc = HwpxDocument.open(io.BytesIO(template_source))
    else:
        doc = HwpxDocument.open(template_source)

    # 문서를 LLM이 본 축소 구조와 동일하게 맞춤
    if removed_indices:
        before_count = len(doc.paragraphs)
        for idx in sorted(removed_indices, reverse=True):
            if 0 <= idx < len(doc.paragraphs):
                doc.paragraphs[idx].remove()
        log.info(
            f"문서 축소: {before_count}개 → {len(doc.paragraphs)}개 문단 "
            f"({len(removed_indices)}개 제거)"
        )

    sorted_actions = _sort_actions(actions)
    success_count = 0
    fail_count = 0
    errors = []
    modified_paragraphs = set()
    modified_cells = set()

    for orig_idx, action in sorted_actions:
        action_type = action.get("type")
        handler = ACTION_HANDLERS.get(action_type)

        if handler is None:
            log.warning(f"알 수 없는 명령 타입: {action_type} (#{orig_idx})")
            errors.append(f"#{orig_idx} {action_type}: 알 수 없는 명령")
            fail_count += 1
            continue

        try:
            handler(doc, action)
            log.info(f"명령 #{orig_idx} 실행 완료: {action_type}")
            success_count += 1
            if action_type == "set_paragraph_text":
                modified_paragraphs.add(action.get("index"))
            elif action_type == "set_cell":
                modified_cells.add((action["table"], action["row"], action["col"]))
        except Exception as e:
            log.warning(f"명령 #{orig_idx} 실행 실패: {action_type} - {e}")
            errors.append(f"#{orig_idx} {action_type}: {e}")
            fail_count += 1

    log.info(
        f"명령 실행 결과: 성공 {success_count}/{len(actions)}, 실패 {fail_count}"
        + (f" [{'; '.join(errors)}]" if errors else "")
    )

    if success_count == 0 and actions:
        raise RuntimeError(
            f"모든 명령 실행 실패 ({fail_count}개): {'; '.join(errors)}"
        )

    # 미수정 editable 필드 자동 클리어
    # clear_body가 사용된 경우 문서 구조가 완전히 바뀌었으므로 클리어 건너뜀
    has_clear_body = any(a.get("type") == "clear_body" for a in actions)
    if structure and not has_clear_body:
        _clear_unmodified_fields(doc, structure, modified_paragraphs, modified_cells)

    return HwpxResult(
        data=doc.to_bytes(),
        success_count=success_count,
        fail_count=fail_count,
        errors=errors,
    )
