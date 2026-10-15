from typing import Final


INSERT_IMAGE_INTENT: Final = "현재 선택 위치 또는 문서 끝에 그림 삽입"
REPLACE_IMAGE_INTENT: Final = "현재 선택 그림 교체"
EDIT_PICTURE_INTENT: Final = "그림 자르기 또는 표 셀 사이 이동"
COPY_PICTURE_INTENT: Final = "원본 내장 그림을 다른 표 셀에 무손실 복제"
RESTRUCTURE_TABLE_INTENT: Final = "사진 표를 병합 없이 새 표로 재구성"
ADD_CAPTION_INTENT: Final = "현재 선택 표 또는 그림에 캡션 추가"
APPLY_STYLE_INTENT: Final = "현재 선택 영역에 스타일 적용"
FORMAT_TEXT_INTENT: Final = "현재 선택 영역 글자 서식"
PATCH_TEXT_INTENT: Final = "본문 텍스트 원자적 패치"
PATCH_TEXT_BATCH_INTENT: Final = "본문 텍스트 원자적 일괄 패치"
APPEND_LAYOUT_INTENT: Final = "문서 끝 레이아웃 추가"
INSERT_LAYOUT_INTENT: Final = "현재 커서 또는 지정 쪽 다음 레이아웃 삽입"
INSERT_CURRENT_FORMAT_CONTENT_INTENT: Final = (
    "현재 이웃 서식으로 텍스트 그림 PPTX Excel 삽입"
)
SAVE_INTENT: Final = "문서 일반 저장"
SAVE_REOPEN_VERIFY_INTENT: Final = "문서 저장 재개방 검증"
DELETE_PAGE_INTENT: Final = "지정한 한컴 쪽 삭제"
DELETE_CONTROL_INTENT: Final = "지정한 기존 한컴 개체 삭제"
UNDO_INTENT: Final = "직전 한컴 작업 실행 취소 롤백"
REDO_INTENT: Final = "취소한 한컴 작업 다시 실행"

HWP_FILL_TABLE_DESCRIPTION: Final = (
    "기존 HWP 표에 데이터를 입력하며 행은 추가하지 않습니다. "
    "값이 머리글에 대응하면 처음부터 끝까지 records를 사용하고, "
    "cells는 검증된 셀 주소가 있을 때 사용합니다. rows는 start_cell을 명시하거나, 생략하고 현재 한컴 표 셀·연속 셀 블록·선택 표를 시작 기준으로 사용할 수 있습니다. "
    "headers는 대상 표에 실제 표시된 머리글만 받는 선택 필터이며 records 키 목록이 아닙니다. "
    "쪽으로 대상이 충분히 좁혀지면 추측한 headers 없이 page만 사용합니다. "
    "needs_target 응답의 ID는 이 도구의 직접 target_id 필드에 넣고 입력 형식은 바꾸지 않습니다. "
    "대상 표가 여러 개면 실제 후보를 반환합니다."
)
HWP_INSERT_IMAGE_DESCRIPTION: Final = (
    "현재 선택 위치 또는 문서 끝에 그림 파일을 1mm 이상 지정 크기로 삽입합니다. "
    "fit=contain(기본)은 width_mm×height_mm 안에 원본 비율 그대로 넣어 한쪽에 여백을 남기고, "
    "fit=cover는 그 영역을 꽉 채운 뒤 넘치는 가장자리를 한/글 그림 자르기로 감춥니다. "
    "cover에는 width_mm와 height_mm가 필요하며, 어느 쪽이든 원본 그림 파일은 읽기만 하고 "
    "잘린 사본을 따로 만들지 않습니다. "
    "기존 그림을 바꾸는 요청은 hwp_replace_image를 사용합니다."
)
HWP_REPLACE_IMAGE_DESCRIPTION: Final = (
    "현재 선택했거나 hwp_inspect_page_fast가 반환한 instance_id 또는 앞선 후보 target_id로 그림을 교체합니다. "
    "대상 그림이 여러 개면 실제 후보를 반환합니다."
)
HWP_EDIT_PICTURE_DESCRIPTION: Final = (
    "문서에 이미 있는 그림 한 장을 자르거나 다른 표 칸으로 옮깁니다. "
    "crop은 네 변에서 감출 몫을 0.0~1.0 비율로 받아 조망점·사업구역처럼 원하는 "
    "부분만 남기며, 그림 상자 크기는 그대로라 남은 부분이 그 상자를 채워 확대한 "
    "것처럼 보입니다. crop은 절대값이라 준 대로가 결과가 되고 생략한 변은 0입니다. "
    "move_to_cell은 그림을 같은 표 또는 move_to_table_id가 가리키는 표의 다른 칸으로 "
    "옮깁니다. 한 번의 호출은 그림 한 장만 다루므로 여러 장을 재배치할 때는 한 장씩 "
    "반복해서 부르고, 옮긴 그림은 개체 ID가 바뀌므로 응답의 picture.after.picture_id를 "
    "다음 호출에 쓰세요. "
    "대상은 hwp_inspect_page_fast(include_cells=true)가 주는 instance_id를 target_id로 "
    "주거나, 같은 응답의 parent_table_instance_id와 parent_cell_address를 table_id·cell로 "
    "줍니다. 응답의 picture에는 원본 크기와 실제로 적용된 자르기가 함께 들어 있습니다."
)
HWP_RESTRUCTURE_TABLE_DESCRIPTION: Final = (
    "사진과 사진 라벨로만 구성된 표의 열 수 변경·재배치 전용입니다. "
    "이미 나뉜 제목 칸 입력은 hwp_fill_table을 사용합니다. "
    "매핑에서 누락된 본문이 있으면 해당 셀을 반환합니다. 원본 표 바로 옆에 최종 표를 "
    "만들고 사진을 한/글 자체 Copy/Paste로 직접 무손실 복사하므로 원본 내장 바이트를 "
    "재인코딩하지 않습니다. 사진 수·개체 ID·크기와 라벨·캡션을 전량 검증한 뒤에만 "
    "원본 표를 삭제하며, 문서 끝에 임시 표를 만들지 않습니다. 기본 사진 격자는 "
    "병합하지 않고 요청한 최종 토폴로지 병합만 새 표 생성 시 선언합니다."
)
HWP_COPY_PICTURE_DESCRIPTION: Final = (
    "문서에 이미 들어 있는 그림 한 장의 ORIGINAL 내장 이미지 바이트를 한/글 자체 "
    "Copy/Paste로 품질 손실 없이 다른 표 칸에 복제하며 원본 그림은 그대로 둡니다. "
    "표를 다시 만들 때는 기존 표를 삭제하기 전에 이 도구로 원본 그림들을 새 표에 "
    "먼저 복사해야 합니다. width_mm와 height_mm를 함께 주면 새 그림의 프레임 크기를 "
    "mm로 정하며 이미지 바이트는 재인코딩하지 않습니다. 대상은 target_id 또는 "
    "table_id와 cell로 지목하고, 목적지는 to_cell과 필요하면 to_table_id로 지목합니다."
)
HWP_ADD_CAPTION_DESCRIPTION: Final = "표·그림 개체에 부착하는 캡션을 명시적으로 요청했을 때만 사용합니다. 본문 문단으로 된 표·그림 제목은 올바른 같은 종류 제목을 원본으로 hwp_apply_style의 source_position과 style_copy_type=2로 글자·문단 모양을 복사하고 제목 문구만 바꾸세요. 번호를 직접 입력하지 마세요. 별도 제목과 개체 부착 캡션이 중복되면 숨은 번호 때문에 순서가 건너뛸 수 있으므로 구조와 앞뒤 번호를 확인하세요. 현재 선택 또는 실제 instance_id/target_id의 부착 캡션을 추가·갱신하며 기존 자동 번호 필드를 보존합니다."
HWP_APPLY_STYLE_DESCRIPTION: Final = (
    "문서 style_id를 적용하거나 source_position의 실제 모양을 target_position에 복사합니다. "
    "style_id와 source_position은 함께 쓰지 않습니다. style_copy_type은 0 글자, 1 문단, "
    "2 글자와 문단(기본값), 3 글자 스타일, 4 문단 스타일입니다. "
    "개별 글자·문단 속성을 지정하는 요청은 hwp_format_text를 사용합니다."
)
HWP_FORMAT_TEXT_DESCRIPTION: Final = (
    "현재 선택 영역에 지정한 글자·문단 서식을 적용합니다. "
    "letter_spacing_percent는 자간의 절대 백분율(-50~50)이며, 생략하면 유지합니다. "
    "본문과 표 안의 일부 글자는 target range와 expected_text로 좁혀 조정합니다. "
    "단어 끊김 보정은 필요한 줄의 구간만 소폭 조정한 뒤 hwp_render_page로 앞뒤 줄을 확인하세요. "
    "표 셀 전체의 기타 서식은 hwp_format_table을 사용합니다."
)
HWP_PATCH_TEXT_DESCRIPTION: Final = (
    "선택 여부와 무관하게 현재 커서·현재 선택·명시 범위·문서 검색 결과·정확한 표 셀의 텍스트를 "
    "한 작업으로 확인하고 교체합니다. 교체된 실제 범위를 다시 선택한 뒤 formatting을 적용하고 "
    "본문과 글자 서식을 재검증합니다. post_selection은 검증 뒤 선택을 유지하거나 시작·끝으로 접으며, "
    "collapse_to_end는 곧바로 hwp_insert_layout target=current를 잇게 합니다. 검색 결과가 여러 개이면 "
    "occurrence를 임의로 정하지 않고 구체적인 위치 후보를 반환합니다."
)
HWP_PATCH_TEXT_FROM_XLSX_DESCRIPTION: Final = (
    "완전한 compact patch projection과 bounded XLSX 질의를 서버 안에서 결합해 "
    "workbook 문자열을 모델에 노출하지 않고 원자적 text patch batch로 소비합니다. "
    "projection의 문서 identity와 content revision을 필수 guard로 사용합니다."
)
HWP_PATCH_TEXT_BATCH_DESCRIPTION: Final = (
    "서로 떨어진 본문·표 셀 교체를 patches 순서대로 한 번의 네이티브 작업으로 실행합니다. "
    "정확한 prepared batch는 기존 본문 범위와 기존 표 셀의 텍스트만 교체하며 "
    "표·행·컨트롤·레이아웃을 만들거나 삭제하지 않습니다. 논리 이력을 사용하며 전체 문서 "
    "체크포인트는 0회입니다. 선택적 최상위 formatting은 비어 있지 않은 모든 replacement에 "
    "공통 적용되며 각 교체·서식을 다음 패치 전에 즉시 되읽어 검증합니다. 항목별 formatting은 "
    "받지 않습니다. expected_text는 문서 구조의 리터럴 텍스트이며 화면에 보이는 자동 "
    "번호·글머리표(예: 가), ◦)는 제외합니다. 실제 리터럴 문자는 자동 제거하지 않습니다."
    " format_changed_only=true는 같은 문단의 expected_text/replacement를 공백으로 구분된 "
    "토큰별 RANGE로 좁혀 바뀐 단어·숫자만 공통 서식으로 적용합니다. 바뀐 표 셀은 셀 전체에 "
    "적용하고 같은 값의 항목은 생략합니다. 이 모드는 formatting과 모든 expected_text가 필요하며 "
    "정확한 같은 문단 RANGE 또는 table_cell 대상만 받습니다."
)
HWP_REPLACE_SELECTED_TEXT_DESCRIPTION: Final = (
    "현재 사용자가 드래그한 선택 영역의 전체 텍스트를 지정한 문자열로 교체합니다. "
    "replacement에 빈 문자열을 전달하면 선택한 글을 삭제합니다. "
    "선택 좌표와 원문은 도구 내부에서 즉시 읽고 C++/ATL이 실행 직전에 다시 검증합니다."
)
# 실기에서 네 번 연속 여기서 막혔다. 도구 설명은 어떤 블록을 언제 쓰는지만
# 말하고 인자 모양은 한 번도 보여주지 않았으며, layout 은 $ref 라서 목록에
# 아무 설명도 붙지 않았다. kind 가 필수라는 사실이 실패한 뒤 오류 문구에만
# 나오니 첫 호출이 계속 죽는다. 최소 형태를 설명 맨 앞에 둔다.
LAYOUT_MINIMUM_SHAPE: Final = (
    '최소 형태: layout={"blocks": [{"kind": "paragraph", "text": "문단 내용"}]}. '
    "blocks의 모든 원소에 kind가 필수이며 kind가 그 블록의 형식을 정합니다: "
    "paragraph, table, image, page_break, reference_layout, reference_layout_patch. "
)
HWP_INSERT_LAYOUT_DESCRIPTION: Final = LAYOUT_MINIMUM_SHAPE + (
    "현재 열린 HWP의 본문 중간에 편집 가능한 문단·표·개별 그림을 삽입합니다. "
    "layout.target=current는 사용자가 둔 현재 커서에 삽입하고, "
    "target=after_page와 page=N은 N쪽 끝에서 쪽 나누기를 실행해 N쪽 다음 새 쪽에 삽입합니다. "
    "target=document_end도 지원하며 after_page는 내용을 앞뒤 쪽 나누기로 격리하므로 별도 page_break 블록을 넣지 않습니다. "
    "reference_layout은 편집 가능한 격자를 만들고, reference_layout_patch는 기존 control ID의 "
    "행·열·스타일·테두리만 갱신하며 새 표를 만들지 않습니다."
)
HWP_APPEND_LAYOUT_DESCRIPTION: Final = LAYOUT_MINIMUM_SHAPE + (
    "현재 열린 HWP의 문서 끝에 편집 가능한 문단·표·개별 그림 레이아웃을 추가합니다. "
    "문서의 기존 본문·제목·표타이틀·표내용·그림타이틀 스타일과 실제 본문 폭을 자동으로 따릅니다. "
    "본문 중간이나 특정 쪽 다음 삽입은 hwp_insert_layout을 사용합니다."
)
HWP_APPEND_REPORT_DESCRIPTION: Final = (
    "텍스트·항목·레코드·그림을 편집 가능한 한컴 문단·표·표 안 그림으로 재구성해 보고서 끝에 추가합니다. "
    "열별 최소 가독 폭과 행 높이를 계산하고 넓은 표는 식별 열을 반복해 의미 단위로 나눕니다. "
    "figures는 기본적으로 테두리가 있는 1×1 표 셀 안에 배치하며 기존 문서 스타일과 본문 폭을 따릅니다."
)
HWP_APPEND_EXCEL_TABLE_DESCRIPTION: Final = (
    "Excel 범위의 바깥쪽 빈 행·열만 제거하고 원본 병합·채움·테두리·행 높이·열 비율을 존중해 "
    "편집 가능한 한컴 표로 정리합니다. 열별 최소 가독 폭을 확보할 수 없는 병합 표는 "
    "억지로 압축하지 않고 의미 단위 범위 분할이 필요하다고 반환합니다."
)
HWP_INSERT_CURRENT_FORMAT_CONTENT_DESCRIPTION: Final = (
    "현재 커서 또는 지정 쪽 다음에 sources 순서 그대로 편집 가능한 문단·표·개별 그림을 삽입합니다. "
    "pptx_slide는 선택한 표시 슬라이드의 text shape/run을 HWP ParagraphBlock/run으로, "
    "PPTX pic 관계의 원본 그림만 ImageBlock으로, PPTX 표를 실제 TableBlock/cell/merge로 재구성하며 "
    "슬라이드 전체를 렌더링한 미리보기나 평면 그림을 만들지 않습니다. pptx_table은 기존 PPTX 표 전용 경로입니다. "
    "Excel legacy .xls/.xlsx 범위도 원본 셀 텍스트·병합·채움·테두리·행 높이를 편집 가능한 HWP 표로 옮깁니다. "
    "쓰기 직전 열린 HWP의 현재 위치에서 CharacterStyle(height_hwpunit,bold,text_color)과 "
    "ParagraphStyle(align_type,line_spacing,문단 좌우 여백,들여쓰기,문단 앞뒤 간격), PageSetup을 실시간으로 읽고 "
    "등록 style_id 없이 관측 raw 값으로 문단/표 셀을 구성합니다. 응답은 semantic source counts, 전후 스타일, "
    "PageSetup 불변 여부, fit 치수, 삽입 순서, 미관측 columns와 자동 번호 numeric 값 미주장을 구조화해 반환합니다."
)
HWP_SAVE_REOPEN_VERIFY_DESCRIPTION: Final = (
    "현재 문서를 저장하고 같은 경로로 다시 열어 구조를 검증합니다. "
    "명시적인 저장·재개방 검증 요청에만 사용합니다."
)
HWP_SAVE_DESCRIPTION: Final = (
    "현재 문서를 닫거나 다시 열지 않고 저장합니다. "
    "저장 전후 본문·표 내용·문자 서식을 포함한 문서 지문을 다시 읽어 성공 여부를 검증합니다."
)
HWP_DELETE_PAGE_DESCRIPTION: Final = (
    "현재 열린 HWP에서 지정한 실제 쪽 하나를 삭제하고 페이지 수가 하나 줄었는지 검증합니다. "
    "빈 페이지 제거·쪽 삭제 요청에 바로 사용합니다."
)
HWP_DELETE_CONTROL_DESCRIPTION: Final = (
    "hwp_inspect_page_fast가 반환한 ID로 기존 표·그림·도형·텍스트 상자 개체를 하나 이상 삭제하고 "
    "빠른 구조에서 제거를 검증합니다. 잘못 만든 독립 표들을 지운 뒤 한 표로 재구성할 때 사용합니다."
)
HWP_UNDO_DESCRIPTION: Final = "현재 열린 HWP의 직전 편집을 1~20단계 실행 취소합니다. 되돌리기·롤백·방금 작업 취소 요청에 바로 사용합니다."
HWP_REDO_DESCRIPTION: Final = "현재 열린 HWP에서 취소한 편집을 1~20단계 다시 실행합니다. 재실행·되살리기 요청에 바로 사용합니다."
