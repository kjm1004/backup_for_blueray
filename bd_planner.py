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
import shutil
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
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


class Cancelled(Exception):
    """사용자가 작업을 중지했다(오류가 아님)."""


class CancelToken:
    """오래 걸리는 작업에 넘기는 중지 신호. 작업은 파일·조각마다 check() 를 불러 중지 요청을 확인하고 정리한다."""

    def __init__(self):
        self._ev = threading.Event()

    def cancel(self):
        self._ev.set()

    @property
    def cancelled(self):
        return self._ev.is_set()

    def check(self):
        if self._ev.is_set():
            raise Cancelled("사용자가 중지했습니다.")


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

    def raw_cap(self, n):
        """고정 오버헤드를 빼기 전의 미디어 기록 가능 용량."""
        if n in self.custom:
            return self.custom[n]
        k = self.media_key(n)
        return self.types[k]["capacity"] if k else self.plan["capacity"]

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
def scan(conn, plan, progress=None, cancel=None):
    """전체 재스캔: 기존 파일 목록·배정·디스크(상태 포함)를 모두 지우고 처음부터 다시 만든다.
    이미 구운 디스크가 있으면 add_folders()/allocate_new() 를 쓸 것.
    cancel 로 중지하면 하나의 트랜잭션이라 이전 결과로 되돌린다."""
    pid = plan["plan_id"]
    conn.execute("DELETE FROM file_info WHERE plan_id=?", (pid,))
    conn.execute("DELETE FROM assignment WHERE plan_id=?", (pid,))
    conn.execute("DELETE FROM disk WHERE plan_id=?", (pid,))
    conn.execute("DELETE FROM warning WHERE plan_id=?", (pid,))
    folders = conn.execute("SELECT seq, folder_path FROM selected_folder WHERE plan_id=? ORDER BY seq",
                           (pid,)).fetchall()
    try:
        total = _scan_folders(conn, plan, folders, progress, cancel, commit=False)
    except Cancelled:
        conn.rollback()                                  # 지운 것도, 읽은 것도 모두 취소
        log_event(conn, plan, 0, "전체 재스캔 중지", "이전 스캔·배정 결과를 그대로 유지")
        raise
    _size_warnings(conn, plan)
    log_event(conn, plan, 0, "전체 재스캔", f"파일 {total[0]:,}개 — 배정·디스크 상태 초기화")
    return total


def _scan_folders(conn, plan, folders, progress=None, cancel=None, commit=True):
    """folders: [(seq, 폴더명)] 을 읽어 file_info 에 추가. (파일수, 바이트) 반환."""
    pid, root = plan["plan_id"], plan["root"]
    total_files = total_bytes = 0
    for seq, folder in folders:
        stack = [os.path.join(root, folder)]
        batch = []
        while stack:
            if cancel:
                cancel.check()
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
        if commit:
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


def add_folders(conn, plan, folders, progress=None, cancel=None):
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
    try:                                               # 폴더 추가와 스캔을 한 트랜잭션으로: 중지하면 추가도 취소
        n, b = _scan_folders(conn, plan, new, progress, cancel, commit=False)
    except Cancelled:
        conn.rollback()
        raise
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


def compare_dirs(src, dst, full=True, progress=None, cancel=None):
    """스테이징 폴더(src)와 미디어(dst)를 대조한다.
    full=True 면 모든 파일을 바이트 단위로 비교, False 면 존재·크기만(빠른 검사).
    숨김/시스템 파일이 미디어에 없는 것은 실패로 치지 않고 따로 보고한다(ImgBurn 이 기본 제외).
    progress(info) 를 주기적으로 호출. info: phase, file(현재 파일), files_done, files_total, bytes_done, bytes_total, percent."""
    a, b = _walk_files(src), _walk_files(dst)
    total_b = 0
    for _k, (_r, _p) in a.items():
        try:
            total_b += os.path.getsize(_p)
        except OSError:
            pass
    res = {"total": len(a), "matched": 0, "bytes": 0, "missing": [], "hidden_missing": [], "extra": [],
           "size_diff": [], "content_diff": [], "read_errors": [], "full": full}
    done_b = [0]
    done_f = 0

    def report(rel):
        if progress:
            by_bytes = full and total_b > 0
            pct = (100.0 * done_b[0] / total_b) if by_bytes else (100.0 * (done_f - 1) / len(a) if a else 100.0)
            progress({"phase": "compare", "file": rel, "files_done": done_f, "files_total": len(a),
                      "bytes_done": done_b[0], "bytes_total": total_b, "percent": min(pct, 100.0)})

    for key in sorted(a, key=lambda k: natural_key(a[k][0])):
        if cancel:
            cancel.check()
        rel, sp = a[key]
        done_f += 1
        report(rel)
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
                def tick(n, rel=rel):
                    if cancel:
                        cancel.check()
                    done_b[0] += n
                    report(rel)
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
    if res.get("copy_errors"):
        parts.append(f"디스크 읽기(복사) 실패 {len(res['copy_errors'])}")
    if res["hidden_missing"]:
        parts.append(f"숨김/시스템 파일 미포함 {len(res['hidden_missing'])}(정상 범위)")
    return ", ".join(parts)


def verify_media(conn, plan, disk_no, stage_dir, media_dir, full=True, copy_no=None, progress=None, cancel=None):
    """스테이징(원본)↔미디어 직접 비교 후 이력에 기록. 전체(바이트) 비교가 모두 일치하면 상태를 '검증됨'으로.
    copy_no 가 없으면 가장 최근 사본 기록에 결과를 붙이고, 사본 기록이 없으면 새로 만든다."""
    if not os.path.isdir(stage_dir):
        raise SystemExit(f"스테이징 폴더가 없습니다: {stage_dir}")
    if not os.path.isdir(media_dir):
        raise SystemExit(f"미디어 경로에 접근할 수 없습니다: {media_dir}")
    try:
        res = compare_dirs(stage_dir, media_dir, full, progress, cancel)
    except Cancelled:                                   # 결과·상태는 바꾸지 않고 이력만 남긴다
        log_event(conn, plan, disk_no, "미디어 비교 중지", f"사용자가 중지함 | 미디어 {media_dir}")
        raise
    return _finish_verification(conn, plan, disk_no, res, full, f"미디어 {media_dir}", copy_no, "미디어 비교")


def _finish_verification(conn, plan, disk_no, res, full, desc, copy_no=None, label="미디어 비교"):
    """비교 결과를 사본 기록·이력·디스크 상태에 반영한다."""
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
    log_event(conn, plan, disk_no, f"{label} {mode}: {verdict}", f"{summarize_compare(res)} | {desc}")
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


# ---------------------------------------------------------------- verify via copy (media -> temp -> compare)
TMP_MARKER = ".bd_planner_tmp"
TMP_DIRNAME = "_bd_verify"


def _related(a, b):
    """두 경로가 같거나 한쪽이 다른 쪽 안에 들어 있는가."""
    a, b = os.path.normcase(os.path.realpath(a)), os.path.normcase(os.path.realpath(b))
    try:
        return os.path.commonpath([a, b]) in (a, b)
    except ValueError:                  # 드라이브가 다름
        return False


def copy_media(src, dst, progress=None, cancel=None):
    """미디어(src)의 모든 파일을 dst 로 복사한다. 읽기 실패는 파일별로 모아 두고 계속 진행한다(중단하지 않음).
    progress(info): phase='copy', file, files_done, files_total, bytes_done, bytes_total, percent.
    복사 실패 [(rel, 오류)], 복사한 바이트를 반환."""
    base = long_path(src)
    files, total = [], 0
    for d, dirs, fs in os.walk(base):
        dirs[:] = [x for x in dirs if x.lower() not in _SKIP_NAMES]
        for f in fs:
            full = os.path.join(d, f)
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            files.append((os.path.relpath(full, base), full, size))
            total += size
    free = shutil.disk_usage(dst).free
    if free < total + 256 * 1024 ** 2:
        raise SystemExit(f"임시 위치의 여유 공간이 부족합니다: 필요 {total / GB:.1f} GB, 여유 {free / GB:.1f} GB")
    errors, done_b = [], 0
    files.sort(key=lambda r: natural_key(r[0]))

    def report(i, rel):
        if progress:
            progress({"phase": "copy", "file": rel, "files_done": i, "files_total": len(files),
                      "bytes_done": done_b, "bytes_total": total,
                      "percent": min(100.0 * done_b / total, 100.0) if total else 100.0 * i / len(files)})

    for i, (rel, full, size) in enumerate(files, 1):
        out = long_path(os.path.join(dst, rel))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        try:
            with open(full, "rb") as fi, open(out, "wb") as fo:
                while True:
                    if cancel:
                        cancel.check()
                    chunk = fi.read(CHUNK)
                    if not chunk:
                        break
                    fo.write(chunk)
                    done_b += len(chunk)
                    report(i, rel)
            try:
                shutil.copystat(full, out)
            except OSError:
                pass
        except Cancelled:
            try:
                os.remove(out)                        # 쓰다 만 파일을 남기지 않는다
            except OSError:
                pass
            raise
        except OSError as e:
            if getattr(e, "errno", None) == 28:       # ENOSPC
                raise SystemExit("임시 위치의 공간이 부족해 복사를 중단했습니다.")
            errors.append((rel, str(e)))
            try:
                os.remove(out)                        # 불완전한 파일이 일치로 오인되지 않게 지운다
            except OSError:
                pass
        report(i, rel)
    return errors, done_b


def verify_via_copy(conn, plan, disk_no, stage_dir, media_dir, temp_root, copy_no=None,
                    delete_when_ok=True, progress=None, cancel=None):
    """① 미디어의 파일을 temp_root 아래 임시 폴더로 복사 → ② 복사본을 스테이징과 전체(바이트) 비교 → 이력 기록.
    임시 폴더는 temp_root/_bd_verify/<계획>_diskNN 이며, 이 프로그램이 만든 표식 파일이 있는 폴더만 지운다.
    복사 중 읽기 실패한 파일이 있으면 불일치로 판정하고 복사본은 남겨 둔다.
    progress(info) — compare_dirs/copy_media 의 info 에 step('1/2 복사'·'2/2 비교'), phase_percent 를 더하고
    percent 는 복사+비교 전체 진행률(각 50%)로 바꿔 전달한다."""
    for p, what in ((stage_dir, "스테이징 폴더"), (media_dir, "미디어 경로"), (temp_root, "임시 위치")):
        if not p or not os.path.isdir(p):
            raise SystemExit(f"{what}에 접근할 수 없습니다: {p}")
    target = os.path.join(temp_root, TMP_DIRNAME, f"{plan['name']}_disk{disk_no:02d}")
    if _related(target, stage_dir) or _related(target, media_dir) or _related(temp_root, media_dir):
        raise SystemExit("임시 위치가 스테이징 폴더나 미디어와 겹칩니다. 다른 위치를 지정하세요.")
    if os.path.isdir(target):
        if not os.path.exists(os.path.join(target, TMP_MARKER)):
            raise SystemExit(f"이미 있는 폴더라 지우지 않았습니다(이 프로그램이 만든 것이 아님): {target}")
        shutil.rmtree(long_path(target))                # 이전 검증의 임시 복사본만 정리
    os.makedirs(long_path(target))
    with open(os.path.join(target, TMP_MARKER), "w", encoding="utf-8") as fh:
        fh.write(f"bd_planner temp copy of disk{disk_no:02d} {now()}\n")

    t0 = time.time()
    def stage(step, base):
        def cb(info):
            if progress:
                info = dict(info, step=step, phase_percent=info["percent"], percent=base + info["percent"] / 2)
                progress(info)
        return cb
    try:
        copy_errors, copied = copy_media(media_dir, target, stage("1/2 복사", 0.0), cancel)
        copy_secs = time.time() - t0
        res = compare_dirs(stage_dir, target, True, stage("2/2 비교", 50.0), cancel)
    except Cancelled:                                   # 임시 복사본(표식이 있는 우리 폴더)을 정리하고 이력만 남긴다
        shutil.rmtree(long_path(target), ignore_errors=True)
        log_event(conn, plan, disk_no, "미디어 비교(복사 경유) 중지", f"사용자가 중지함, 임시 복사본 삭제 | 미디어 {media_dir}")
        raise
    res["extra"] = [r for r in res["extra"] if r.lower() != TMP_MARKER]      # 표식 파일은 비교 대상이 아님
    res["copy_errors"] = copy_errors
    res["ok"] = not (res["missing"] or res["extra"] or res["size_diff"] or res["content_diff"]
                     or res["read_errors"] or copy_errors)
    res["temp_path"], res["copy_seconds"], res["copied_bytes"] = target, copy_secs, copied
    res = _finish_verification(conn, plan, disk_no, res, True,
                               f"미디어 {media_dir} → 임시 복사 {fmt(copied)} ({copy_secs / 60:.0f}분) → {target}",
                               copy_no, "미디어 비교(복사 경유)")
    if res["ok"] and delete_when_ok:
        shutil.rmtree(long_path(target), ignore_errors=True)
        res["kept"] = False
    else:
        res["kept"] = True
    return res


# ---------------------------------------------------------------- export
def export_disk(conn, plan, disk_no, stage_root, hardlink=True, out_dir=None, progress=None, cancel=None):
    """stage_root/diskNN 에 하드링크 구성 + INDEX.txt, 목록 CSV(out_dir) 생성.
    원본 파일은 변경하지 않는다. 하드링크 불가(다른 드라이브/비NTFS)면 중단한다.
    progress(info): phase='export', file, files_done, files_total, percent."""
    name = f"disk{disk_no:02d}"
    dest = os.path.join(stage_root, name)
    files = disk_files(conn, plan, disk_no)
    made = 0
    if hardlink:
        for i, (rel, _size) in enumerate(files, 1):
            if cancel and cancel.cancelled:               # 만들어 둔 하드링크는 다음 내보내기에서 이어서 쓴다
                log_event(conn, plan, disk_no, "스테이징 내보내기 중지", f"{made:,}개 만든 상태에서 중지 — 다시 실행하면 이어서 진행")
                cancel.check()
            if progress:
                progress({"phase": "export", "file": rel, "files_done": i, "files_total": len(files),
                          "bytes_done": 0, "bytes_total": 0, "percent": 100.0 * (i - 1) / len(files)})
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


def verify_disk(conn, plan, disk_no, disc_root, log=True):
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
    if log:
        log_event(conn, plan, disk_no, "경로·크기 검증(DB 기준)" + (": 일치" if ok else ": 불일치"),
                  f"누락 {len(missing)}, 추가 {len(extra)}, 크기다름 {len(diff)} | {disc_root}")
    return ok, missing, extra, diff


# ---------------------------------------------------------------- ISO (UDF 2.50) — 검증 먼저, 그다음 생성
UDF_REV_250 = 0x250
ISO_SECTOR = 2048
IMAPI_UDF = 4                       # FsiFileSystemUDF
LABEL_BAD = set(range(32)) | {127}

# Windows 내장 IMAPI2FS 로 스테이징 폴더를 UDF 이미지로 저장하는 PowerShell 스크립트.
# -ProbeOnly 는 지원하는 UDF 리비전 목록만 출력(아무것도 만들지 않음).
ISO_SCRIPT = r"""param([string]$Src, [string]$Dest, [string]$Label, [int]$Revision = 592, [switch]$ProbeOnly)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
try {
  $fsi = New-Object -ComObject IMAPI2FS.MsftFileSystemImage
  $sup = @($fsi.UDFRevisionsSupported)
  if ($ProbeOnly) { Write-Output ("SUPPORTED:" + ($sup -join ',')); exit 0 }
  if ($sup -notcontains $Revision) { Write-Output ("ERROR:UDF 리비전 $Revision 을(를) 지원하지 않습니다. 지원: " + ($sup -join ',')); exit 3 }
  $fsi.FreeMediaBlocks = 0
  $fsi.FileSystemsToCreate = 4
  $fsi.UDFRevision = $Revision
  $fsi.VolumeName = $Label
  $fsi.Root.AddTree($Src, $false)
  $res = $fsi.CreateResultImage()
  $cs = @"
using System; using System.IO; using System.Runtime.InteropServices; using System.Runtime.InteropServices.ComTypes;
public class BdpIsoWriter {
  public static void Write(string path, object stream, int blockSize, long totalBlocks) {
    IStream s = (IStream)stream; byte[] buf = new byte[blockSize * 64]; IntPtr pRead = Marshal.AllocHGlobal(4);
    try {
      using (FileStream o = new FileStream(path, FileMode.CreateNew, FileAccess.Write)) {
        long written = 0, total = totalBlocks * blockSize; int last = -1;
        while (written < total) {
          int want = (int)Math.Min((long)buf.Length, total - written);
          s.Read(buf, want, pRead); int got = Marshal.ReadInt32(pRead);
          if (got <= 0) break;
          o.Write(buf, 0, got); written += got;
          int pct = (int)(written * 100 / total);
          if (pct != last) { last = pct; Console.WriteLine("PROGRESS:" + written + "," + total); }
        }
        if (written != total) throw new Exception("이미지 스트림이 중간에 끝났습니다: " + written + "/" + total);
      }
    } finally { Marshal.FreeHGlobal(pRead); }
  }
}
"@
  Add-Type -TypeDefinition $cs
  [BdpIsoWriter]::Write($Dest, $res.ImageStream, [int]$res.BlockSize, [long]$res.TotalBlocks)
  Write-Output ("DONE:" + $res.TotalBlocks + "," + $res.BlockSize)
} catch {
  Write-Output ("ERROR:" + $_.Exception.Message)
  exit 1
}
"""


def validate_label(label):
    """UDF 볼륨 라벨 검사. 문제가 있으면 설명 문자열, 없으면 None.
    UDF 의 볼륨 식별자(dstring 32바이트)는 8비트 문자면 최대 31자, 한글처럼 16비트가 필요하면 최대 15자."""
    if not label or not label.strip():
        return "라벨이 비어 있습니다."
    if label != label.strip():
        return "라벨 앞뒤에 공백이 있습니다."
    if any(ord(c) in LABEL_BAD for c in label):
        return "라벨에 제어 문자가 있습니다."
    wide = any(ord(c) > 0xFF for c in label)
    limit = 15 if wide else 31
    if len(label) > limit:
        return f"라벨이 너무 깁니다({len(label)}자). {'한글 등 16비트 문자가 있으면' if wide else '영문·숫자만이면'} 최대 {limit}자."
    return None


def _powershell():
    return shutil.which("powershell.exe") or shutil.which("powershell")


def probe_udf_revisions():
    """IMAPI 가 지원하는 UDF 리비전 목록(정수). Windows 가 아니거나 실행할 수 없으면 None."""
    if os.name != "nt" or not _powershell():
        return None
    with tempfile.TemporaryDirectory() as td:
        script = os.path.join(td, "iso.ps1")
        with open(script, "w", encoding="utf-8-sig") as fh:
            fh.write(ISO_SCRIPT)
        try:
            out = subprocess.run([_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
                                  "-ProbeOnly"], capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None
    for line in out.stdout.splitlines():
        if line.startswith("SUPPORTED:"):
            return [int(x) for x in line[10:].split(",") if x.strip().lstrip("-").isdigit()]
    return None


def iso_preflight(conn, plan, disk_no, stage_root, iso_path, label=None, overwrite=False, probe=True):
    """ISO 를 쓰기 전에 하는 검증. {"ok", "checks": [(이름, True/False/None(건너뜀), 설명)], "label", "estimate", "can_build"}.
    아무것도 쓰지 않는다. ok 가 False 면 ISO 를 만들면 안 된다."""
    name = f"disk{disk_no:02d}"
    label = label if label is not None else name
    stage_dir = os.path.join(stage_root, name)
    checks = []

    def add(title, ok, msg):
        checks.append((title, ok, msg))

    row = conn.execute("SELECT used_bytes, file_count FROM disk WHERE plan_id=? AND disk_no=?",
                       (plan["plan_id"], disk_no)).fetchone()
    add("디스크 존재", bool(row), f"{name} 배정 있음 (파일 {row[1]:,}개)" if row else f"{name} 이(가) 배정에 없습니다.")
    add("스테이징 폴더", os.path.isdir(stage_dir), stage_dir if os.path.isdir(stage_dir) else f"없음: {stage_dir}")
    est = raw_total = 0
    if row and os.path.isdir(stage_dir):
        ok, missing, extra, diff = verify_disk(conn, plan, disk_no, stage_dir, log=False)
        add("스테이징 = 배정(경로·크기)", ok,
            "일치" if ok else f"불일치: 누락 {len(missing)}, 추가 {len(extra)}, 크기 다름 {len(diff)}")
        sp = Specs(conn, plan)
        total = sum(sz for _r, sz in disk_files(conn, plan, disk_no))
        try:
            total += os.path.getsize(os.path.join(stage_dir, "INDEX.txt"))
        except OSError:
            pass
        raw_total = total
        est = total + sp.ovh(disk_no) * (row[1] + 1)
        fits = est <= sp.raw_cap(disk_no)
        add("미디어 용량", fits, f"예상 이미지 {fmt(est)} / 미디어 {fmt(sp.raw_cap(disk_no))}"
            + ("" if fits else " — 초과"))
        longest = max((len(os.path.join(stage_dir, r)) for r, _s in disk_files(conn, plan, disk_no)), default=0)
        add("경로 길이", longest < 240, f"가장 긴 경로 {longest}자" + ("" if longest < 240 else
            " — 260자 제한에 걸려 이미지 생성이 실패할 수 있습니다."))
    err = validate_label(label)
    add("UDF 볼륨 라벨", err is None, f"'{label}'" if err is None else err)
    out_dir = os.path.dirname(os.path.abspath(iso_path))
    dest_ok = os.path.isdir(out_dir)
    add("저장 폴더", dest_ok, out_dir if dest_ok else f"없음: {out_dir}")
    if dest_ok:
        exists = os.path.exists(iso_path)
        add("저장 파일", (not exists) or overwrite,
            "새 파일" if not exists else ("기존 파일을 덮어씀(--overwrite)" if overwrite else
                                         f"이미 있음: {iso_path} (덮어쓰려면 --overwrite)"))
        free = shutil.disk_usage(out_dir).free
        add("저장 공간", free >= est + 256 * 1024 ** 2, f"여유 {fmt(free)} / 필요 약 {fmt(est)}")
        if os.path.isdir(stage_dir) and _related(iso_path, stage_dir):
            add("저장 위치", False, "ISO 를 스테이징 폴더 안에 저장할 수 없습니다.")
    can_build = False
    if probe:
        revs = probe_udf_revisions()
        if revs is None:
            add("UDF 2.50 지원(IMAPI)", None, "건너뜀 — Windows PowerShell 에서만 확인·생성할 수 있습니다.")
        else:
            can_build = UDF_REV_250 in revs
            add("UDF 2.50 지원(IMAPI)", can_build,
                "지원함" if can_build else f"지원 목록에 2.50 이 없습니다: {[hex(r) for r in revs]}")
    return {"ok": all(c[1] is not False for c in checks), "checks": checks, "label": label,
            "estimate": est, "raw_total": raw_total, "can_build": can_build, "stage_dir": stage_dir}


def _dstring(buf):
    """UDF dstring 을 문자열로(첫 바이트가 압축 ID: 8=8비트, 16=UTF-16BE, 마지막 바이트가 길이)."""
    if not buf or buf[-1] == 0:
        return ""
    n = buf[-1]
    body = buf[1:n]
    try:
        if buf[0] == 8:
            return body.decode("latin-1")
        if buf[0] == 16:
            return body.decode("utf-16-be")
    except UnicodeDecodeError:
        pass
    return ""


def inspect_iso(path):
    """ISO 파일에서 UDF 라벨·리비전·파일 시스템 종류를 직접 읽는다(2048바이트 섹터).
    {"size","udf","nsr","iso9660","pvd_label","lvd_label","revision","revision_text","errors"}"""
    info = {"size": os.path.getsize(path), "udf": False, "nsr": None, "iso9660": False, "pvd_label": None,
            "lvd_label": None, "revision": None, "revision_text": None, "revisions": {}, "errors": []}
    with open(path, "rb") as fh:
        def sector(n):
            fh.seek(n * ISO_SECTOR)
            return fh.read(ISO_SECTOR)
        for n in range(16, 32):                                # 볼륨 인식 구역
            sec = sector(n)
            ident = sec[1:6]
            if ident == b"CD001" and sec[0] == 1:
                info["iso9660"] = True
            elif ident in (b"NSR02", b"NSR03"):
                info["nsr"] = ident.decode()
            elif ident == b"TEA01":
                break
        avdp = sector(256)
        if len(avdp) < ISO_SECTOR or struct.unpack_from("<H", avdp, 0)[0] != 2:
            info["errors"].append("섹터 256 에서 앵커 볼륨 기술자(AVDP)를 찾지 못했습니다.")
            return info
        length, loc = struct.unpack_from("<II", avdp, 16)
        for i in range(max(length // ISO_SECTOR, 1)):         # 주 볼륨 기술자 시퀀스
            d = sector(loc + i)
            if len(d) < ISO_SECTOR:
                break
            tag = struct.unpack_from("<H", d, 0)[0]
            if tag == 1 and info["pvd_label"] is None:        # 기본 볼륨 기술자
                info["pvd_label"] = _dstring(d[24:56])
            elif tag == 6:                                    # 논리 볼륨 기술자
                info["lvd_label"] = _dstring(d[84:212])
                if d[217:217 + 19] == b"*OSTA UDF Compliant":
                    info["udf"] = True
                    info["revisions"]["도메인"] = struct.unpack_from("<H", d, 240)[0]
                    ilen, iloc = struct.unpack_from("<II", d, 432)        # 논리 볼륨 무결성 기술자(LVID)
                    lvid = sector(iloc) if ilen else b""
                    if len(lvid) == ISO_SECTOR and struct.unpack_from("<H", lvid, 0)[0] == 9:
                        base = 80 + 8 * struct.unpack_from("<I", lvid, 72)[0]
                        if base + 46 <= ISO_SECTOR:
                            rd, wr, mx = struct.unpack_from("<HHH", lvid, base + 40)
                            info["revisions"].update({"최소읽기": rd, "최소쓰기": wr, "최대쓰기": mx})
            elif tag == 8:                                    # 종료
                break
    if not info["udf"]:
        info["errors"].append("UDF 논리 볼륨 기술자를 찾지 못했습니다.")
    else:
        revs = info["revisions"]
        info["revision"] = max(revs.values()) if revs else None      # 가장 높은 값을 대표 리비전으로
        info["revision_text"] = " / ".join(f"{k} {v >> 8}.{v & 0xFF:02x}" for k, v in revs.items())
    return info


def info_label(path):
    info = inspect_iso(path)
    return info["lvd_label"] or info["pvd_label"]


def check_iso(path, label, revision=UDF_REV_250, min_size=0, max_size=None):
    """만들어진 ISO 를 요구사항과 대조: UDF 전용, 리비전, 라벨, 크기. [(이름, 통과, 설명)]"""
    info = inspect_iso(path)
    out = [("파일 시스템 UDF", info["udf"] and info["nsr"] in ("NSR02", "NSR03"),
            f"UDF ({info['nsr']})" if info["udf"] else "UDF 가 아닙니다. " + " ".join(info["errors"])),
           ("ISO9660 없음(UDF 전용)", not info["iso9660"], "UDF 전용" if not info["iso9660"] else "ISO9660 구조도 들어 있습니다."),
           ("UDF 리비전", revision in info["revisions"].values(),
            (info["revision_text"] + ("" if revision in info["revisions"].values() else
             f" — {revision >> 8}.{revision & 0xFF:02x} 인 필드가 없습니다")) if info["revisions"] else "읽을 수 없음")]
    got = info["lvd_label"] or info["pvd_label"] or ""
    pvd_ok = (info["pvd_label"] or "") == label[:15 if any(ord(c) > 0xFF for c in label) else 31]
    out.append(("UDF 라벨", got == label or pvd_ok, f"논리 볼륨 '{info['lvd_label']}', 기본 볼륨 '{info['pvd_label']}'"
                + ("" if (got == label or pvd_ok) else f" — 기대 '{label}'")))
    out.append(("이미지 크기", info["size"] >= min_size and (max_size is None or info["size"] <= max_size)
                and info["size"] % ISO_SECTOR == 0, f"{info['size']:,}바이트 ({info['size'] // ISO_SECTOR:,} 섹터)"))
    return out, info


def make_iso(conn, plan, disk_no, stage_root, iso_path, label=None, overwrite=False, check_only=False,
             progress=None, confirm=None, cancel=None):
    """스테이징 폴더 → UDF 2.50 ISO. 순서: ① 사전 검증 → ② (confirm 이 있으면 확인) → ③ 생성 → ④ 생성물 검증.
    사전 검증이 실패하면 아무것도 쓰지 않는다. check_only 면 ①만 하고 돌아온다.
    반환 dict: preflight, created(bool), post(생성물 검증 [(이름,통과,설명)]), ok."""
    pre = iso_preflight(conn, plan, disk_no, stage_root, iso_path, label, overwrite)
    res = {"preflight": pre, "created": False, "post": [], "ok": False, "iso": iso_path}
    log_event(conn, plan, disk_no, "ISO 사전 검증" + (": 통과" if pre["ok"] else ": 실패"),
              "; ".join(f"{t}={'OK' if o else ('건너뜀' if o is None else '실패')}" for t, o, _m in pre["checks"]))
    if check_only or not pre["ok"]:
        res["ok"] = pre["ok"] and check_only
        return res
    if os.name != "nt" or not _powershell():
        raise SystemExit("ISO 생성은 Windows(PowerShell + IMAPI2)에서만 가능합니다. 사전 검증은 통과했습니다.")
    if not pre["can_build"]:
        raise SystemExit("IMAPI 가 UDF 2.50 을 지원하지 않아 ISO 를 만들지 않았습니다.")
    if confirm and not confirm(pre):
        res["cancelled"] = True
        return res
    if cancel:
        cancel.check()
    if os.path.exists(iso_path):
        os.remove(iso_path)                             # overwrite 가 이미 검증을 통과한 경우에만 여기에 옴
    with tempfile.TemporaryDirectory() as td:
        script = os.path.join(td, "iso.ps1")
        with open(script, "w", encoding="utf-8-sig") as fh:
            fh.write(ISO_SCRIPT)
        proc = subprocess.Popen([_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
                                 "-Src", pre["stage_dir"], "-Dest", iso_path, "-Label", pre["label"],
                                 "-Revision", str(UDF_REV_250)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")
        if cancel:                                       # 중지 요청이 오면 PowerShell 을 바로 종료한다
            def watch():
                while proc.poll() is None:
                    if cancel.cancelled:
                        proc.kill()
                        return
                    time.sleep(0.3)
            threading.Thread(target=watch, daemon=True).start()
        errors = []
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("PROGRESS:") and progress:
                w, t = (int(x) for x in line[9:].split(","))
                progress({"phase": "iso", "file": os.path.basename(iso_path), "files_done": 0, "files_total": 0,
                          "bytes_done": w, "bytes_total": t, "percent": 100.0 * w / t if t else 0.0})
            elif line.startswith("ERROR:"):
                errors.append(line[6:])
        code = proc.wait()
    if cancel and cancel.cancelled:
        try:
            os.remove(iso_path)                         # 불완전한 ISO 는 지운다
        except OSError:
            pass
        log_event(conn, plan, disk_no, "ISO 생성 중지", f"사용자가 중지함, 불완전한 ISO 삭제 | {iso_path}")
        raise Cancelled("사용자가 중지했습니다.")
    if code != 0:
        try:
            os.remove(iso_path)                         # 불완전한 ISO 를 남기지 않는다
        except OSError:
            pass
        log_event(conn, plan, disk_no, "ISO 생성 실패", "; ".join(errors) or f"종료 코드 {code}")
        raise SystemExit("ISO 생성 실패: " + ("; ".join(errors) or f"종료 코드 {code}"))
    res["created"] = True
    sp = Specs(conn, plan)
    post, _info = check_iso(iso_path, pre["label"], UDF_REV_250, min_size=pre["raw_total"],
                            max_size=sp.raw_cap(disk_no))
    res["post"] = post
    res["ok"] = all(p[1] for p in post)
    log_event(conn, plan, disk_no, "ISO 생성" + (": 검증 통과" if res["ok"] else ": 생성물 검증 실패"),
              f"{iso_path} ({os.path.getsize(iso_path):,}B, 라벨 {pre['label']}, UDF 2.50)")
    return res


# ---------------------------------------------------------------- CLI
def fmt_duration(sec):
    """초 → 'mm:ss' 또는 'h:mm:ss'. None 이면 '--:--'."""
    if sec is None:
        return "--:--"
    sec = int(max(sec, 0))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class Eta:
    """진행률(0~100)로 경과 시간과 남은 시간을 추정한다. 최근 구간의 속도와 전체 평균 속도를 섞어 흔들림을 줄인다.
    update(percent) 로 갱신하고, snapshot() 은 갱신 이후 흐른 시간을 반영해 (경과, 남은) 초를 돌려준다."""

    def __init__(self, clock=time.time, window=45.0):
        self.clock, self.window = clock, window
        self.t0 = clock()
        self.samples, self._eta, self._at = [], None, None

    def update(self, percent):
        now = self.clock()
        self.samples.append((now, percent))
        while len(self.samples) > 2 and now - self.samples[1][0] > self.window:
            self.samples.pop(0)
        el = now - self.t0
        eta = None
        if percent >= 100:
            eta = 0.0
        elif percent >= 0.5 and el >= 3:
            overall = percent / el
            t1, p1 = self.samples[0]
            dt, dp = now - t1, percent - p1
            recent = dp / dt if dt >= 2 and dp > 0 else None
            rate = 0.7 * recent + 0.3 * overall if recent else overall
            eta = (100 - percent) / rate
        self._eta, self._at = eta, now
        return self.snapshot()

    def snapshot(self):
        now = self.clock()
        rem = None if self._eta is None else max(self._eta - (now - self._at), 0.0)
        return now - self.t0, rem


def cli_progress(label=""):
    """CLI 용 진행 표시기: 한 줄에 단계·진행률·건수·용량·경과·남은 시간·파일명(0.5초마다 갱신)."""
    eta, last = Eta(), [0.0]

    def cb(p):
        if time.time() - last[0] < 0.5:
            return
        last[0] = time.time()
        el, rem = eta.update(p["percent"])
        files = f"  {p['files_done']:,}/{p['files_total']:,}개" if p.get("files_total") else ""
        size = f"  {fmt(p['bytes_done'])}" if p.get("bytes_total") else ""
        line = (f"{p.get('step', label)} {p['percent']:5.1f}%{files}{size}  경과 {fmt_duration(el)}  "
                f"남은 {fmt_duration(rem)}  {short_name(p.get('file', ''), 40)}")
        print(f"\r{line:<120}", end="", file=sys.stderr)
    return cb


def scan_progress():
    """스캔처럼 전체 크기를 모르는 작업용: 건수·용량·경과 시간만 표시."""
    t0, last = time.time(), [0.0]

    def cb(files, size):
        if time.time() - last[0] < 0.5:
            return
        last[0] = time.time()
        print(f"\r스캔 {files:,}개  {fmt(size)}  경과 {fmt_duration(time.time() - t0)}", end="", file=sys.stderr)
    return cb


def short_name(name, width=60):
    """긴 경로를 가운데를 줄여 표시."""
    if len(name) <= width:
        return name
    keep = (width - 1) // 2
    return name[:keep] + "…" + name[-(width - 1 - keep):]


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
    p = sub.add_parser("make-iso", help="스테이징 폴더 → UDF 2.50 ISO. 사전 검증을 먼저 하고 통과해야 생성(Windows)")
    p.add_argument("name"); p.add_argument("disk", type=int)
    p.add_argument("--stage", required=True, help="스테이징 루트(그 아래 diskNN)")
    p.add_argument("--iso", required=True, help="만들 ISO 파일 경로")
    p.add_argument("--label", help="UDF 볼륨 라벨(기본: diskNN)")
    p.add_argument("--overwrite", action="store_true", help="같은 이름의 ISO 가 있으면 덮어씀")
    p.add_argument("--check-only", action="store_true", help="사전 검증만 하고 만들지 않음")
    p.add_argument("--yes", action="store_true", help="검증 통과 후 확인 질문 없이 바로 생성")
    p = sub.add_parser("inspect-iso", help="ISO 의 UDF 라벨·리비전·파일 시스템 종류를 읽어 표시")
    p.add_argument("file"); p.add_argument("--label"); p.add_argument("--revision", default="2.50")
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
    p.add_argument("--via-copy", metavar="TEMP_ROOT",
                   help="미디어 파일을 이 위치의 임시 폴더로 먼저 복사한 뒤, 복사본을 스테이징과 비교(항상 전체 비교)")
    p.add_argument("--keep-copy", action="store_true", help="--via-copy 일치해도 임시 복사본을 지우지 않음")
    p = sub.add_parser("verify", help="경로·크기를 DB 와 대조(내용 비교 아님)")
    p.add_argument("name"); p.add_argument("disk", type=int); p.add_argument("path")
    a = ap.parse_args(argv)
    tok = CancelToken()
    hits = [0]

    def on_sigint(_sig, _frame):
        """첫 Ctrl+C: 안전하게 중지(뒤처리 후 종료). 두 번째: 강제 종료."""
        hits[0] += 1
        if hits[0] == 1:
            tok.cancel()
            print("\n중지하는 중… (정리 후 종료됩니다. 한 번 더 누르면 강제 종료)", file=sys.stderr)
        else:
            raise KeyboardInterrupt
    signal.signal(signal.SIGINT, on_sigint)
    try:
        _dispatch(a, tok)
    except Cancelled:
        print("\n사용자가 작업을 중지했습니다.", file=sys.stderr)
        sys.exit(130)


def _dispatch(a, tok):

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
    if a.cmd == "inspect-iso":
        want = int(a.revision.replace(".", "") , 16) if a.revision else UDF_REV_250
        checks, info = check_iso(a.file, a.label if a.label is not None else (info_label(a.file) or ""), want)
        for t, ok, m in checks:
            print(f"  {'✔' if ok else '✖'} {t}: {m}")
        sys.exit(0 if all(c[1] for c in checks) else 1)
    if a.cmd == "load-settings":
        plan, notes = apply_settings(conn, read_settings(a.file), a.name, a.root, a.apply_media_types, a.force)
        print(f"계획 '{plan['name']}' 에 설정을 반영했습니다. (원본 {plan['root']})")
        for n in notes:
            print("※", n)
        print("※ 배정에 반영하려면 allocate(재배정) 또는 scan 을 실행하세요.")
        return
    plan = get_plan(conn, a.name)
    if a.cmd == "make-iso":
        def show(pre):
            print("[사전 검증]")
            for t, ok, m in pre["checks"]:
                print(f"  {'✔' if ok else ('–' if ok is None else '✖')} {t}: {m}")
        def ask(pre):
            if a.yes:
                return True
            old = signal.signal(signal.SIGINT, signal.default_int_handler)   # 입력 대기 중에는 Ctrl+C 로 바로 취소
            try:
                return input("검증을 통과했습니다. ISO 를 만들까요? [y/N] ").strip().lower() in ("y", "yes")
            except KeyboardInterrupt:
                return False
            finally:
                signal.signal(signal.SIGINT, old)
        prog = cli_progress("ISO 생성")
        res = make_iso(conn, plan, a.disk, a.stage, a.iso, a.label, a.overwrite, a.check_only, prog, ask, tok)
        show(res["preflight"])
        if not res["preflight"]["ok"]:
            print("사전 검증에 실패해 ISO 를 만들지 않았습니다.")
            sys.exit(1)
        if a.check_only:
            print("사전 검증 통과(--check-only: 만들지 않음).")
            return
        if res.get("cancelled"):
            print("취소했습니다. ISO 를 만들지 않았습니다.")
            return
        print("\n[생성물 검증]")
        for t, ok, m in res["post"]:
            print(f"  {'✔' if ok else '✖'} {t}: {m}")
        print(("완료: " if res["ok"] else "검증 실패: ") + a.iso)
        sys.exit(0 if res["ok"] else 1)
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
        nf, n, b = add_folders(conn, plan, folders, scan_progress(), tok)
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
        n, b = scan(conn, plan, scan_progress(), tok)
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
            dest, csvp, made = export_disk(conn, plan, n, a.stage, not a.no_hardlink,
                                           progress=cli_progress(f"disk{n:02d} 내보내기"), cancel=tok)
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
        prog = cli_progress("비교")
        if a.via_copy:
            res = verify_via_copy(conn, plan, a.disk, stage_dir, a.media, a.via_copy, a.copy,
                                  not a.keep_copy, prog, tok)
        else:
            res = verify_media(conn, plan, a.disk, stage_dir, a.media, not a.quick, a.copy, prog, tok)
        print(f"\n{res['verdict']}: {summarize_compare(res)}")
        for r, e in res.get("copy_errors", [])[:30]:
            print(f"  디스크 읽기 실패: {r} ({e})")
        if a.via_copy:
            print(f"  임시 복사본: {res['temp_path']} ({'보존' if res['kept'] else '삭제함'}), 복사 {res['copy_seconds'] / 60:.1f}분")
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
