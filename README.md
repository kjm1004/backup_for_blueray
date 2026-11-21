# BD-Planner 사용법 (Windows, Python 3.9+, 추가 설치 없음)

```
python bd_planner.py new 사진2차 "H:\사진 2차 백업"          # 원본 폴더는 직접 지정 (--capacity-gb 90, --binary 로 GiB)
python bd_planner.py subfolders 사진2차                       # 하부 폴더 목록 확인
python bd_planner.py select 사진2차 폴더A 폴더B 폴더C         # 백업 대상, 적은 순서가 배정 순서
python bd_planner.py scan 사진2차                             # 스캔(읽기 전용) + 90G 배정 + 선경고 + 예상 디스크 수
python bd_planner.py show 사진2차                             # disk01.. 배정 결과, 분할 폴더 표시
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
