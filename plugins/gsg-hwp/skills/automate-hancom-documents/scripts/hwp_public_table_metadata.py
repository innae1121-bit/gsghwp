from typing import Final


HWP_EXPAND_AND_FILL_TABLE_DESCRIPTION: Final = (
    "기존 HWP 표의 행을 필요한 만큼 추가하고 데이터를 입력합니다. "
    "행 추가가 필요한 요청에 사용합니다. "
    "instance_id나 후보 ID는 target: {\"target_id\": \"...\"} 형식으로 받으며 대상 표가 여러 개면 실제 후보를 반환합니다."
)
HWP_REPEAT_TABLE_TEMPLATE_DESCRIPTION: Final = (
    "기존 HWP 표 양식을 지정한 개수만큼 반복합니다. "
    "각 표의 데이터를 함께 채우는 요청은 hwp_build_table_series를 사용합니다. "
    "instance_id나 후보 ID는 target: {\"target_id\": \"...\"} 형식으로 받으며 대상 표가 여러 개면 실제 후보를 반환합니다."
)
HWP_BUILD_TABLE_SERIES_DESCRIPTION: Final = (
    "기존 HWP 표 양식을 반복하면서 각 표의 값과 그림을 함께 입력합니다. "
    "items는 values·images·text_cells·image_cells·caption을 사용하며 고유 셀은 "
    "values/images의 현재 셀 텍스트나 주소로, 중복 셀은 "
    "text_cells/image_cells에 label, occurrence, row_offset, column_offset와 "
    "value/path를 지정합니다. "
    "원본 캡션이 공백·하이픈 뒤 숫자로 끝나면 복제본마다 그 번호만 순차 증가시키고 "
    "원본 캡션 서식은 그대로 상속합니다. "
    "빈 표만 반복하면 hwp_repeat_table_template을 사용합니다. instance_id나 후보 ID는 "
    "target: {\"target_id\": \"...\"} 형식으로 받으며 대상 표가 여러 개면 실제 후보를 반환합니다."
)
HWP_SYNC_VISIBILITY_ANALYSIS_TABLES_DESCRIPTION: Final = (
    "예비조망점 선정표를 직접 읽어 가시권 분석표의 개수와 내용을 동기화합니다. "
    "조망위치·이격거리·표고·병합된 구분을 의미 머리글로 찾고, 번호별 가시권/현황 "
    "사진을 폴더에서 정확히 매칭해 기존 표를 재사용하거나 원본 한 개에서 생성합니다. "
    "원본 캡션의 끝 번호는 생성 순서에 맞춰 증가시키며 캡션 서식은 상속합니다. "
    "모델이 셀 주소, 컨트롤 ID, 레코드 목록을 직접 구성할 필요가 없습니다."
)
HWP_FILL_TABLE_IMAGES_DESCRIPTION: Final = (
    "기존 HWP 표의 지정 셀에 그림을 입력합니다. "
    "표 밖에 일반 그림을 넣는 요청은 hwp_insert_image를 사용합니다. "
    "instance_id나 후보 ID는 target: {\"target_id\": \"...\"} 형식으로 받으며 대상 표가 여러 개면 실제 후보를 반환합니다."
)
HWP_FORMAT_TABLE_DESCRIPTION: Final = (
    "기존 HWP 표의 셀에 글자·문단·배경·테두리 서식을 적용하고, "
    "row_height_mm은 그 셀이 속한 전체 행 높이, column_width_mm은 그 셀이 속한 전체 열 너비를 1~250mm로 변경합니다. "
    "불규칙 병합표에서도 F1 같은 가상 셀을 가정하지 않고 지정 셀과 교차하는 실제 물리 행·열을 선택합니다. "
    "cell을 생략하면 현재 한컴 표 셀, 연속 선택 셀 블록, 또는 선택한 표 전체를 사용합니다. 명시할 때는 셀 주소 문자열 또는 address/label·occurrence·row_offset·column_offset 선택자를 받습니다. "
    "instance_id나 후보 ID는 target: {\"target_id\": \"...\"} 형식으로 받습니다. "
    "일반 선택 글자 서식은 hwp_format_text를 사용하며 대상 표가 여러 개면 실제 후보를 반환합니다."
)
HWP_SET_TABLE_SIZE_FIXED_DESCRIPTION: Final = (
    "기존 HWP 표 개체 속성의 크기 고정을 설정합니다. fixed=true는 고정, false는 해제입니다. "
    "target.target_id에 검사한 표 instance_id를 지정하거나 현재 선택 표를 사용합니다. "
    "선택 표의 HWPML SIZE.Protect를 되읽어 설정값과 다른 표 속성·크기·내용 및 페이지 수 보존을 검증합니다. "
    "이미 요청값이면 다시 토글하지 않습니다."
)
HWP_MERGE_TABLE_CELLS_DESCRIPTION: Final = (
    "기존 HWP 표의 직사각형 셀 범위를 병합합니다. "
    "실제 셀 span과 오른쪽·아래 이웃을 검사해 빈틈 없는 직사각형일 때만 실행합니다. "
    "start_cell과 end_cell은 셀 주소 문자열 또는 address/label·occurrence·row_offset·column_offset 선택자를 받습니다. "
    "instance_id나 후보 ID는 target: {\"target_id\": \"...\"} 형식으로 받으며 대상 표가 여러 개면 실제 후보를 반환합니다."
)
HWP_SPLIT_TABLE_CELL_DESCRIPTION: Final = (
    "기존 HWP 표의 셀 하나를 지정한 칸과 줄로 나눕니다. "
    "split_mode='equal'은 병합되지 않은 셀을 균등 분할하고, 'existing_grid'는 대상 셀의 row_span·column_span과 정확히 일치하는 기존 격자선만 복원합니다. "
    "격자선 불일치, 예상 밖 행·열 수 증가, 표 전체 크기 급증은 구조 검증에서 거절합니다. "
    "cell은 셀 주소 문자열 또는 address/label·occurrence·row_offset·column_offset 선택자를 받습니다. "
    "instance_id나 후보 ID는 target: {\"target_id\": \"...\"} 형식으로 받으며 대상 표가 여러 개면 실제 후보를 반환합니다."
)
EXPAND_INTENT: Final = "표 행 확장 후 채우기"
REPEAT_INTENT: Final = "표 양식 반복"
SERIES_INTENT: Final = "표 시리즈 생성"
TABLE_IMAGES_INTENT: Final = "표 셀 그림 입력"
FORMAT_TABLE_INTENT: Final = "표 셀 서식 적용"
MERGE_TABLE_CELLS_INTENT: Final = "표 셀 병합"
SPLIT_TABLE_CELL_INTENT: Final = "표 셀 분할"
