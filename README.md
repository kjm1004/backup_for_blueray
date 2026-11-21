# BD-Planner 사용법 (Windows, Python 3.9+, 추가 설치 없음)

```
python bd_planner.py new 사진2차 "H:\사진 2차 백업"          # 원본 폴더는 직접 지정 (--capacity-gb 90, --binary 로 GiB)
python bd_planner.py subfolders 사진2차                       # 하부 폴더 목록 확인
python bd_planner.py select 사진2차 폴더A 폴더B 폴더C         # 백업 대상, 적은 순서가 배정 순서
python bd_planner.py scan 사진2차                             # 스캔(읽기 전용) + 90G 배정 + 선경고 + 예상 디스크 수
python bd_planner.py show 사진2차                             # disk01.. 배정 결과, 분할 폴더 표시
python bd_planner.py capacity 사진2차 1=95 2=23.3 3=45        # 디스크별 용량 지정(기본 용량과 다르게). --binary 로 GiB
python bd_planner.py capacity 사진2차 2=default               # 한 디스크만 기본 용량으로 / --clear 는 전체 해제
python bd_planner.py allocate 사진2차                         # 재스캔 없이 현재 용량 설정으로 다시 배정
python bd_planner.py export 사진2차 --stage "H:\_BD_STAGE" --disks 1   # 하드링크 스테이징 + INDEX.txt + CSV
python bd_planner.py status 사진2차 1 완료                    # 계획됨/굽는 중/완료/검증됨
python bd_planner.py verify 사진2차 1 "F:\"                   # 굽기 후 경로·크기 대조(해시 없음)
```
ImgBurn: Build 모드 → 소스에 `H:\_BD_STAGE\disk01` 폴더 추가(폴더 내용이 디스크 루트가 되도록
"Add Folder"가 아닌 폴더 내용 추가 사용) → 파일시스템 UDF → 볼륨 라벨은 INDEX.txt 첫 줄 참고 → 굽기.
스테이징은 원본과 같은 NTFS 드라이브여야 합니다. 굽기 후 스테이징 폴더를 지워도 원본은 안전합니다.

## GUI
```
python bd_planner_gui.py
```
(Windows 정식 Python에는 tkinter 포함. 같은 폴더의 bd_planner.py, DB는 bd_planner.db 사용 — CLI와 공유)
순서: ① 원본 폴더 [찾아보기] + 계획 이름 + 용량 → [폴더 목록 읽기] → ② ☑ 선택·▲▼ 순서 → ③ [스캔 + 배정 계산]
→ ④ 디스크 선택 후 상태 변경/검증 → ⑤ 스테이징 위치 지정 후 [선택 디스크 내보내기] → ImgBurn.

## 디스크별 용량 지정
디스크마다 다른 크기를 쓸 수 있습니다(예: disk01=BDXL 95GB, disk02=25GB 디스크 23.3GB 등).
- 지정하지 않은 디스크는 계획의 **기본 용량**을 씁니다. disk01부터 순서대로 채우므로 앞 디스크의 구성은 뒤쪽 용량을 바꿔도 그대로입니다.
- GUI: ① 설정의 **디스크별 용량** 칸에 `95, 23.3, , 45` 처럼 입력(빈 칸=기본값) 후 스캔, 또는 ④ 결과에서 디스크를 선택하고
  **선택 디스크 용량 → 적용 후 재배정**(스캔 없이 즉시). 용량 열의 `*` 는 개별 지정 표시.
- 이미 구운 디스크(`완료`/`검증됨`)는 재배정해도 구성이 같으면 상태를 유지하고, 구성이 바뀌면 `계획됨`으로 초기화하며 경고를 표시합니다.
- 어느 디스크에도 들어가지 않는 큰 파일은 배정에서 제외(경고), 용량이 너무 작아 비게 된 디스크는 경고합니다.
- INDEX.txt 에 해당 디스크의 용량 기준이 함께 기록됩니다.
