#!/usr/bin/env python3
"""BD-Planner GUI (tkinter). bd_planner.py 와 같은 폴더에 두고 실행한다."""
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bd_planner as core  # noqa: E402

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bd_planner.db")
CHECK, UNCHECK = "☑", "☐"
NO_MEDIA = "(없음: 위 숫자 사용)"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("BD-Planner · 블루레이 백업 배정")
        self.geometry("1180x720")
        self.minsize(900, 560)
        self.conn = core.connect(DB_PATH)
        self.q = queue.Queue()
        self.busy = False
        self.plan = None
        self._build()
        self.after(100, self._poll)

    # ------------------------------------------------------------ UI
    def _build(self):
        pad = {"padx": 6, "pady": 4}
        top = ttk.LabelFrame(self, text="① 설정")
        top.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(top, text="원본 폴더").grid(row=0, column=0, **pad)
        self.root_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.root_var, width=34).grid(row=0, column=1, **pad)
        ttk.Button(top, text="찾아보기…", command=self.browse_root).grid(row=0, column=2, **pad)
        ttk.Label(top, text="계획 이름").grid(row=0, column=3, **pad)
        self.name_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.name_var, width=16).grid(row=0, column=4, **pad)
        ttk.Label(top, text="디스크 용량").grid(row=0, column=5, **pad)
        self.cap_var = tk.StringVar(value="90")
        ttk.Entry(top, textvariable=self.cap_var, width=6).grid(row=0, column=6, **pad)
        self.unit_var = tk.StringVar(value="GB(십진)")
        ttk.Combobox(top, textvariable=self.unit_var, width=9, state="readonly",
                     values=["GB(십진)", "GiB(이진)"]).grid(row=0, column=7, **pad)
        ttk.Label(top, text="파일당 오버헤드(바이트)").grid(row=1, column=5, columnspan=2, sticky="e", **pad)
        self.ovh_var = tk.StringVar(value="4096")
        ttk.Entry(top, textvariable=self.ovh_var, width=8).grid(row=1, column=7, **pad)
        ttk.Label(top, text="※ 파일 1개마다 용량에 더해 계산(UDF 메타데이터·섹터 올림 보정). 0이면 사용 안 함.",
                  foreground="#555").grid(row=1, column=0, columnspan=5, sticky="w", **pad)
        ttk.Button(top, text="폴더 목록 읽기", command=self.load_folders).grid(row=0, column=8, **pad)
        ttk.Label(top, text="기본 미디어").grid(row=3, column=0, **pad)
        self.defmedia_var = tk.StringVar(value=NO_MEDIA)
        self.defmedia_cb = ttk.Combobox(top, textvariable=self.defmedia_var, width=30, state="readonly")
        self.defmedia_cb.grid(row=3, column=1, columnspan=4, sticky="we", **pad)
        ttk.Button(top, text="미디어 종류 설정…", command=self.edit_media_types).grid(row=3, column=5, columnspan=2, **pad)
        ttk.Label(top, text="※ 지정 없는 디스크는 이 미디어(100G/50G/25G)의 용량·오버헤드를 씁니다. '(없음)'이면 위 숫자(디스크 용량·오버헤드)를 사용.",
                  foreground="#555").grid(row=4, column=0, columnspan=9, sticky="w", **pad)
        ttk.Label(top, text="디스크별 용량").grid(row=2, column=0, **pad)
        self.caps_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.caps_var, width=34).grid(row=2, column=1, columnspan=4, sticky="we", **pad)
        ttk.Label(top, text="※ disk01부터 쉼표로 구분(위 단위). 예: 95, 23.3, , 45  → 비운 칸·나머지는 기본 용량",
                  foreground="#555").grid(row=2, column=5, columnspan=4, sticky="w", **pad)

        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=8, pady=4)

        left = ttk.LabelFrame(body, text="② 백업 대상 폴더  (☑ 클릭으로 선택, 위/아래로 순서 지정)")
        body.add(left, weight=1)
        self.ftree = ttk.Treeview(left, columns=("chk", "no", "name", "cnt", "size"),
                                  show="headings", selectmode="browse", height=16)
        for c, t, w, a in (("chk", "", 34, "center"), ("no", "순서", 44, "center"), ("name", "폴더", 200, "w"),
                           ("cnt", "파일수", 70, "e"), ("size", "용량", 90, "e")):
            self.ftree.heading(c, text=t)
            self.ftree.column(c, width=w, anchor=a, stretch=(c == "name"))
        self.ftree.pack(fill="both", expand=True, padx=4, pady=4)
        self.ftree.bind("<Button-1>", self.on_folder_click)
        fb = ttk.Frame(left)
        fb.pack(fill="x", padx=4, pady=(0, 4))
        for t, cmd in (("▲ 위로", lambda: self.move(-1)), ("▼ 아래로", lambda: self.move(1)),
                       ("전체 선택", lambda: self.set_all(True)), ("선택 해제", lambda: self.set_all(False))):
            ttk.Button(fb, text=t, command=cmd).pack(side="left", padx=2)
        self.sum_var = tk.StringVar(value="폴더 목록을 읽어 주세요.")
        ttk.Label(left, textvariable=self.sum_var, foreground="#555").pack(anchor="w", padx=6, pady=(0, 4))
        self.scan_btn = ttk.Button(left, text="③ 스캔 + 배정 계산", command=self.run_scan)
        self.scan_btn.pack(fill="x", padx=6, pady=(0, 2))
        self.add_btn = ttk.Button(left, text="③' 새 폴더만 추가 배정 (구운 디스크 유지)", command=self.run_add)
        self.add_btn.pack(fill="x", padx=6, pady=(0, 6))

        right = ttk.LabelFrame(body, text="④ 디스크 배정 결과")
        body.add(right, weight=2)
        self.dtree = ttk.Treeview(right, columns=("disk", "st", "media", "cap", "used", "pct", "cnt", "copy", "ver", "folders"),
                                  show="headings", selectmode="extended", height=12)
        for c, t, w, a in (("disk", "디스크", 70, "w"), ("st", "상태", 70, "w"), ("media", "미디어", 100, "w"),
                           ("cap", "용량(GB)", 80, "e"),
                           ("used", "사용(GB)", 80, "e"),
                           ("pct", "채움", 55, "e"), ("cnt", "파일수", 70, "e"), ("copy", "사본", 45, "e"),
                           ("ver", "최근 검증", 120, "w"), ("folders", "포함 폴더", 300, "w")):
            self.dtree.heading(c, text=t)
            self.dtree.column(c, width=w, anchor=a, stretch=(c == "folders"))
        self.dtree.tag_configure("검증됨", foreground="#2e9d68")
        self.dtree.tag_configure("굽는 중", foreground="#2f6fdb")
        self.dtree.tag_configure("완료", foreground="#7a4fc9")
        self.dtree.pack(fill="both", expand=True, padx=4, pady=4)
        self.dtree.bind("<<TreeviewSelect>>", self.on_disk_select)

        act = ttk.Frame(right)
        act.pack(fill="x", padx=4)
        ttk.Label(act, text="선택 디스크 상태:").pack(side="left")
        self.st_var = tk.StringVar(value=core.STATUSES[0])
        ttk.Combobox(act, textvariable=self.st_var, values=core.STATUSES, width=8,
                     state="readonly").pack(side="left", padx=4)
        ttk.Button(act, text="변경", command=self.change_status).pack(side="left")
        ttk.Button(act, text="경로·크기 검증(DB)…", command=self.run_verify).pack(side="left", padx=12)

        cap = ttk.Frame(right)
        cap.pack(fill="x", padx=4, pady=(4, 0))
        ttk.Label(cap, text="선택 디스크 용량:").pack(side="left")
        self.cap_one_var = tk.StringVar()
        ttk.Entry(cap, textvariable=self.cap_one_var, width=8).pack(side="left", padx=4)
        ttk.Label(cap, text="(위 단위)").pack(side="left")
        ttk.Button(cap, text="적용 후 재배정", command=self.apply_disk_capacity).pack(side="left", padx=6)
        ttk.Button(cap, text="기본 용량으로", command=lambda: self.apply_disk_capacity(reset=True)).pack(side="left")
        ttk.Button(cap, text="재배정", command=self.run_realloc).pack(side="left", padx=12)

        md = ttk.Frame(right)
        md.pack(fill="x", padx=4, pady=(4, 0))
        ttk.Label(md, text="선택 디스크 미디어:").pack(side="left")
        self.media_var = tk.StringVar()
        self.media_cb = ttk.Combobox(md, textvariable=self.media_var, width=16, state="readonly")
        self.media_cb.pack(side="left", padx=4)
        ttk.Button(md, text="적용 후 재배정", command=self.apply_media).pack(side="left")
        ttk.Button(md, text="선택 번호~끝까지", command=lambda: self.apply_media(to_end=True)).pack(side="left", padx=4)
        ttk.Button(md, text="해제", command=lambda: self.apply_media(clear=True)).pack(side="left")
        self.media_info = tk.StringVar()
        ttk.Label(md, textvariable=self.media_info, foreground="#555").pack(side="left", padx=8)

        hv = ttk.Frame(right)
        hv.pack(fill="x", padx=4, pady=(4, 0))
        ttk.Label(hv, text="이력·검증:").pack(side="left")
        ttk.Button(hv, text="백업 완료 기록…", command=self.record_burn).pack(side="left", padx=6)
        ttk.Button(hv, text="스테이징↔미디어 비교…", command=self.run_media_compare).pack(side="left")
        ttk.Button(hv, text="이력 보기…", command=self.show_history).pack(side="left", padx=6)

        out = ttk.LabelFrame(right, text="⑤ 출력 (하드링크 스테이징 + INDEX.txt + 목록 CSV)")
        out.pack(fill="x", padx=4, pady=4)
        ttk.Label(out, text="스테이징 위치").grid(row=0, column=0, padx=6, pady=4)
        self.stage_var = tk.StringVar()
        ttk.Entry(out, textvariable=self.stage_var, width=26).grid(row=0, column=1, padx=4)
        ttk.Button(out, text="찾아보기…", command=self.browse_stage).grid(row=0, column=2, padx=4)
        self.export_btn = ttk.Button(out, text="선택 디스크 내보내기", command=self.run_export)
        self.export_btn.grid(row=0, column=3, padx=8)
        ttk.Label(out, text="※ 스테이징은 원본과 같은 NTFS 드라이브여야 합니다(하드링크, 용량 미사용).",
                  foreground="#555").grid(row=1, column=0, columnspan=4, sticky="w", padx=6, pady=(0, 4))

        self.refresh_media_widgets()
        self.log = tk.Text(right, height=9, wrap="word")
        self.log.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self.status = tk.StringVar(value="준비")
        ttk.Label(self, textvariable=self.status, relief="sunken", anchor="w").pack(fill="x", side="bottom")

    # ------------------------------------------------------------ helpers
    def say(self, msg):
        self.log.insert("end", msg + "\n")
        self.log.see("end")

    def browse_root(self):
        d = filedialog.askdirectory(title="원본 폴더 선택", initialdir=self.root_var.get() or None)
        if d:
            self.root_var.set(os.path.normpath(d))
            if not self.name_var.get():
                self.name_var.set(os.path.basename(os.path.normpath(d)) or "plan")

    def browse_stage(self):
        d = filedialog.askdirectory(title="스테이징 폴더 선택")
        if d:
            self.stage_var.set(os.path.normpath(d))

    def capacity(self):
        try:
            v = float(self.cap_var.get())
        except ValueError:
            raise ValueError("디스크 용량은 숫자여야 합니다.")
        return int(v * (core.GIB if self.unit_var.get().startswith("GiB") else core.GB))

    def unit_bytes(self):
        return core.GIB if self.unit_var.get().startswith("GiB") else core.GB

    def parse_caps(self):
        """'95, 23.3, , 45' → [bytes, bytes, None, bytes]"""
        out = []
        for tok in self.caps_var.get().replace("，", ",").split(","):
            tok = tok.strip()
            if not tok:
                out.append(None)
                continue
            try:
                v = float(tok)
            except ValueError:
                raise ValueError(f"디스크별 용량에 숫자가 아닌 값이 있습니다: '{tok}'")
            if v <= 0:
                raise ValueError("디스크별 용량은 0보다 커야 합니다.")
            out.append(int(v * self.unit_bytes()))
        while out and out[-1] is None:
            out.pop()
        return out

    def sync_caps_entry(self):
        """DB 의 디스크별 용량 지정을 입력칸에 반영."""
        if not self.plan:
            return
        caps = core.get_capacities(self.conn, self.plan)
        if not caps:
            self.caps_var.set("")
            return
        u = self.unit_bytes()
        vals = []
        for n in range(1, max(caps) + 1):
            vals.append(f"{caps[n] / u:.6g}" if n in caps else "")
        self.caps_var.set(", ".join(vals))

    def overhead(self):
        try:
            v = int(float(self.ovh_var.get() or 0))
        except ValueError:
            raise ValueError("파일당 오버헤드는 숫자(바이트)여야 합니다.")
        if v < 0:
            raise ValueError("파일당 오버헤드는 0 이상이어야 합니다.")
        return v

    def ensure_plan(self):
        name, root = self.name_var.get().strip(), self.root_var.get().strip()
        if not name or not root:
            raise ValueError("원본 폴더와 계획 이름을 입력하세요.")
        if not os.path.isdir(root):
            raise ValueError(f"원본 폴더에 접근할 수 없습니다:\n{root}")
        cap, ovh = self.capacity(), self.overhead()
        if cap <= 0:
            raise ValueError("디스크 용량이 올바르지 않습니다.")
        row = self.conn.execute("SELECT plan_id, root_path FROM plan WHERE name=?", (name,)).fetchone()
        if not row:
            core.create_plan(self.conn, name, root, cap, ovh)
        else:
            if os.path.normcase(row[1]) != os.path.normcase(root):
                if not messagebox.askyesno("원본 폴더 변경",
                                           "이 계획의 기존 원본 폴더와 다릅니다.\n"
                                           "원본 폴더를 바꾸면 이전 스캔·배정 결과가 삭제됩니다. 계속할까요?"):
                    raise ValueError("취소됨")
                for t in ("selected_folder", "file_info", "assignment", "disk", "warning"):
                    self.conn.execute(f"DELETE FROM {t} WHERE plan_id=?", (row[0],))
            self.conn.execute("UPDATE plan SET root_path=?, disk_capacity_bytes=?, overhead_per_file=? "
                              "WHERE plan_id=?", (root, cap, ovh, row[0]))
            self.conn.commit()
        self.plan = core.get_plan(self.conn, name)
        return self.plan

    # ------------------------------------------------------------ folder list
    def load_folders(self):
        try:
            plan = self.ensure_plan()
            subs = core.list_subfolders(plan["root"])
        except ValueError as e:
            if str(e) != "취소됨":
                messagebox.showerror("오류", str(e))
            return
        except OSError as e:
            messagebox.showerror("오류", f"폴더를 읽을 수 없습니다:\n{e}")
            return
        saved = [r[0] for r in self.conn.execute(
            "SELECT folder_path FROM selected_folder WHERE plan_id=? ORDER BY seq", (plan["plan_id"],))]
        stats = {r[0]: r[1:] for r in self.conn.execute(
            "SELECT sf.folder_path, COUNT(f.file_id), COALESCE(SUM(f.size_bytes),0) FROM selected_folder sf "
            "LEFT JOIN file_info f ON f.plan_id=sf.plan_id AND f.folder_seq=sf.seq "
            "WHERE sf.plan_id=? GROUP BY sf.seq", (plan["plan_id"],))}
        self.ftree.delete(*self.ftree.get_children())
        scanned = bool(self.conn.execute("SELECT 1 FROM file_info WHERE plan_id=? LIMIT 1",
                                         (plan["plan_id"],)).fetchone())
        for f in [s for s in saved if s in subs] + [s for s in subs if s not in saved]:
            cnt, size = stats.get(f, (None, None))
            self.ftree.insert("", "end", values=(CHECK if f in saved else UNCHECK, "", f,
                                                 f"{cnt:,}" if cnt else ("신규" if scanned and f not in saved else ""),
                                                 core.fmt(size) if size else ""))
        self.renumber()
        self.sync_caps_entry()
        self.sync_default_media()
        self.refresh_disks()
        self.say(f"원본: {plan['root']}  (하부 폴더 {len(subs)}개)")

    def on_folder_click(self, ev):
        if self.ftree.identify_region(ev.x, ev.y) != "cell":
            return
        if self.ftree.identify_column(ev.x) == "#1":
            iid = self.ftree.identify_row(ev.y)
            v = list(self.ftree.item(iid, "values"))
            v[0] = UNCHECK if v[0] == CHECK else CHECK
            self.ftree.item(iid, values=v)
            self.scan_sum = None
            self.renumber()
            return "break"

    def set_all(self, on):
        self.scan_sum = None
        for iid in self.ftree.get_children():
            v = list(self.ftree.item(iid, "values"))
            v[0] = CHECK if on else UNCHECK
            self.ftree.item(iid, values=v)
        self.renumber()

    def move(self, d):
        sel = self.ftree.selection()
        if not sel:
            return
        iid = sel[0]
        idx = self.ftree.index(iid) + d
        if 0 <= idx < len(self.ftree.get_children()):
            self.ftree.move(iid, "", idx)
            self.renumber()

    def checked(self):
        return [self.ftree.item(i, "values")[2] for i in self.ftree.get_children()
                if self.ftree.item(i, "values")[0] == CHECK]

    def renumber(self):
        n = 0
        for iid in self.ftree.get_children():
            v = list(self.ftree.item(iid, "values"))
            if v[0] == CHECK:
                n += 1
                v[1] = n
            else:
                v[1] = ""
            self.ftree.item(iid, values=v)
        self.sum_var.set(getattr(self, "scan_sum", None) or f"선택 {n}개 폴더 (스캔 후 용량·예상 디스크 수가 표시됩니다)")

    # ------------------------------------------------------------ background jobs
    def run_bg(self, fn, done, label):
        if self.busy:
            messagebox.showinfo("작업 중", "진행 중인 작업이 끝난 뒤에 다시 시도하세요.")
            return
        self.busy = True
        self.status.set(label)

        def work():
            conn = core.connect(DB_PATH)
            try:
                res = fn(conn)
                self.q.put(("ok", done, res))
            except SystemExit as e:
                self.q.put(("err", done, str(e)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("err", done, f"{type(e).__name__}: {e}"))
            finally:
                conn.close()
        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, done, res = self.q.get_nowait()
                if kind == "prog":
                    self.status.set(res)
                    continue
                self.busy = False
                self.status.set("준비")
                if kind == "err":
                    self.say(f"[오류] {res}")
                    messagebox.showerror("오류", str(res))
                else:
                    done(res)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    # ------------------------------------------------------------ scan / allocate
    def run_scan(self):
        names = self.checked()
        if not names:
            messagebox.showwarning("선택 없음", "백업할 폴더를 하나 이상 선택하세요.")
            return
        try:
            plan = self.ensure_plan()
        except ValueError as e:
            if str(e) != "취소됨":
                messagebox.showerror("오류", str(e))
            return
        locked = core.locked_disks(self.conn, plan)
        if locked and not messagebox.askyesno(
                "전체 재스캔 경고",
                "이미 구운(또는 굽는 중) 디스크가 있습니다: " + ", ".join(f"disk{n:02d}" for n in locked) +
                "\n\n전체 재스캔은 모든 배정과 디스크 상태를 초기화하며, 구운 디스크와 구성이 달라질 수 있습니다.\n"
                "새 폴더만 추가하려면 '새 폴더만 추가 배정'을 쓰세요.\n\n그래도 전체 재스캔할까요?",
                default="no", icon="warning"):
            return
        core.set_folders(self.conn, plan, names)
        try:
            core.set_capacities(self.conn, plan, self.parse_caps())   # 입력칸 = 디스크별 용량(스캔 시 저장)
            self.save_default_media()
        except ValueError as e:
            messagebox.showerror("오류", str(e))
            return

        def fn(conn):
            p = core.get_plan(conn, plan["name"])
            n, b = core.scan(conn, p, lambda f, s: self.q.put(("prog", None, f"스캔 중… {f:,}개 / {core.fmt(s)}")))
            disks = core.allocate(conn, p)
            return n, b, len(disks)

        def done(res):
            n, b, nd = res
            dec, bi = core.estimate_disks(b, plan["capacity"])
            self.say(f"스캔 완료: 파일 {n:,}개, {core.fmt(b)}")
            self.say(f"예상 디스크 수(기본 용량 기준): 십진 {dec}장 / 이진 {bi}장 → 디스크별 용량 반영 실제 배정 {nd}장")
            self.scan_sum = f"선택 {len(names)}개 · {n:,}개 파일 · {core.fmt(b)} · 배정 {nd}장"
            self.sum_var.set(self.scan_sum)
            self.load_folders()
            over = self.conn.execute("SELECT message FROM warning WHERE plan_id=? AND kind IN "
                                     "('oversize','unassigned','access','emptydisk','changed')", (plan["plan_id"],)).fetchall()
            for (m,) in over:
                self.say(f"[경고] {m}")
            if over:
                messagebox.showwarning("경고", f"경고 {len(over)}건이 있습니다. 아래 로그를 확인하세요.\n"
                                       "(용량 초과 파일은 배정에서 제외됩니다.)")
        self.run_bg(fn, done, "스캔 중…")

    def run_add(self):
        """체크된 폴더 중 아직 계획에 없는 '새 폴더'만 스캔해 남는 공간·새 디스크에 배정."""
        try:
            plan = self.ensure_plan()
        except ValueError as e:
            if str(e) != "취소됨":
                messagebox.showerror("오류", str(e))
            return
        if not self.conn.execute("SELECT 1 FROM disk WHERE plan_id=? LIMIT 1", (plan["plan_id"],)).fetchone():
            messagebox.showinfo("확인", "아직 배정 결과가 없습니다. 처음에는 '스캔 + 배정 계산'을 사용하세요.")
            return
        have = {r[0] for r in self.conn.execute("SELECT folder_path FROM selected_folder WHERE plan_id=?",
                                                (plan["plan_id"],))}
        new = [f for f in self.checked() if f not in have]
        if not new:
            messagebox.showinfo("새 폴더 없음", "목록에서 '신규' 폴더를 ☑ 체크한 뒤 눌러 주세요.\n"
                                "(먼저 [폴더 목록 읽기]로 원본의 새 폴더를 불러옵니다.)")
            return
        try:
            core.set_capacities(self.conn, plan, self.parse_caps())
            self.save_default_media()
        except ValueError as e:
            messagebox.showerror("오류", str(e))
            return

        def fn(conn):
            p = core.get_plan(conn, plan["name"])
            nf, n, b = core.add_folders(conn, p, new,
                                        lambda f, s: self.q.put(("prog", None, f"새 폴더 스캔 중… {f:,}개 / {core.fmt(s)}")))
            placed, touched = core.allocate_new(conn, p)
            return nf, n, b, placed, touched

        def done(res):
            nf, n, b, placed, touched = res
            self.say(f"새 폴더 {nf}개 · 파일 {n:,}개 · {core.fmt(b)} → {placed:,}개 배정")
            self.say("내용이 바뀐 디스크: " + (", ".join(f"disk{t:02d}" for t in touched) or "없음") +
                     "  ← 이 디스크만 다시 내보내기(export). 구운 디스크는 변경 없음.")
            for (m,) in self.conn.execute("SELECT message FROM warning WHERE plan_id=? AND kind IN "
                                          "('oversize','unassigned','emptydisk','access')", (plan["plan_id"],)):
                self.say(f"[경고] {m}")
            self.load_folders()
            self.dtree.selection_set([str(t) for t in touched if self.dtree.exists(str(t))])
        self.run_bg(fn, done, "새 폴더 추가 중…")

    # ------------------------------------------------------------ disks view
    def refresh_disks(self):
        keep = self.dtree.selection()
        self.dtree.delete(*self.dtree.get_children())
        self._restore_sel = keep
        if not self.plan:
            return
        plan = self.plan
        split = dict(core.split_folders(self.conn, plan))
        sp = core.Specs(self.conn, plan)
        recs = {}
        for r in core.burn_records(self.conn, plan):
            recs.setdefault(r[1], []).append(r)
        for no, name, used, cnt, st in self.conn.execute(
                "SELECT disk_no, disk_name, used_bytes, file_count, status FROM disk "
                "WHERE plan_id=? ORDER BY disk_no", (plan["plan_id"],)).fetchall():
            parts = []
            for folder, c, size in core.disk_folders(self.conn, plan, no):
                parts.append(f"{folder} {size / core.GB:.1f}G" + ("◀▶" if folder in split else ""))
            cap = sp.cap(no)
            self.dtree.insert("", "end", iid=str(no), tags=(st,), values=(
                name, st, sp.media_label(no), f"{cap / core.GB:.2f}" + ("*" if no in sp.custom else ""),
                f"{used / core.GB:.2f}", f"{used * 100 / cap:.0f}%", f"{cnt:,}",
                len(recs.get(no, [])) or "", self.last_verify(recs.get(no, [])),
                " · ".join(parts)))
        again = [i for i in self._restore_sel if self.dtree.exists(i)]
        if again:
            self.dtree.selection_set(again)

    def on_disk_select(self, _ev):
        sel = self.selected_disks()
        if len(sel) == 1 and self.plan:
            sp = core.Specs(self.conn, self.plan)
            self.cap_one_var.set(f"{sp.cap(sel[0]) / self.unit_bytes():.6g}")
            self.media_var.set(sp.media_label(sel[0]))
            self.media_info.set(f"사용 가능 {core.fmt(sp.cap(sel[0]))} · 파일당 오버헤드 {sp.ovh(sel[0]):,}B")

    # ------------------------------------------------------------ media types
    def media_pairs(self):
        return [(k, t["label"]) for k, t in core.media_types(self.conn).items()]

    def key_of_label(self, label):
        return next((k for k, lb in self.media_pairs() if lb == label), None)

    def refresh_media_widgets(self):
        labels = [lb for _k, lb in self.media_pairs()]
        self.defmedia_cb.configure(values=[NO_MEDIA] + labels)
        self.media_cb.configure(values=labels)
        if self.defmedia_var.get() not in [NO_MEDIA] + labels:
            self.defmedia_var.set(NO_MEDIA)

    def sync_default_media(self):
        """DB 의 기본 미디어를 콤보박스에 반영."""
        if not self.plan:
            return
        k = core.get_setting(self.conn, self.plan, "default_media")
        types = core.media_types(self.conn)
        self.defmedia_var.set(types[k]["label"] if k in types else NO_MEDIA)

    def save_default_media(self):
        core.set_default_media(self.conn, self.plan, self.key_of_label(self.defmedia_var.get()))

    def apply_media(self, to_end=False, clear=False):
        """선택 디스크(들)에 미디어 종류를 지정하고, 그 종류의 용량·오버헤드로 재배정."""
        nos = self.selected_disks()
        if not nos or not self.plan:
            messagebox.showinfo("선택 없음", "미디어를 지정할 디스크를 선택하세요.")
            return
        key = None
        if not clear:
            key = self.key_of_label(self.media_var.get())
            if not key:
                messagebox.showinfo("확인", "미디어 종류(100G/50G/25G)를 고르세요.")
                return
        if to_end:
            nos = list(range(min(nos), max(int(i) for i in self.dtree.get_children()) + 1))
        core.assign_media(self.conn, self.plan, nos, key)
        self.sync_caps_entry()
        self.say(("미디어 지정 해제: " if clear else f"미디어 {self.media_var.get()} 지정: ") +
                 ", ".join(f"disk{n:02d}" for n in nos))
        self.run_realloc(changed=min(nos))

    def edit_media_types(self):
        win = tk.Toplevel(self)
        win.title("미디어 종류 설정 (모든 계획 공통)")
        win.transient(self)
        win.grab_set()
        tv = ttk.Treeview(win, show="headings", height=4, selectmode="browse",
                          columns=("key", "label", "cap", "pf", "fx"))
        for c, t, w, a in (("key", "키", 60, "w"), ("label", "이름", 140, "w"), ("cap", "기록 가능 용량(B)", 150, "e"),
                           ("pf", "파일당 오버헤드(B)", 130, "e"), ("fx", "디스크당 고정(MB)", 120, "e")):
            tv.heading(c, text=t)
            tv.column(c, width=w, anchor=a)
        tv.pack(fill="x", padx=8, pady=8)

        def fill():
            tv.delete(*tv.get_children())
            for k, t in core.media_types(self.conn).items():
                tv.insert("", "end", iid=k, values=(k, t["label"], f"{t['capacity']:,}", f"{t['per_file']:,}",
                                                    f"{t['fixed'] / 1_000_000:.1f}"))
        fill()

        def edit():
            sel = tv.selection()
            if not sel:
                messagebox.showinfo("선택 없음", "수정할 종류를 선택하세요.", parent=win)
                return
            k = sel[0]
            t = core.media_types(self.conn)[k]
            f = self.form(f"{k} 수정", [("label", "이름"), ("cap", "기록 가능 용량(바이트)"),
                                        ("pf", "파일당 오버헤드(바이트)"), ("fx", "디스크당 고정 오버헤드(MB)")],
                          {"label": t["label"], "cap": str(t["capacity"]), "pf": str(t["per_file"]),
                           "fx": f"{t['fixed'] / 1_000_000:.6g}"})
            if not f:
                return
            try:
                core.set_media_type(self.conn, k, capacity=int(f["cap"].replace(",", "")),
                                    per_file=int(f["pf"].replace(",", "")),
                                    fixed=int(float(f["fx"]) * 1_000_000), label=f["label"] or None)
            except (ValueError, SystemExit) as e:
                messagebox.showerror("오류", str(e) or "숫자를 확인하세요.", parent=win)
                return
            fill()
            self.refresh_media_widgets()
            self.refresh_disks()

        def reset():
            if messagebox.askyesno("기본값 복원", "100G/50G/25G 설정을 기본값으로 되돌릴까요?", parent=win):
                core.reset_media_types(self.conn)
                fill()
                self.refresh_media_widgets()
                self.refresh_disks()
        bf = ttk.Frame(win)
        bf.pack(pady=(0, 6))
        ttk.Button(bf, text="수정…", command=edit).pack(side="left", padx=6)
        ttk.Button(bf, text="기본값 복원", command=reset).pack(side="left", padx=6)
        ttk.Button(bf, text="닫기", command=win.destroy).pack(side="left", padx=6)
        ttk.Label(win, foreground="#555", wraplength=520, text=(
            "※ 기록 가능 용량은 드라이브가 보고하는 Free Space(바이트)입니다. 오버헤드는 UDF 메타데이터·섹터 올림 보정용으로 "
            "파일마다 더하는 값(파일당)과 디스크마다 빼 두는 값(고정)이 있습니다. 바꾼 값은 [재배정] 후 반영됩니다.")
        ).pack(padx=8, pady=(0, 8))

    # ------------------------------------------------------------ per-disk capacity
    def apply_disk_capacity(self, reset=False):
        nos = self.selected_disks()
        if not nos or not self.plan:
            messagebox.showinfo("선택 없음", "용량을 바꿀 디스크를 선택하세요.")
            return
        try:
            val = None if reset else float(self.cap_one_var.get())
            if val is not None and val <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("오류", "용량은 0보다 큰 숫자여야 합니다.")
            return
        for n in nos:
            core.set_capacity(self.conn, self.plan, n, None if val is None else int(val * self.unit_bytes()))
        self.sync_caps_entry()
        self.run_realloc(changed=min(nos))

    def run_realloc(self, changed=None):
        """스캔 없이 현재 용량 설정으로 다시 배정."""
        if not self.plan:
            messagebox.showinfo("확인", "먼저 스캔 + 배정 계산을 실행하세요.")
            return
        plan = self.plan
        if not self.conn.execute("SELECT 1 FROM file_info WHERE plan_id=? LIMIT 1", (plan["plan_id"],)).fetchone():
            messagebox.showinfo("확인", "스캔 결과가 없습니다. 먼저 스캔 + 배정 계산을 실행하세요.")
            return
        try:
            core.set_capacities(self.conn, plan, self.parse_caps())
            self.save_default_media()
        except ValueError as e:
            messagebox.showerror("오류", str(e))
            return
        locked = [n for n in core.locked_disks(self.conn, plan) if changed is None or n >= changed]
        if locked and not messagebox.askyesno(
                "이미 구운 디스크",
                "다음 디스크는 '계획됨'이 아닙니다: " + ", ".join(f"disk{n:02d}" for n in locked) +
                "\n용량 변경으로 구성이 바뀌면 해당 디스크 상태가 '계획됨'으로 초기화됩니다.\n"
                "(구성이 그대로인 디스크는 상태가 유지됩니다.) 계속할까요?"):
            return

        def fn(conn):
            p = core.get_plan(conn, plan["name"])
            return len(core.allocate(conn, p))

        def done(nd):
            self.say(f"재배정 완료: {nd}장")
            for (m,) in self.conn.execute("SELECT message FROM warning WHERE plan_id=? AND kind IN "
                                          "('unassigned','emptydisk','changed')", (plan["plan_id"],)):
                self.say(f"[경고] {m}")
            self.refresh_disks()
        self.run_bg(fn, done, "재배정 중…")

    def selected_disks(self):
        return [int(i) for i in self.dtree.selection()]

    def change_status(self):
        nos = self.selected_disks()
        if not nos:
            messagebox.showinfo("선택 없음", "디스크를 선택하세요.")
            return
        for n in nos:
            core.set_status(self.conn, self.plan, n, self.st_var.get())
        self.refresh_disks()

    # ------------------------------------------------------------ history / media compare
    @staticmethod
    def last_verify(rows):
        """사본 기록 중 가장 최근 검증 결과를 짧게 표시."""
        done = [r for r in rows if r[6]]
        if not done:
            return ""
        r = max(done, key=lambda x: x[6])
        return ("✔ " if r[7] in ("전체일치", "빠른일치") else "✖ ") + f"{r[7]} {r[6][5:10]}"

    def form(self, title, fields, initial=None):
        """fields: [(key, 라벨)] 입력창. 확인하면 dict, 취소하면 None."""
        win = tk.Toplevel(self)
        win.title(title)
        win.transient(self)
        win.grab_set()
        vs = {}
        for i, (k, label) in enumerate(fields):
            ttk.Label(win, text=label).grid(row=i, column=0, padx=8, pady=4, sticky="e")
            vs[k] = tk.StringVar(value=(initial or {}).get(k, ""))
            ttk.Entry(win, textvariable=vs[k], width=38).grid(row=i, column=1, padx=8, pady=4)
        out = {}

        def ok():
            out.update({k: v.get().strip() for k, v in vs.items()})
            out["_ok"] = True
            win.destroy()
        bf = ttk.Frame(win)
        bf.grid(row=len(fields), column=0, columnspan=2, pady=8)
        ttk.Button(bf, text="확인", command=ok).pack(side="left", padx=6)
        ttk.Button(bf, text="취소", command=win.destroy).pack(side="left")
        self.wait_window(win)
        return out if out.get("_ok") else None

    def one_disk(self):
        nos = self.selected_disks()
        if len(nos) != 1 or not self.plan:
            messagebox.showinfo("확인", "디스크를 하나만 선택하세요.")
            return None
        return nos[0]

    def record_burn(self):
        n = self.one_disk()
        if n is None:
            return
        f = self.form(f"disk{n:02d} 백업 완료 기록", [
            ("copy", "사본 번호(비우면 자동)"), ("media", "미디어 ID/시리얼"),
            ("loc", "보관 위치"), ("note", "메모")])
        if not f:
            return
        try:
            copy = int(f["copy"]) if f["copy"] else None
            c = core.record_burn(self.conn, self.plan, n, copy, f["media"], f["loc"], f["note"])
        except (ValueError, SystemExit) as e:
            messagebox.showerror("오류", str(e) or "사본 번호는 숫자여야 합니다.")
            return
        self.say(f"disk{n:02d} 사본 {c} 백업 완료 기록됨 → 이제 '스테이징↔미디어 비교'로 검증하세요.")
        self.refresh_disks()

    def run_media_compare(self):
        n = self.one_disk()
        if n is None:
            return
        stage = self.stage_var.get().strip()
        if not stage:
            messagebox.showinfo("확인", "스테이징 위치를 먼저 지정하세요(⑤ 출력).")
            return
        stage_dir = os.path.join(stage, f"disk{n:02d}")
        if not os.path.isdir(stage_dir):
            messagebox.showerror("오류", f"스테이징 폴더가 없습니다:\n{stage_dir}\n먼저 내보내기를 하세요.")
            return
        media = filedialog.askdirectory(title=f"disk{n:02d} 를 구운 미디어(드라이브) 선택")
        if not media:
            return
        ans = messagebox.askyesnocancel(
            "비교 방식", "예: 전체(바이트) 비교 — 권장. 수 GB~수십 GB를 읽으므로 오래 걸립니다.\n"
                        "아니요: 빠른 검사 — 존재·크기만 확인(내용 변조·읽기 불량은 못 잡음).")
        if ans is None:
            return
        full, plan = bool(ans), self.plan
        last = [0.0]

        def fn(conn):
            def prog(f, t, b):
                if time.time() - last[0] > 0.3:
                    last[0] = time.time()
                    self.q.put(("prog", None, f"비교 중… {f:,}/{t:,}개 · {core.fmt(b)}"))
            return core.verify_media(conn, core.get_plan(conn, plan["name"]), n, stage_dir, media, full, None, prog)

        def done(res):
            self.say(f"disk{n:02d} 미디어 비교 {'전체' if full else '빠른'}: {res['verdict']} — "
                     f"{core.summarize_compare(res)}")
            for key, label in (("missing", "미디어에 없음"), ("extra", "미디어에만 있음"),
                               ("content_diff", "내용 다름"), ("hidden_missing", "숨김/시스템(미포함)")):
                for r in res[key][:30]:
                    self.say(f"  {label}: {r}")
            for r, ss, ds in res["size_diff"][:30]:
                self.say(f"  크기 다름: {r} (스테이징 {ss:,} / 미디어 {ds:,})")
            for r, e in res["read_errors"][:30]:
                self.say(f"  읽기 오류: {r} ({e})")
            self.refresh_disks()
            if res["ok"] and full:
                messagebox.showinfo("검증 완료", f"disk{n:02d}: 스테이징과 미디어가 모두 일치합니다. ('검증됨')")
            elif res["ok"]:
                messagebox.showinfo("빠른 검사 일치", "존재·크기는 일치합니다. 내용까지 확인하려면 전체 비교를 하세요.")
            else:
                messagebox.showwarning("불일치", f"disk{n:02d}: {core.summarize_compare(res)}\n자세한 목록은 로그를 보세요.")
        self.run_bg(fn, done, "미디어 비교 중…")

    def show_history(self):
        if not self.plan:
            messagebox.showinfo("확인", "계획이 없습니다.")
            return
        nos = self.selected_disks()
        disk = nos[0] if len(nos) == 1 else None
        win = tk.Toplevel(self)
        win.title("이력 · " + (f"disk{disk:02d}" if disk else "전체"))
        win.geometry("980x560")
        ttk.Label(win, text="사본(굽기) 기록").pack(anchor="w", padx=8, pady=(8, 0))
        rt = ttk.Treeview(win, show="headings", height=7,
                          columns=("disk", "copy", "media", "loc", "burned", "ver", "res", "note"))
        for c, t, w in (("disk", "디스크", 60), ("copy", "사본", 40), ("media", "미디어 ID", 150),
                        ("loc", "보관 위치", 120), ("burned", "구운 시각", 130), ("ver", "검증 시각", 130),
                        ("res", "결과", 80), ("note", "메모", 150)):
            rt.heading(c, text=t)
            rt.column(c, width=w, anchor="w")
        rt.pack(fill="x", padx=8, pady=4)
        for _id, dn, cp, mid, loc, bat, vat, vres, note in core.burn_records(self.conn, self.plan, disk):
            rt.insert("", "end", values=(f"disk{dn:02d}", cp, mid or "", loc or "", bat or "", vat or "",
                                         vres or "", note or ""))
        ttk.Label(win, text="이력 (최근 순)").pack(anchor="w", padx=8)
        fr = ttk.Frame(win)
        fr.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        ht = ttk.Treeview(fr, show="headings", columns=("at", "disk", "event", "detail"))
        for c, t, w in (("at", "시각", 130), ("disk", "대상", 60), ("event", "이벤트", 200), ("detail", "내용", 500)):
            ht.heading(c, text=t)
            ht.column(c, width=w, anchor="w", stretch=(c == "detail"))
        sb = ttk.Scrollbar(fr, orient="vertical", command=ht.yview)
        ht.configure(yscrollcommand=sb.set)
        ht.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for at, dn, ev, detail in reversed(core.history(self.conn, self.plan, disk)):
            ht.insert("", "end", values=(at, "계획" if dn == 0 else f"disk{dn:02d}", ev, detail or ""))

    # ------------------------------------------------------------ export / verify
    def run_export(self):
        nos = self.selected_disks()
        stage = self.stage_var.get().strip()
        if not nos or not stage:
            messagebox.showinfo("확인", "내보낼 디스크를 선택하고 스테이징 위치를 지정하세요.")
            return
        plan = self.plan

        def fn(conn):
            p = core.get_plan(conn, plan["name"])
            out = []
            for n in nos:
                self.q.put(("prog", None, f"disk{n:02d} 하드링크 생성 중…"))
                out.append(core.export_disk(conn, p, n, stage))
            return out

        def done(res):
            for dest, csvp, made in res:
                self.say(f"내보냄: {dest} (링크 {made:,}개), 목록 {csvp}")
            self.say("ImgBurn: Build 모드 → 위 폴더를 소스로 → UDF → 볼륨 라벨은 INDEX.txt 참고")
        self.run_bg(fn, done, "내보내는 중…")

    def run_verify(self):
        nos = self.selected_disks()
        if len(nos) != 1:
            messagebox.showinfo("확인", "검증할 디스크를 하나만 선택하세요.")
            return
        path = filedialog.askdirectory(title="구운 디스크(또는 스테이징 폴더) 선택")
        if not path:
            return
        plan, n = self.plan, nos[0]

        def fn(conn):
            return core.verify_disk(conn, core.get_plan(conn, plan["name"]), n, path)

        def done(res):
            ok, miss, extra, diff = res
            if ok:
                self.say(f"disk{n:02d} 경로·크기 일치(DB 기준). 내용 검증은 '스테이징↔미디어 비교'(전체)를 사용하세요.")
            else:
                self.say(f"disk{n:02d} 불일치: 누락 {len(miss)}, 추가 {len(extra)}, 크기다름 {len(diff)}")
                for lbl, lst in (("누락", miss), ("추가", extra), ("크기다름", diff)):
                    for r in lst[:30]:
                        self.say(f"  {lbl}: {r}")
            self.refresh_disks()
        self.run_bg(fn, done, "검증 중…")


if __name__ == "__main__":
    App().mainloop()
