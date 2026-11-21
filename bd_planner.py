#!/usr/bin/env python3
"""BD-Planner: 블루레이(M-DISC) 백업 배정 계획 도구.

원본 파일은 복사/이동/수정하지 않는다. 폴더·파일 정보를 SQLite 에 저장하고
90G 단위의 가상 디스크(disk01, disk02 ...)로 배정한다.
디스크별 출력(하드링크 스테이징, CSV, INDEX.txt)과 크기 기준 검증을 제공한다.
"""
import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import time

GB = 1000 ** 3
GIB = 1024 ** 3
DEFAULT_CAPACITY = 90 * GB

SCHEMA = """
CREATE TABLE IF NOT EXISTS plan(
  plan_id INTEGER PRIMARY KEY, name TEXT UNIQUE, root_path TEXT,
  disk_capacity_bytes INTEGER, overhead_per_file INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS selected_folder(
  plan_id INTEGER, seq INTEGER, folder_path TEXT, PRIMARY KEY(plan_id, seq));
CREATE TABLE IF NOT EXISTS file_info(
  file_id INTEGER PRIMARY KEY, plan_id INTEGER, folder_seq INTEGER,
  rel_path TEXT, size_bytes INTEGER, mtime REAL);
CREATE INDEX IF NOT EXISTS ix_file_plan ON file_info(plan_id, folder_seq);
CREATE TABLE IF NOT EXISTS disk(
  plan_id INTEGER, disk_no INTEGER, disk_name TEXT, volume_label TEXT,
  used_bytes INTEGER, file_count INTEGER, status TEXT DEFAULT '계획됨',
  burned_at TEXT, verified_at TEXT, note TEXT, PRIMARY KEY(plan_id, disk_no));
CREATE TABLE IF NOT EXISTS assignment(
  plan_id INTEGER, file_id INTEGER, disk_no INTEGER, order_in_disk INTEGER,
  PRIMARY KEY(plan_id, file_id));
CREATE TABLE IF NOT EXISTS warning(
  plan_id INTEGER, file_id INTEGER, kind TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS disk_history(
  hist_id INTEGER PRIMARY KEY, plan_id INTEGER, disk_no INTEGER, at TEXT, event TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS burn_record(
  rec_id INTEGER PRIMARY KEY, plan_id INTEGER, disk_no INTEGER, copy_no INTEGER,
  media_id TEXT, location TEXT, burned_at TEXT, verified_at TEXT, verify_result TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS media_type(
  key TEXT PRIMARY KEY, label TEXT, capacity_bytes INTEGER, per_file_overhead INTEGER,
  fixed_overhead INTEGER, sort INTEGER);
CREATE TABLE IF NOT EXISTS disk_media(
  plan_id INTEGER, disk_no INTEGER, media_key TEXT, PRIMARY KEY(plan_id, disk_no));
CREATE TABLE IF NOT EXISTS plan_setting(
  plan_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(plan_id, key));
CREATE TABLE IF NOT EXISTS disk_capacity(
  plan_id INTEGER, disk_no INTEGER, capacity_bytes INTEGER, PRIMARY KEY(plan_id, disk_no));
"""

STATUSES = ["계획됨", "굽는 중", "완료", "검증됨"]

# 미디어 종류 기본값: (key, 라벨, 기록 가능 용량(바이트: 드라이브가 보고하는 Free Space), 파일당 오버헤드, 고정 오버헤드)
# 100GB = 48,878,592 섹터 × 2048,  50GB = 24,438,784 섹터,  25GB = 12,219,392 섹터
DEFAULT_MEDIA = [
    ("BD100", "BD-R XL 100GB", 100_103_356_416, 4096, 64 * 1024 * 1024),
    ("BD50", "BD-R DL 50GB", 50_050_629_632, 4096, 64 * 1024 * 1024),
    ("BD25", "BD-R 25GB", 25_025_314_816, 4096, 64 * 1024 * 1024),
]


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    for i, (key, label, cap, per_file, fixed) in enumerate(DEFAULT_MEDIA):
        conn.execute("INSERT OR IGNORE INTO media_type VALUES(?,?,?,?,?,?)", (key, label, cap, per_file, fixed, i))
    conn.commit()
    return conn


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def long_path(p):
    """Windows 긴 경로(260자 초과) 대응."""
    if os.name == "nt":
        p = os.path.abspath(p)
        if not p.startswith("\\\\?\\"):
            p = "\\\\?\\UNC" + p[1:] if p.startswith("\\\\") else "\\\\?\\" + p
    return p


# ---------------------------------------------------------------- plan
def get_plan(conn, name):
    row = conn.execute("SELECT plan_id, root_path, disk_capacity_bytes, overhead_per_file, name "
                       "FROM plan WHERE name=?", (name,)).fetchone()
    if not row:
        raise SystemExit(f"계획 '{name}' 이(가) 없습니다.")
    return dict(zip(("plan_id", "root", "capacity", "overhead", "name"), row))


# ---------------------------------------------------------------- per-disk capacity
def get_capacities(conn, plan):
    """디스크별로 따로 지정한 용량 {disk_no: bytes}. 없는 디스크는 계획 기본 용량."""
    return dict(conn.execute("SELECT disk_no, capacity_bytes FROM disk_capacity WHERE plan_id=?",
                             (plan["plan_id"],)).fetchall())


def capacity_of(caps, plan, disk_no):
    return caps.get(disk_no, plan["capacity"])


def set_capacity(conn, plan, disk_no, capacity_bytes):
    """한 디스크의 용량 지정. capacity_bytes=None 이면 기본 용량으로 되돌림."""
    if capacity_bytes is None:
        conn.execute("DELETE FROM disk_capacity WHERE plan_id=? AND disk_no=?", (plan["plan_id"], disk_no))
    else:
        if capacity_bytes <= 0:
            raise SystemExit("디스크 용량은 0보다 커야 합니다.")
        conn.execute("INSERT OR REPLACE INTO disk_capacity VALUES(?,?,?)",
                     (plan["plan_id"], disk_no, int(capacity_bytes)))
    conn.commit()


def set_capacities(conn, plan, values):
    """values: disk01 부터 순서대로의 용량(바이트) 목록. None 은 기본 용량. 기존 지정은 모두 교체."""
    conn.execute("DELETE FROM disk_capacity WHERE plan_id=?", (plan["plan_id"],))
    for i, v in enumerate(values, 1):
        if v is not None:
            if v <= 0:
                raise SystemExit("디스크 용량은 0보다 커야 합니다.")
            conn.execute("INSERT INTO disk_capacity VALUES(?,?,?)", (plan["plan_id"], i, int(v)))
    conn.commit()


def max_capacity(caps, plan):
    return max([plan["capacity"], *caps.values()])


# ---------------------------------------------------------------- media types
def media_types(conn):
    """{key: {label, capacity, per_file, fixed}} (정렬 순서 유지)"""
    return {r[0]: {"label": r[1], "capacity": r[2], "per_file": r[3], "fixed": r[4]}
            for r in conn.execute("SELECT key, label, capacity_bytes, per_file_overhead, fixed_overhead "
                                  "FROM media_type ORDER BY sort, key")}


def set_media_type(conn, key, capacity=None, per_file=None, fixed=None, label=None):
    """미디어 종류의 값을 바꾼다(None 은 유지). 용량·오버헤드는 바이트."""
    if key not in media_types(conn):
        raise SystemExit(f"알 수 없는 미디어 종류: {key}")
    for col, v in (("capacity_bytes", capacity), ("per_file_overhead", per_file),
                   ("fixed_overhead", fixed), ("label", label)):
        if v is None:
            continue
        if col != "label" and v < 0:
            raise SystemExit("용량과 오버헤드는 0 이상이어야 합니다.")
        if col == "capacity_bytes" and v <= 0:
            raise SystemExit("미디어 용량은 0보다 커야 합니다.")
        conn.execute(f"UPDATE media_type SET {col}=? WHERE key=?", (v, key))
    conn.commit()


def reset_media_types(conn):
    for i, (key, label, cap, per_file, fixed) in enumerate(DEFAULT_MEDIA):
        conn.execute("INSERT OR REPLACE INTO media_type VALUES(?,?,?,?,?,?)", (key, label, cap, per_file, fixed, i))
    conn.commit()


def get_setting(conn, plan, key, default=None):
    r = conn.execute("SELECT value FROM plan_setting WHERE plan_id=? AND key=?", (plan["plan_id"], key)).fetchone()
    return r[0] if r else default


def set_setting(conn, plan, key, value):
    if value is None:
        conn.execute("DELETE FROM plan_setting WHERE plan_id=? AND key=?", (plan["plan_id"], key))
    else:
        conn.execute("INSERT OR REPLACE INTO plan_setting VALUES(?,?,?)", (plan["plan_id"], key, value))
    conn.commit()


def get_disk_media(conn, plan):
    return dict(conn.execute("SELECT disk_no, media_key FROM disk_media WHERE plan_id=?",
                             (plan["plan_id"],)).fetchall())


def set_default_media(conn, plan, key):
    """지정이 없는 디스크에 쓸 기본 미디어 종류. None 이면 계획의 '디스크 용량'·'파일당 오버헤드' 숫자를 쓴다."""
    if key is not None and key not in media_types(conn):
        raise SystemExit(f"알 수 없는 미디어 종류: {key}")
    set_setting(conn, plan, "default_media", key)


def assign_media(conn, plan, disk_nos, key):
    """디스크별 미디어 종류 지정(key=None 이면 지정 해제 → 기본값). 같은 디스크의 '직접 지정 용량'은 지운다."""
    if key is not None and key not in media_types(conn):
        raise SystemExit(f"알 수 없는 미디어 종류: {key}")
    pid = plan["plan_id"]
    for n in disk_nos:
        if key is None:
            conn.execute("DELETE FROM disk_media WHERE plan_id=? AND disk_no=?", (pid, n))
        else:
            conn.execute("INSERT OR REPLACE INTO disk_media VALUES(?,?,?)", (pid, n, key))
            conn.execute("DELETE FROM disk_capacity WHERE plan_id=? AND disk_no=?", (pid, n))
    conn.commit()


class Specs:
    """디스크 번호별 (사용 가능 용량, 파일당 오버헤드)를 한곳에서 결정한다.
    용량: 직접 지정(disk_capacity) > 지정한 미디어 종류(용량 - 고정 오버헤드) > 기본 미디어 > 계획 숫자.
    파일당 오버헤드: 지정/기본 미디어 종류의 값 > 계획 숫자."""

    def __init__(self, conn, plan):
        self.plan = plan
        self.custom = get_capacities(conn, plan)
        self.media = get_disk_media(conn, plan)
        self.types = media_types(conn)
        dm = get_setting(conn, plan, "default_media")
        self.default_media = dm if dm in self.types else None
        self.explicit = sorted(set(self.custom) | set(self.media))
        self.far = max(self.explicit, default=0) + 1000      # '지정 없는 디스크'를 대표하는 번호

    def media_key(self, n):
        k = self.media.get(n, self.default_media)
        return k if k in self.types else None

    def media_label(self, n):
        k = self.media_key(n)
        return self.types[k]["label"] if k else ""

    def cap(self, n):
        if n in self.custom:
            return self.custom[n]
        k = self.media_key(n)
        if k:
            t = self.types[k]
            return max(t["capacity"] - t["fixed"], 0)
        return self.plan["capacity"]

    def ovh(self, n):
        k = self.media_key(n)
        return self.types[k]["per_file"] if k else self.plan["overhead"]

    def fits_later(self, n, size):
        """번호가 n 보다 큰 디스크 중(지정 디스크 + 기본 설정 디스크) 이 파일이 들어가는 곳이 있는가."""
        cands = [k for k in self.explicit if k > n] + [max(n, self.far)]
        return any(size + self.ovh(k) <= self.cap(k) for k in cands)

    def max_file_size(self):
        """어느 디스크에든 들어갈 수 있는 가장 큰 파일 크기."""
        return max(self.cap(k) - self.ovh(k) for k in self.explicit + [self.far])


# ---------------------------------------------------------------- settings save / load
SETTINGS_VERSION = 1


def export_settings(conn, plan, folders=None, extra=None):
    """①~⑤ 설정을 dict 로: 원본·이름·용량·오버헤드, 기본 미디어, 폴더 선택/순서, 디스크별 미디어, 미디어 종류 값.
    folders 를 주면 그 목록(화면의 체크·순서)을, 없으면 DB 의 선택 목록을 쓴다. extra 는 그대로 합쳐 저장(화면 전용 값 등)."""
    pid = plan["plan_id"]
    if folders is None:
        folders = [r[0] for r in conn.execute("SELECT folder_path FROM selected_folder WHERE plan_id=? ORDER BY seq",
                                              (pid,))]
    d = {
        "app": "bd_planner", "version": SETTINGS_VERSION, "saved_at": now(),
        "plan": {"name": plan["name"], "root": plan["root"], "capacity_bytes": plan["capacity"],
                 "overhead_per_file": plan["overhead"]},
        "folders": list(folders),
        "default_media": get_setting(conn, plan, "default_media"),
        "disk_media": {str(n): k for n, k in sorted(get_disk_media(conn, plan).items())},
        "media_types": {k: {"label": t["label"], "capacity_bytes": t["capacity"],
                            "per_file_overhead": t["per_file"], "fixed_overhead": t["fixed"]}
                        for k, t in media_types(conn).items()},
    }
    if extra:
        d.update(extra)
    return d


def write_settings(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def read_settings(path):
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError) as e:
        raise SystemExit(f"설정 파일을 읽을 수 없습니다: {e}")
    if not isinstance(d, dict) or d.get("app") != "bd_planner" or "plan" not in d:
        raise SystemExit("BD-Planner 설정 파일이 아닙니다.")
    if d.get("version", 0) > SETTINGS_VERSION:
        raise SystemExit("더 새로운 버전에서 저장한 설정 파일입니다. 프로그램을 업데이트하세요.")
    return d


def media_types_differ(conn, data):
    """저장된 미디어 종류 값이 지금 값과 다른 키 목록."""
    cur = media_types(conn)
    out = []
    for k, t in (data.get("media_types") or {}).items():
        c = cur.get(k)
        if c and (c["capacity"], c["per_file"], c["fixed"]) != (
                t.get("capacity_bytes"), t.get("per_file_overhead"), t.get("fixed_overhead")):
            out.append(k)
    return out


def apply_settings(conn, data, name=None, root=None, apply_media_types=False, force=False):
    """설정을 DB 에 반영한다. (plan, notes) 반환.
    계획이 없으면 만들고, 있으면 용량·오버헤드를 갱신. 스캔 결과가 있으면 폴더 선택은 건드리지 않고(다음 스캔 때 적용),
    원본 폴더가 달라지면 force 없이는 거부한다. 배정은 바꾸지 않는다(재배정은 사용자가 실행)."""
    p = data["plan"]
    name, root = name or p["name"], root or p["root"]
    notes = []
    row = conn.execute("SELECT plan_id, root_path FROM plan WHERE name=?", (name,)).fetchone()
    scanned = bool(row and conn.execute("SELECT 1 FROM file_info WHERE plan_id=? LIMIT 1", (row[0],)).fetchone())
    if row is None:
        create_plan(conn, name, root, p.get("capacity_bytes", DEFAULT_CAPACITY), p.get("overhead_per_file", 0))
    else:
        if os.path.normcase(row[1]) != os.path.normcase(root):
            if scanned and not force:
                raise SystemExit(f"계획 '{name}' 은(는) 다른 원본 폴더({row[1]})로 스캔되어 있습니다. "
                                 "바꾸면 스캔·배정이 초기화됩니다(--force).")
            if not os.path.isdir(root):
                raise SystemExit(f"원본 폴더에 접근할 수 없습니다: {root}")
            if scanned:
                for t in ("selected_folder", "file_info", "assignment", "disk", "warning"):
                    conn.execute(f"DELETE FROM {t} WHERE plan_id=?", (row[0],))
                scanned = False
                notes.append("원본 폴더가 바뀌어 기존 스캔·배정을 초기화했습니다.")
        conn.execute("UPDATE plan SET root_path=?, disk_capacity_bytes=?, overhead_per_file=? WHERE plan_id=?",
                     (root, p.get("capacity_bytes", DEFAULT_CAPACITY), p.get("overhead_per_file", 0), row[0]))
        conn.commit()
    plan = get_plan(conn, name)

    # 미디어 종류 값(전 계획 공통)
    diff = media_types_differ(conn, data)
    if apply_media_types:
        for k, t in (data.get("media_types") or {}).items():
            if k in media_types(conn):
                set_media_type(conn, k, t.get("capacity_bytes"), t.get("per_file_overhead"),
                               t.get("fixed_overhead"), t.get("label"))
        if diff:
            notes.append("미디어 종류 값을 저장된 값으로 바꿨습니다: " + ", ".join(diff))
    elif diff:
        notes.append("저장된 미디어 종류 값이 현재와 다릅니다(" + ", ".join(diff) + ") — 적용하지 않았습니다.")

    # 폴더 선택·순서
    want = data.get("folders") or []
    if scanned:
        notes.append("스캔 결과가 있어 폴더 선택은 DB 에 반영하지 않았습니다(다음 스캔 때 화면의 선택이 적용됩니다).")
    else:
        ok = [f for f in want if os.path.isdir(os.path.join(plan["root"], f))]
        if len(ok) != len(want):
            notes.append("원본에 없는 폴더는 건너뜀: " + ", ".join(f for f in want if f not in ok))
        set_folders(conn, plan, ok)

    # 기본 미디어·디스크별 미디어
    types = media_types(conn)
    dm = data.get("default_media")
    set_default_media(conn, plan, dm if dm in types else None)
    if dm and dm not in types:
        notes.append(f"알 수 없는 기본 미디어 종류: {dm}")
    conn.execute("DELETE FROM disk_media WHERE plan_id=?", (plan["plan_id"],))
    for n, k in (data.get("disk_media") or {}).items():
        if k in types and str(n).isdigit():
            conn.execute("INSERT OR REPLACE INTO disk_media VALUES(?,?,?)", (plan["plan_id"], int(n), k))
        else:
            notes.append(f"무시한 디스크 미디어 지정: disk{n}={k}")
    conn.commit()
    log_event(conn, plan, 0, "설정 불러옴", f"저장 시각 {data.get('saved_at', '?')}")
    return plan, notes


def create_plan(conn, name, root, capacity=DEFAULT_CAPACITY, overhead=0):
    if not os.path.isdir(root):
        raise SystemExit(f"원본 폴더에 접근할 수 없습니다: {root}")
    conn.execute("INSERT INTO plan(name, root_path, disk_capacity_bytes, overhead_per_file, created_at) "
                 "VALUES(?,?,?,?,?)", (name, root, capacity, overhead, now()))
    conn.commit()


def list_subfolders(root):
    return sorted((e.name for e in os.scandir(root) if e.is_dir(follow_symlinks=False)), key=natural_key)


def set_folders(conn, plan, folders):
    """folders: 루트 아래 폴더명(또는 상대경로) 목록, 순서가 곧 배정 순서."""
    for f in folders:
        if not os.path.isdir(os.path.join(plan["root"], f)):
            raise SystemExit(f"폴더가 없습니다: {f}")
    pid = plan["plan_id"]
    conn.execute("DELETE FROM selected_folder WHERE plan_id=?", (pid,))
    conn.executemany("INSERT INTO selected_folder VALUES(?,?,?)",
                     [(pid, i + 1, f) for i, f in enumerate(folders)])
    conn.commit()


# ---------------------------------------------------------------- scan
def scan(conn, plan, progress=None):
    """전체 재스캔: 기존 파일 목록·배정·디스크(상태 포함)를 모두 지우고 처음부터 다시 만든다.
    이미 구운 디스크가 있으면 add_folders()/allocate_new() 를 쓸 것."""
    pid = plan["plan_id"]
    conn.execute("DELETE FROM file_info WHERE plan_id=?", (pid,))
    conn.execute("DELETE FROM assignment WHERE plan_id=?", (pid,))
    conn.execute("DELETE FROM disk WHERE plan_id=?", (pid,))
    conn.execute("DELETE FROM warning WHERE plan_id=?", (pid,))
    folders = conn.execute("SELECT seq, folder_path FROM selected_folder WHERE plan_id=? ORDER BY seq",
                           (pid,)).fetchall()
    total = _scan_folders(conn, plan, folders, progress)
    _size_warnings(conn, plan)
    log_event(conn, plan, 0, "전체 재스캔", f"파일 {total[0]:,}개 — 배정·디스크 상태 초기화")
    return total


def _scan_folders(conn, plan, folders, progress=None):
    """folders: [(seq, 폴더명)] 을 읽어 file_info 에 추가. (파일수, 바이트) 반환."""
    pid, root = plan["plan_id"], plan["root"]
    total_files = total_bytes = 0
    for seq, folder in folders:
        stack = [os.path.join(root, folder)]
        batch = []
        while stack:
            d = stack.pop()
            try:
                entries = list(os.scandir(long_path(d)))
            except OSError as e:
                _warn(conn, pid, None, "access", f"폴더 읽기 실패: {d} ({e})")
                continue
            for e in entries:
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(os.path.join(d, e.name))
                    elif e.is_file(follow_symlinks=False):
                        st = e.stat(follow_symlinks=False)
                        rel = os.path.relpath(os.path.join(d, e.name), root)
                        batch.append((pid, seq, rel, st.st_size, st.st_mtime))
                        total_files += 1
                        total_bytes += st.st_size
                except OSError as ex:
                    _warn(conn, pid, None, "access", f"파일 읽기 실패: {e.path} ({ex})")
            if progress and total_files % 1000 < len(entries):
                progress(total_files, total_bytes)
        conn.executemany("INSERT INTO file_info(plan_id, folder_seq, rel_path, size_bytes, mtime) "
                         "VALUES(?,?,?,?,?)", batch)
        conn.commit()
    return total_files, total_bytes


def _size_warnings(conn, plan, min_file_id=0):
    """선경고: 0바이트, 디스크 용량 초과 파일(가장 큰 디스크 기준)."""
    pid = plan["plan_id"]
    biggest = Specs(conn, plan).max_file_size()
    for fid, rel, size in conn.execute(
            "SELECT file_id, rel_path, size_bytes FROM file_info WHERE plan_id=? AND file_id>? AND "
            "(size_bytes=0 OR size_bytes>?)", (pid, min_file_id, biggest)).fetchall():
        if size == 0:
            _warn(conn, pid, fid, "zero", f"0바이트 파일: {rel}")
        else:
            _warn(conn, pid, fid, "oversize", f"디스크 용량 초과 ({size / GB:.1f} GB): {rel}")
    conn.commit()


# ---------------------------------------------------------------- incremental add
def new_subfolders(conn, plan):
    """원본 루트 아래 하부 폴더 중 아직 선택 목록(selected_folder)에 없는 것."""
    have = {r[0] for r in conn.execute("SELECT folder_path FROM selected_folder WHERE plan_id=?",
                                       (plan["plan_id"],))}
    return [f for f in list_subfolders(plan["root"]) if f not in have]


def add_folders(conn, plan, folders, progress=None):
    """새 하위 폴더만 선택 목록 '끝'에 추가하고 그 폴더만 스캔한다.
    기존 파일 목록·배정·디스크 상태는 건드리지 않는다. (추가된 폴더 수, 파일 수, 바이트) 반환."""
    pid = plan["plan_id"]
    have = {r[0] for r in conn.execute("SELECT folder_path FROM selected_folder WHERE plan_id=?", (pid,))}
    todo = [f for f in folders if f not in have]
    for f in todo:
        if not os.path.isdir(os.path.join(plan["root"], f)):
            raise SystemExit(f"폴더가 없습니다: {f}")
    seq0 = conn.execute("SELECT COALESCE(MAX(seq),0) FROM selected_folder WHERE plan_id=?", (pid,)).fetchone()[0]
    min_fid = conn.execute("SELECT COALESCE(MAX(file_id),0) FROM file_info").fetchone()[0]
    new = [(seq0 + i + 1, f) for i, f in enumerate(todo)]
    conn.executemany("INSERT INTO selected_folder VALUES(?,?,?)", [(pid, q, f) for q, f in new])
    conn.commit()
    n, b = _scan_folders(conn, plan, new, progress)
    _size_warnings(conn, plan, min_fid)
    return len(todo), n, b


def allocate_new(conn, plan):
    """아직 배정되지 않은 파일만 배정한다. (배정한 파일 수, 내용이 바뀐 디스크 번호 목록) 반환. 이미 구운 디스크('계획됨'이 아닌 것)는 절대 건드리지 않는다.
    '계획됨' 디스크의 남는 공간을 번호순으로 먼저 채우고, 넘치면 새 디스크(마지막 번호+1…)를 만든다."""
    pid = plan["plan_id"]
    sp = Specs(conn, plan)
    state = {}   # disk_no -> [used, count, order, status, is_new]
    for no, used, cnt, st in conn.execute("SELECT disk_no, used_bytes, file_count, status FROM disk "
                                          "WHERE plan_id=? ORDER BY disk_no", (pid,)):
        order = conn.execute("SELECT COALESCE(MAX(order_in_disk),0) FROM assignment WHERE plan_id=? AND disk_no=?",
                             (pid, no)).fetchone()[0]
        state[no] = [used, cnt, order, st, False]
    conn.execute("DELETE FROM warning WHERE plan_id=? AND kind='unassigned'", (pid,))

    pending = conn.execute(
        "SELECT f.file_id, f.rel_path, f.size_bytes, f.folder_seq FROM file_info f "
        "LEFT JOIN assignment a ON a.plan_id=f.plan_id AND a.file_id=f.file_id "
        "WHERE f.plan_id=? AND a.file_id IS NULL ORDER BY f.folder_seq", (pid,)).fetchall()
    by_seq = {}
    for r in pending:
        by_seq.setdefault(r[3], []).append(r)
    placed = 0
    touched = set()
    for seq in sorted(by_seq):
        for fid, rel, size, _ in sorted(by_seq[seq], key=lambda r: natural_key(r[1])):
            target = next((n for n in sorted(state) if state[n][3] == STATUSES[0]
                           and state[n][0] + size + sp.ovh(n) <= sp.cap(n)), None)
            if target is None:
                n = max(state, default=0) + 1
                while size + sp.ovh(n) > sp.cap(n):     # 용량이 모자란 번호는 건너뛰되, 담을 곳이 없으면 포기
                    if not sp.fits_later(n, size):
                        n = None
                        break
                    state[n] = [0, 0, 0, STATUSES[0], True]   # 빈 디스크로 남음(경고)
                    n += 1
                if n is None:
                    _warn(conn, pid, fid, "unassigned",
                          f"배정 불가(어느 디스크에도 들어가지 않음, {size / GB:.2f} GB): {rel}")
                    continue
                state[n] = [0, 0, 0, STATUSES[0], True]
                target = n
            st = state[target]
            st[0] += size + sp.ovh(target)
            st[1] += 1
            st[2] += 1
            conn.execute("INSERT INTO assignment VALUES(?,?,?,?)", (pid, fid, target, st[2]))
            placed += 1
            touched.add(target)
    for n, (used, cnt, _o, _st, is_new) in state.items():
        if is_new:
            conn.execute("INSERT INTO disk(plan_id, disk_no, disk_name, volume_label, used_bytes, file_count) "
                         "VALUES(?,?,?,?,?,?)", (pid, n, f"disk{n:02d}", f"{plan['name']}_disk{n:02d}", used, cnt))
            if cnt == 0:
                _warn(conn, pid, None, "emptydisk",
                      f"disk{n:02d}: 용량({sp.cap(n) / GB:.2f} GB)이 작아 배정된 파일이 없습니다.")
        else:
            conn.execute("UPDATE disk SET used_bytes=?, file_count=? WHERE plan_id=? AND disk_no=?",
                         (used, cnt, pid, n))
    conn.commit()
    for t in sorted(touched):
        log_event(conn, plan, t, "추가 배정", f"새 폴더 파일 배정 → 현재 {state[t][1]:,}개")
    return placed, sorted(touched)


def _warn(conn, pid, fid, kind, msg):
    conn.execute("INSERT INTO warning VALUES(?,?,?,?)", (pid, fid, kind, msg))


def estimate_disks(total_bytes, capacity=DEFAULT_CAPACITY):
    """십진(GB)·이진(GiB) 기준 예상 디스크 수."""
    # 총 바이트는 실측값이므로 차이는 용량 해석(N GB vs N GiB)에서만 생긴다.
    cap_i = max(1, round(capacity * GIB / GB))
    return -(-total_bytes // capacity), -(-total_bytes // cap_i)


# ---------------------------------------------------------------- allocate
def allocate(conn, plan):
    """목록 순서 → 폴더 내 경로 자연정렬 순으로 디스크를 채움. 파일은 쪼개지 않음.
    디스크마다 용량이 다를 수 있다(disk_capacity). 지정이 없는 디스크는 계획 기본 용량.
    재배정 시 구성이 그대로인 디스크는 상태(완료/검증됨 등)를 유지하고, 바뀐 디스크만 '계획됨'으로 초기화한다."""
    pid = plan["plan_id"]
    sp = Specs(conn, plan)

    # 이전 배정 스냅샷(상태 보존용)
    old_members = {}
    for fid, no in conn.execute("SELECT file_id, disk_no FROM assignment WHERE plan_id=? "
                                "ORDER BY disk_no, order_in_disk", (pid,)):
        old_members.setdefault(no, []).append(fid)
    old_state = {r[0]: r[1:] for r in conn.execute(
        "SELECT disk_no, status, burned_at, verified_at, note FROM disk WHERE plan_id=?", (pid,))}

    conn.execute("DELETE FROM assignment WHERE plan_id=?", (pid,))
    conn.execute("DELETE FROM disk WHERE plan_id=?", (pid,))
    conn.execute("DELETE FROM warning WHERE plan_id=? AND kind IN ('unassigned','emptydisk','changed')", (pid,))

    disk_no = used = count = order = 0
    disks, members = {}, {}

    def open_disk():
        nonlocal disk_no, used, count, order
        if disk_no:
            disks[disk_no] = (used, count)
        disk_no += 1
        used = count = order = 0
        members[disk_no] = []

    for seq, in conn.execute("SELECT seq FROM selected_folder WHERE plan_id=? ORDER BY seq", (pid,)).fetchall():
        files = conn.execute("SELECT file_id, rel_path, size_bytes FROM file_info "
                             "WHERE plan_id=? AND folder_seq=?", (pid, seq)).fetchall()
        files.sort(key=lambda r: natural_key(r[1]))
        for fid, rel, size in files:
            if disk_no == 0:
                open_disk()
            while used + size + sp.ovh(disk_no) > sp.cap(disk_no):
                # 이후 디스크(번호가 더 큰 것) 중 이 파일을 담을 수 있는 곳이 있어야 계속 진행
                if not sp.fits_later(disk_no, size):
                    _warn(conn, pid, fid, "unassigned",
                          f"배정 불가(어느 디스크에도 들어가지 않음, {size / GB:.2f} GB): {rel}")
                    break
                open_disk()
            else:
                used += size + sp.ovh(disk_no)
                count += 1
                order += 1
                members[disk_no].append(fid)
                conn.execute("INSERT INTO assignment VALUES(?,?,?,?)", (pid, fid, disk_no, order))
    if disk_no:
        disks[disk_no] = (used, count)

    for n, (u, c) in disks.items():
        conn.execute("INSERT INTO disk(plan_id, disk_no, disk_name, volume_label, used_bytes, file_count) "
                     "VALUES(?,?,?,?,?,?)", (pid, n, f"disk{n:02d}", f"{plan['name']}_disk{n:02d}", u, c))
        if c == 0:
            _warn(conn, pid, None, "emptydisk",
                  f"disk{n:02d}: 용량({sp.cap(n) / GB:.2f} GB)이 작아 배정된 파일이 없습니다.")
        prev = old_state.get(n)
        if prev and prev[0] != STATUSES[0]:
            if old_members.get(n) == members.get(n):      # 구성 동일 → 상태 유지
                conn.execute("UPDATE disk SET status=?, burned_at=?, verified_at=?, note=? "
                             "WHERE plan_id=? AND disk_no=?", (*prev, pid, n))
            else:
                _warn(conn, pid, None, "changed",
                      f"disk{n:02d}: 이미 '{prev[0]}' 상태였으나 구성이 바뀌어 '계획됨'으로 초기화됨 — 다시 구워야 합니다.")
                log_event(conn, plan, n, "구성 변경", f"'{prev[0]}' → '계획됨' 초기화(재배정)")
    conn.commit()
    return disks


def locked_disks(conn, plan):
    """'계획됨'이 아닌(이미 굽기 시작/완료/검증된) 디스크 번호."""
    return [r[0] for r in conn.execute(
        "SELECT disk_no FROM disk WHERE plan_id=? AND status<>? ORDER BY disk_no", (plan["plan_id"], STATUSES[0]))]


# ---------------------------------------------------------------- queries
def disk_folders(conn, plan, disk_no):
    """디스크별 폴더 구성: (폴더, 파일수, 바이트)."""
    return conn.execute(
        "SELECT sf.folder_path, COUNT(*), SUM(f.size_bytes) FROM assignment a "
        "JOIN file_info f ON f.file_id=a.file_id JOIN selected_folder sf "
        "ON sf.plan_id=f.plan_id AND sf.seq=f.folder_seq "
        "WHERE a.plan_id=? AND a.disk_no=? GROUP BY sf.seq ORDER BY sf.seq",
        (plan["plan_id"], disk_no)).fetchall()


def split_folders(conn, plan):
    rows = conn.execute(
        "SELECT sf.folder_path, a.disk_no FROM assignment a "
        "JOIN file_info f ON f.file_id=a.file_id JOIN selected_folder sf "
        "ON sf.plan_id=f.plan_id AND sf.seq=f.folder_seq WHERE a.plan_id=? "
        "GROUP BY sf.seq, a.disk_no ORDER BY sf.seq, a.disk_no", (plan["plan_id"],)).fetchall()
    by = {}
    for folder, no in rows:
        by.setdefault(folder, []).append(str(no))
    return [(f, ",".join(n)) for f, n in by.items() if len(n) > 1]


def disk_files(conn, plan, disk_no):
    return conn.execute(
        "SELECT f.rel_path, f.size_bytes FROM assignment a JOIN file_info f ON f.file_id=a.file_id "
        "WHERE a.plan_id=? AND a.disk_no=? ORDER BY a.order_in_disk", (plan["plan_id"], disk_no)).fetchall()


def set_status(conn, plan, disk_no, status, note=None, log=True):
    if status not in STATUSES:
        raise SystemExit(f"상태는 {STATUSES} 중 하나여야 합니다.")
    col = {"완료": "burned_at", "검증됨": "verified_at"}.get(status)
    conn.execute("UPDATE disk SET status=? WHERE plan_id=? AND disk_no=?", (status, plan["plan_id"], disk_no))
    if col:
        conn.execute(f"UPDATE disk SET {col}=? WHERE plan_id=? AND disk_no=?", (now(), plan["plan_id"], disk_no))
    if note is not None:
        conn.execute("UPDATE disk SET note=? WHERE plan_id=? AND disk_no=?", (note, plan["plan_id"], disk_no))
    if log:
        log_event(conn, plan, disk_no, f"상태 → {status}", note or "")
    conn.commit()


# ---------------------------------------------------------------- history / burn records
def log_event(conn, plan, disk_no, event, detail=""):
    """이력 한 줄 추가. disk_no=0 은 계획 전체에 관한 이벤트."""
    conn.execute("INSERT INTO disk_history(plan_id, disk_no, at, event, detail) VALUES(?,?,?,?,?)",
                 (plan["plan_id"], disk_no, now(), event, detail))
    conn.commit()


def history(conn, plan, disk_no=None):
    """(시각, 디스크번호, 이벤트, 내용) 오래된 순."""
    q = "SELECT at, disk_no, event, detail FROM disk_history WHERE plan_id=?"
    args = [plan["plan_id"]]
    if disk_no is not None:
        q += " AND disk_no=?"
        args.append(disk_no)
    return conn.execute(q + " ORDER BY hist_id", args).fetchall()


def burn_records(conn, plan, disk_no=None):
    """(rec_id, disk_no, copy_no, media_id, location, burned_at, verified_at, verify_result, note)"""
    q = ("SELECT rec_id, disk_no, copy_no, media_id, location, burned_at, verified_at, verify_result, note "
         "FROM burn_record WHERE plan_id=?")
    args = [plan["plan_id"]]
    if disk_no is not None:
        q += " AND disk_no=?"
        args.append(disk_no)
    return conn.execute(q + " ORDER BY disk_no, copy_no", args).fetchall()


def record_burn(conn, plan, disk_no, copy_no=None, media_id="", location="", note=""):
    """'이 디스크를 미디어에 구웠다' 기록(사본 번호별). 상태는 '완료'로(이미 '검증됨'이면 유지). 사본 번호를 돌려준다."""
    pid = plan["plan_id"]
    row = conn.execute("SELECT status FROM disk WHERE plan_id=? AND disk_no=?", (pid, disk_no)).fetchone()
    if not row:
        raise SystemExit(f"disk{disk_no:02d} 이(가) 없습니다.")
    if copy_no is None:
        copy_no = conn.execute("SELECT COALESCE(MAX(copy_no),0)+1 FROM burn_record WHERE plan_id=? AND disk_no=?",
                               (pid, disk_no)).fetchone()[0]
    if conn.execute("SELECT 1 FROM burn_record WHERE plan_id=? AND disk_no=? AND copy_no=?",
                    (pid, disk_no, copy_no)).fetchone():
        raise SystemExit(f"disk{disk_no:02d} 사본 {copy_no} 은(는) 이미 기록되어 있습니다.")
    conn.execute("INSERT INTO burn_record(plan_id, disk_no, copy_no, media_id, location, burned_at, note) "
                 "VALUES(?,?,?,?,?,?,?)", (pid, disk_no, copy_no, media_id, location, now(), note))
    if row[0] in (STATUSES[0], STATUSES[1]):
        set_status(conn, plan, disk_no, "완료", log=False)
    detail = f"사본 {copy_no}" + (f", 미디어 {media_id}" if media_id else "") + \
             (f", 보관 {location}" if location else "") + (f" — {note}" if note else "")
    log_event(conn, plan, disk_no, "백업 완료 기록", detail)
    conn.commit()
    return copy_no


# ---------------------------------------------------------------- staging <-> media compare
_SKIP_NAMES = {"system volume information", "$recycle.bin"}
CHUNK = 4 * 1024 * 1024


def _walk_files(root):
    """{정규화된 상대경로: (표시용 상대경로, 전체경로)}"""
    base = long_path(root)
    out = {}
    for d, dirs, fs in os.walk(base):
        dirs[:] = [x for x in dirs if x.lower() not in _SKIP_NAMES]
        for f in fs:
            full = os.path.join(d, f)
            rel = os.path.relpath(full, base)
            out[os.path.normcase(rel)] = (rel, full)
    return out


def _is_hidden_or_system(path):
    """Windows 숨김/시스템 속성(ImgBurn 이 기본으로 빼는 파일). 다른 OS 는 항상 False."""
    try:
        return bool(getattr(os.stat(path), "st_file_attributes", 0) & 0x6)
    except OSError:
        return False


def _same_content(a, b, on_bytes=None):
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            ca, cb = fa.read(CHUNK), fb.read(CHUNK)
            if on_bytes:
                on_bytes(len(ca))
            if ca != cb:
                return False
            if not ca:
                return True


def compare_dirs(src, dst, full=True, progress=None):
    """스테이징 폴더(src)와 미디어(dst)를 대조한다.
    full=True 면 모든 파일을 바이트 단위로 비교, False 면 존재·크기만(빠른 검사).
    숨김/시스템 파일이 미디어에 없는 것은 실패로 치지 않고 따로 보고한다(ImgBurn 이 기본 제외).
    progress(done_files, total_files, done_bytes) 를 주기적으로 호출."""
    a, b = _walk_files(src), _walk_files(dst)
    res = {"total": len(a), "matched": 0, "bytes": 0, "missing": [], "hidden_missing": [], "extra": [],
           "size_diff": [], "content_diff": [], "read_errors": [], "full": full}
    done_b = [0]
    done_f = 0
    for key in sorted(a, key=lambda k: natural_key(a[k][0])):
        rel, sp = a[key]
        done_f += 1
        if progress:
            progress(done_f, len(a), done_b[0])
        if key not in b:
            (res["hidden_missing"] if _is_hidden_or_system(sp) else res["missing"]).append(rel)
            continue
        dp = b[key][1]
        try:
            ss, ds = os.path.getsize(sp), os.path.getsize(dp)
            if ss != ds:
                res["size_diff"].append((rel, ss, ds))
                continue
            if full:
                def tick(n):
                    done_b[0] += n
                    if progress:
                        progress(done_f, len(a), done_b[0])
                if not _same_content(sp, dp, tick):
                    res["content_diff"].append(rel)
                    continue
        except OSError as e:
            res["read_errors"].append((rel, str(e)))
            continue
        res["matched"] += 1
        res["bytes"] += ss
    res["extra"] = sorted(b[k][0] for k in set(b) - set(a))
    res["ok"] = not (res["missing"] or res["extra"] or res["size_diff"] or res["content_diff"] or res["read_errors"])
    return res


def summarize_compare(res):
    parts = [f"일치 {res['matched']:,}/{res['total']:,}개"]
    for key, label in (("missing", "미디어에 없음"), ("extra", "미디어에만 있음"), ("size_diff", "크기 다름"),
                       ("content_diff", "내용 다름"), ("read_errors", "읽기 오류")):
        if res[key]:
            parts.append(f"{label} {len(res[key])}")
    if res["hidden_missing"]:
        parts.append(f"숨김/시스템 파일 미포함 {len(res['hidden_missing'])}(정상 범위)")
    return ", ".join(parts)


def verify_media(conn, plan, disk_no, stage_dir, media_dir, full=True, copy_no=None, progress=None):
    """스테이징(원본)↔미디어 비교 후 이력에 기록. 전체(바이트) 비교가 모두 일치하면 상태를 '검증됨'으로.
    copy_no 가 없으면 가장 최근 사본 기록에 결과를 붙이고, 사본 기록이 없으면 새로 만든다."""
    if not os.path.isdir(stage_dir):
        raise SystemExit(f"스테이징 폴더가 없습니다: {stage_dir}")
    if not os.path.isdir(media_dir):
        raise SystemExit(f"미디어 경로에 접근할 수 없습니다: {media_dir}")
    res = compare_dirs(stage_dir, media_dir, full, progress)
    mode = "전체(바이트)" if full else "빠른(크기)"
    verdict = ("전체일치" if full else "빠른일치") if res["ok"] else "불일치"
    pid = plan["plan_id"]
    rec = (conn.execute("SELECT rec_id FROM burn_record WHERE plan_id=? AND disk_no=? AND copy_no=?",
                        (pid, disk_no, copy_no)).fetchone() if copy_no else
           conn.execute("SELECT rec_id FROM burn_record WHERE plan_id=? AND disk_no=? ORDER BY copy_no DESC LIMIT 1",
                        (pid, disk_no)).fetchone())
    if rec is None:
        record_burn(conn, plan, disk_no, copy_no, note="검증 시 자동 생성")
        rec = conn.execute("SELECT rec_id FROM burn_record WHERE plan_id=? AND disk_no=? ORDER BY copy_no DESC LIMIT 1",
                           (pid, disk_no)).fetchone()
    prev = conn.execute("SELECT verify_result FROM burn_record WHERE rec_id=?", (rec[0],)).fetchone()[0]
    if not (res["ok"] and not full and prev == "전체일치"):    # 빠른 검사가 앞선 전체 일치 기록을 덮지 않게
        conn.execute("UPDATE burn_record SET verified_at=?, verify_result=? WHERE rec_id=?",
                     (now(), verdict, rec[0]))
    log_event(conn, plan, disk_no, f"미디어 비교 {mode}: {verdict}", f"{summarize_compare(res)} | 미디어 {media_dir}")
    status = conn.execute("SELECT status FROM disk WHERE plan_id=? AND disk_no=?", (pid, disk_no)).fetchone()[0]
    if res["ok"] and full:
        set_status(conn, plan, disk_no, "검증됨", log=False)
    elif not res["ok"] and status == "검증됨":
        # 다른 사본 중 전체 일치한 것이 없으면 '완료'로 되돌린다
        if not conn.execute("SELECT 1 FROM burn_record WHERE plan_id=? AND disk_no=? AND verify_result='전체일치'",
                            (pid, disk_no)).fetchone():
            set_status(conn, plan, disk_no, "완료", note=None, log=False)
            log_event(conn, plan, disk_no, "상태 강등", "'검증됨' → '완료' (최근 비교 불일치, 일치한 다른 사본 없음)")
    conn.commit()
    res["verdict"] = verdict
    return res


# ---------------------------------------------------------------- export
def export_disk(conn, plan, disk_no, stage_root, hardlink=True, out_dir=None):
    """stage_root/diskNN 에 하드링크 구성 + INDEX.txt, 목록 CSV(out_dir) 생성.
    원본 파일은 변경하지 않는다. 하드링크 불가(다른 드라이브/비NTFS)면 중단한다."""
    name = f"disk{disk_no:02d}"
    dest = os.path.join(stage_root, name)
    files = disk_files(conn, plan, disk_no)
    made = 0
    if hardlink:
        for rel, _size in files:
            src = os.path.join(plan["root"], rel)
            dst = os.path.join(dest, rel)
            os.makedirs(long_path(os.path.dirname(dst)), exist_ok=True)
            if os.path.exists(long_path(dst)):
                continue
            try:
                os.link(long_path(src), long_path(dst))
            except OSError as e:
                raise SystemExit(f"하드링크 실패: {src} -> {dst}\n  {e}\n"
                                 "스테이징 폴더는 원본과 같은 NTFS 드라이브에 있어야 합니다.")
            made += 1
    # INDEX.txt (디스크 루트에 포함될 안내 파일)
    os.makedirs(long_path(dest), exist_ok=True)
    row = conn.execute("SELECT volume_label, used_bytes, file_count FROM disk WHERE plan_id=? AND disk_no=?",
                       (plan["plan_id"], disk_no)).fetchone()
    split = {f: d for f, d in split_folders(conn, plan)}
    with open(os.path.join(dest, "INDEX.txt"), "w", encoding="utf-8-sig") as fh:
        sp = Specs(conn, plan)
        kind = f", {sp.media_label(disk_no)}" if sp.media_label(disk_no) else ""
        fh.write(f"{row[0]}  ({name})\n파일 {row[2]:,}개 / {row[1] / GB:.2f} GB "
                 f"(사용 가능 용량 기준 {sp.cap(disk_no) / GB:.2f} GB{kind})\n\n[포함 폴더]\n")
        for folder, cnt, size in disk_folders(conn, plan, disk_no):
            mark = f"  ※ 분할 폴더(디스크 {split[folder]} 에 나뉨)" if folder in split else ""
            fh.write(f"- {folder}  {cnt:,}개  {size / GB:.2f} GB{mark}\n")
    # 목록 CSV
    out_dir = out_dir or stage_root
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, f"{plan['name']}_{name}.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["disk", "rel_path", "size_bytes"])
        w.writerows((name, rel, size) for rel, size in files)
    log_event(conn, plan, disk_no, "스테이징 내보냄", f"{dest} (파일 {len(files):,}개, 새 링크 {made:,}개)")
    return dest, csv_path, made


def verify_disk(conn, plan, disk_no, disc_root):
    """디스크(또는 스테이징 폴더)의 파일 경로·크기를 DB 와 대조. 해시는 사용하지 않는다."""
    expected = {rel: size for rel, size in disk_files(conn, plan, disk_no)}
    found = {}
    for d, _dirs, fs in os.walk(long_path(disc_root)):
        for f in fs:
            full = os.path.join(d, f)
            rel = os.path.relpath(full, long_path(disc_root))
            if rel.upper() == "INDEX.TXT":
                continue
            found[rel] = os.path.getsize(full)
    missing = sorted(set(expected) - set(found))
    extra = sorted(set(found) - set(expected))
    diff = sorted(r for r in set(expected) & set(found) if expected[r] != found[r])
    ok = not (missing or extra or diff)
    log_event(conn, plan, disk_no, "경로·크기 검증(DB 기준)" + (": 일치" if ok else ": 불일치"),
              f"누락 {len(missing)}, 추가 {len(extra)}, 크기다름 {len(diff)} | {disc_root}")
    return ok, missing, extra, diff


# ---------------------------------------------------------------- CLI
def fmt(b):
    return f"{b / GB:,.2f} GB"


def cmd_show(conn, plan):
    rows = conn.execute("SELECT disk_no, disk_name, used_bytes, file_count, status FROM disk "
                        "WHERE plan_id=? ORDER BY disk_no", (plan["plan_id"],)).fetchall()
    split = dict(split_folders(conn, plan))
    sp = Specs(conn, plan)
    for no, name, used, cnt, st in rows:
        mark = f" ({sp.media_label(no)})" if sp.media_label(no) else (" (직접 지정)" if no in sp.custom else "")
        print(f"{name} [{st}] {fmt(used)} / {fmt(sp.cap(no))}{mark}  파일 {cnt:,}")
        for folder, c, size in disk_folders(conn, plan, no):
            mark = f"  (분할: 디스크 {split[folder]})" if folder in split else ""
            print(f"    {folder}  {c:,}개  {fmt(size)}{mark}")
    for kind, msg in conn.execute("SELECT kind, message FROM warning WHERE plan_id=?", (plan["plan_id"],)):
        print(f"[경고:{kind}] {msg}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="BD-Planner")
    ap.add_argument("--db", default="bd_planner.db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("new", help="계획 생성(원본 폴더는 사용자가 지정)")
    p.add_argument("name"); p.add_argument("root")
    p.add_argument("--capacity-gb", type=float, default=90)
    p.add_argument("--binary", action="store_true", help="용량을 GiB(이진)로 해석")
    p.add_argument("--overhead", type=int, default=0, help="파일당 가산 바이트")
    p = sub.add_parser("subfolders", help="원본 폴더의 하부 폴더 나열"); p.add_argument("name")
    p = sub.add_parser("select", help="백업 대상 폴더 목록(순서대로)")
    p.add_argument("name"); p.add_argument("folders", nargs="+")
    p = sub.add_parser("scan", help="전체 스캔 후 배정 계산(기존 배정·상태 초기화)"); p.add_argument("name")
    p.add_argument("--force", action="store_true", help="구운 디스크가 있어도 초기화하고 전체 재스캔")
    p = sub.add_parser("show", help="배정 결과"); p.add_argument("name")
    p = sub.add_parser("capacity", help="디스크별 용량 지정/조회 (예: capacity 계획 1=95 2=23.3 --binary)")
    p.add_argument("name"); p.add_argument("items", nargs="*", help="번호=용량  (용량 'default' 는 기본값으로)")
    p.add_argument("--binary", action="store_true", help="용량을 GiB(이진)로 해석")
    p.add_argument("--clear", action="store_true", help="지정을 모두 지우고 기본 용량으로")
    p = sub.add_parser("add", help="새 하위 폴더만 추가 스캔·배정(구운 디스크는 그대로). 폴더 생략 시 새 폴더 전부")
    p.add_argument("name"); p.add_argument("folders", nargs="*")
    p.add_argument("--list", action="store_true", help="추가 대상 새 폴더만 나열")
    p = sub.add_parser("mediatypes", help="미디어 종류(100G/50G/25G)의 용량·오버헤드 조회/수정 (모든 계획 공통)")
    p.add_argument("--set", metavar="KEY", help="수정할 종류(BD100/BD50/BD25)")
    p.add_argument("--capacity-gb", type=float, help="기록 가능 용량(GB, 십진)")
    p.add_argument("--per-file", type=int, help="파일당 오버헤드(바이트)")
    p.add_argument("--fixed-mb", type=float, help="디스크당 고정 오버헤드(MB, 십진)")
    p.add_argument("--label"); p.add_argument("--reset", action="store_true", help="세 종류를 기본값으로 복원")
    p = sub.add_parser("media", help="디스크별 미디어 종류 지정/조회")
    p.add_argument("name"); p.add_argument("key", nargs="?", help="BD100 / BD50 / BD25")
    p.add_argument("--disks", nargs="*", type=int, help="적용할 디스크 번호")
    p.add_argument("--from", dest="from_no", type=int, help="이 번호부터 현재 마지막 디스크까지")
    p.add_argument("--default", action="store_true", help="지정 없는 디스크의 기본 미디어로 설정")
    p.add_argument("--clear", action="store_true", help="지정 해제(--disks 생략 시 전체)")
    p = sub.add_parser("save-settings", help="①~⑤ 설정(원본·용량·오버헤드·폴더 선택/순서·미디어 지정·스테이징)을 JSON 으로 저장")
    p.add_argument("name"); p.add_argument("file"); p.add_argument("--stage", help="스테이징 위치도 함께 저장")
    p = sub.add_parser("load-settings", help="저장한 설정을 불러와 계획에 반영(재배정은 따로 allocate/scan)")
    p.add_argument("file"); p.add_argument("--name", help="계획 이름을 바꿔서 불러오기")
    p.add_argument("--root", help="원본 폴더를 다른 경로로(다른 PC 에서 불러올 때)")
    p.add_argument("--apply-media-types", action="store_true", help="저장된 100G/50G/25G 용량·오버헤드도 적용")
    p.add_argument("--force", action="store_true", help="원본 폴더가 달라도 기존 스캔·배정을 초기화하고 적용")
    p = sub.add_parser("allocate", help="스캔 없이 현재 용량 설정으로 다시 배정")
    p.add_argument("name")
    p = sub.add_parser("export", help="하드링크 스테이징 + INDEX + CSV")
    p.add_argument("name"); p.add_argument("--stage", required=True)
    p.add_argument("--disks", nargs="*", type=int); p.add_argument("--no-hardlink", action="store_true")
    p = sub.add_parser("status", help="디스크 상태 변경")
    p.add_argument("name"); p.add_argument("disk", type=int); p.add_argument("status", choices=STATUSES)
    p = sub.add_parser("record", help="백업 완료 기록(사본 번호·미디어 ID·보관 위치) → 상태 '완료'")
    p.add_argument("name"); p.add_argument("disk", type=int)
    p.add_argument("--copy", type=int, help="사본 번호(생략 시 자동)")
    p.add_argument("--media", default="", help="미디어 ID/시리얼/라벨")
    p.add_argument("--location", default="", help="보관 위치")
    p.add_argument("--note", default="")
    p = sub.add_parser("history", help="이력과 사본 기록 보기")
    p.add_argument("name"); p.add_argument("--disk", type=int)
    p = sub.add_parser("compare", help="스테이징(원본)↔미디어 일치 검사. 전체(바이트) 일치 시 '검증됨'")
    p.add_argument("name"); p.add_argument("disk", type=int)
    p.add_argument("--stage", required=True, help="내보내기 때 지정한 스테이징 루트(그 아래 diskNN)")
    p.add_argument("--media", required=True, help="미디어(굽은 디스크) 드라이브/경로")
    p.add_argument("--quick", action="store_true", help="내용 비교 없이 존재·크기만 확인")
    p.add_argument("--copy", type=int, help="결과를 붙일 사본 번호(생략 시 최근 사본)")
    p = sub.add_parser("verify", help="경로·크기를 DB 와 대조(내용 비교 아님)")
    p.add_argument("name"); p.add_argument("disk", type=int); p.add_argument("path")
    a = ap.parse_args(argv)

    conn = connect(a.db)
    if a.cmd == "new":
        unit = GIB if a.binary else GB
        create_plan(conn, a.name, a.root, int(a.capacity_gb * unit), a.overhead)
        print("계획 생성 완료")
        return
    if a.cmd == "mediatypes":
        if a.reset:
            reset_media_types(conn)
        if a.set:
            set_media_type(conn, a.set,
                           None if a.capacity_gb is None else int(a.capacity_gb * GB), a.per_file,
                           None if a.fixed_mb is None else int(a.fixed_mb * 1000 ** 2), a.label)
        for k, t in media_types(conn).items():
            print(f"{k:6} {t['label']:16} 용량 {t['capacity']:>15,} B ({t['capacity'] / GB:.3f} GB)  "
                  f"파일당 {t['per_file']:,} B  고정 {t['fixed'] / 1000 ** 2:.1f} MB")
        return
    if a.cmd == "load-settings":
        plan, notes = apply_settings(conn, read_settings(a.file), a.name, a.root, a.apply_media_types, a.force)
        print(f"계획 '{plan['name']}' 에 설정을 반영했습니다. (원본 {plan['root']})")
        for n in notes:
            print("※", n)
        print("※ 배정에 반영하려면 allocate(재배정) 또는 scan 을 실행하세요.")
        return
    plan = get_plan(conn, a.name)
    if a.cmd == "save-settings":
        extra = {"ui": {"stage": a.stage}} if a.stage else None
        write_settings(a.file, export_settings(conn, plan, extra=extra))
        print(f"저장됨: {a.file}")
        return
    if a.cmd == "media":
        types = media_types(conn)
        if a.key and a.key not in types:
            raise SystemExit(f"알 수 없는 미디어 종류: {a.key} (사용 가능: {', '.join(types)})")
        all_disks = [r[0] for r in conn.execute("SELECT disk_no FROM disk WHERE plan_id=? ORDER BY disk_no",
                                                (plan["plan_id"],))]
        if a.default:
            set_default_media(conn, plan, None if a.clear else a.key)
        elif a.clear:
            assign_media(conn, plan, a.disks or list(get_disk_media(conn, plan)), None)
        elif a.key:
            nos = a.disks or ([n for n in all_disks if n >= a.from_no] if a.from_no else [])
            if not nos:
                raise SystemExit("적용할 디스크를 --disks 또는 --from 으로 지정하세요.")
            assign_media(conn, plan, nos, a.key)
        sp = Specs(conn, plan)
        dm = sp.default_media
        print(f"기본 미디어: {types[dm]['label'] if dm else '없음(계획의 용량·오버헤드 숫자 사용)'}")
        for n in all_disks:
            print(f"  disk{n:02d}: {sp.media_label(n) or '-':16} 사용 가능 {fmt(sp.cap(n))}, 파일당 {sp.ovh(n):,} B"
                  + ("  (용량 직접 지정)" if n in sp.custom else ""))
        if a.key or a.clear or a.default:
            print("※ 적용하려면 allocate(재배정) 또는 scan 을 실행하세요.")
        return
    if a.cmd == "subfolders":
        for f in list_subfolders(plan["root"]):
            print(f)
    elif a.cmd == "select":
        set_folders(conn, plan, a.folders); print(f"{len(a.folders)}개 폴더 선택됨")
    elif a.cmd == "add":
        new = new_subfolders(conn, plan)
        if a.list:
            for f in new:
                print(f)
            print(f"새 폴더 {len(new)}개")
            return
        folders = a.folders or new
        if not folders:
            print("추가할 새 폴더가 없습니다.")
            return
        nf, n, b = add_folders(conn, plan, folders,
                               lambda f, s: print(f"\r스캔 {f:,}개 {fmt(s)}", end="", file=sys.stderr))
        placed, touched = allocate_new(conn, plan)
        print(f"\n새 폴더 {nf}개, 파일 {n:,}개, {fmt(b)} → {placed:,}개 배정")
        print("내용이 바뀐 디스크: " + (", ".join(f"disk{t:02d}" for t in touched) or "없음"))
        print("※ 이 디스크들만 export 를 다시 실행하세요. 이미 구운 디스크는 변경되지 않았습니다.")
        for kind, msg in conn.execute("SELECT kind, message FROM warning WHERE plan_id=? AND kind IN "
                                      "('oversize','unassigned','emptydisk','access','zero')", (plan["plan_id"],)):
            print(f"[경고:{kind}] {msg}")
    elif a.cmd == "scan":
        if locked_disks(conn, plan) and not a.force:
            raise SystemExit("이미 구운(또는 굽는 중) 디스크가 있어 전체 재스캔은 상태·배정을 모두 초기화합니다.\n"
                             "새 폴더만 추가하려면 'add' 를 쓰세요. 정말 초기화하려면 --force.")
        n, b = scan(conn, plan, lambda f, s: print(f"\r스캔 {f:,}개 {fmt(s)}", end="", file=sys.stderr))
        print(f"\n파일 {n:,}개, {fmt(b)}")
        dec, bi = estimate_disks(b, plan["capacity"])
        print(f"예상 디스크 수: 십진 {dec}장 / 이진 {bi}장")
        over = conn.execute("SELECT message FROM warning WHERE plan_id=? AND kind='oversize'",
                            (plan["plan_id"],)).fetchall()
        for (m,) in over:
            print("[선경고]", m)
        d = allocate(conn, plan)
        print(f"배정 완료: {len(d)}장")
    elif a.cmd == "show":
        cmd_show(conn, plan)
    elif a.cmd == "capacity":
        unit = GIB if a.binary else GB
        if a.clear:
            set_capacities(conn, plan, [])
        for it in a.items:
            no, _, val = it.partition("=")
            if not no.isdigit() or not val:
                raise SystemExit(f"형식은 번호=용량 입니다: {it}")
            set_capacity(conn, plan, int(no), None if val == "default" else int(float(val) * unit))
        caps = get_capacities(conn, plan)
        print(f"기본 용량 {fmt(plan['capacity'])}")
        for n in sorted(caps):
            print(f"  disk{n:02d}: {fmt(caps[n])}")
        if a.items or a.clear:
            print("※ 적용하려면 scan(재스캔+배정) 또는 allocate(재배정)을 실행하세요.")
    elif a.cmd == "allocate":
        d = allocate(conn, plan)
        print(f"재배정 완료: {len(d)}장")
        for kind, msg in conn.execute("SELECT kind, message FROM warning WHERE plan_id=? AND kind IN "
                                      "('unassigned','emptydisk','changed')", (plan["plan_id"],)):
            print(f"[경고:{kind}] {msg}")
    elif a.cmd == "export":
        nos = a.disks or [r[0] for r in conn.execute(
            "SELECT disk_no FROM disk WHERE plan_id=? ORDER BY disk_no", (plan["plan_id"],))]
        for n in nos:
            dest, csvp, made = export_disk(conn, plan, n, a.stage, not a.no_hardlink)
            print(f"disk{n:02d}: {dest} (링크 {made}개), {csvp}")
    elif a.cmd == "status":
        set_status(conn, plan, a.disk, a.status); print("변경됨")
    elif a.cmd == "record":
        c = record_burn(conn, plan, a.disk, a.copy, a.media, a.location, a.note)
        print(f"disk{a.disk:02d} 사본 {c} 백업 완료 기록됨")
    elif a.cmd == "history":
        for rec in burn_records(conn, plan, a.disk):
            _id, dn, cp, mid, loc, bat, vat, vres, note = rec
            print(f"[사본] disk{dn:02d} #{cp}  미디어 {mid or '-'}  보관 {loc or '-'}  구움 {bat}  "
                  f"검증 {vat or '-'} {vres or ''}" + (f"  ({note})" if note else ""))
        for at, dn, ev, detail in history(conn, plan, a.disk):
            print(f"{at}  {'계획' if dn == 0 else f'disk{dn:02d}'}  {ev}" + (f"  — {detail}" if detail else ""))
    elif a.cmd == "compare":
        stage_dir = os.path.join(a.stage, f"disk{a.disk:02d}")
        last = [0.0]

        def prog(f, t, b):
            if time.time() - last[0] > 0.5:
                last[0] = time.time()
                print(f"\r비교 {f:,}/{t:,}개  {fmt(b)}", end="", file=sys.stderr)
        res = verify_media(conn, plan, a.disk, stage_dir, a.media, not a.quick, a.copy, prog)
        print(f"\n{res['verdict']}: {summarize_compare(res)}")
        for key, label in (("missing", "미디어에 없음"), ("extra", "미디어에만 있음"), ("content_diff", "내용 다름"),
                           ("hidden_missing", "숨김/시스템(미포함)")):
            for r in res[key][:30]:
                print(f"  {label}: {r}")
        for r, ss, ds in res["size_diff"][:30]:
            print(f"  크기 다름: {r} (스테이징 {ss:,} / 미디어 {ds:,})")
        for r, e in res["read_errors"][:30]:
            print(f"  읽기 오류: {r} ({e})")
        sys.exit(0 if res["ok"] else 1)
    elif a.cmd == "verify":
        ok, miss, extra, diff = verify_disk(conn, plan, a.disk, a.path)
        print("일치" if ok else f"불일치: 누락 {len(miss)}, 추가 {len(extra)}, 크기다름 {len(diff)}")
        for lbl, lst in (("누락", miss), ("추가", extra), ("크기다름", diff)):
            for r in lst[:20]:
                print(f"  {lbl}: {r}")


if __name__ == "__main__":
    main()
