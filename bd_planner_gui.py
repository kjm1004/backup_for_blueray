#!/usr/bin/env python3
"""BD-Planner GUI (tkinter). bd_planner.py 와 같은 폴더에 두고 실행한다."""
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bd_planner as core  # noqa: E402

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bd_planner.db")
CHECK, UNCHECK = "☑", "☐"


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
        self.scan_btn.pack(fill="x", padx=6, pady=(0, 6))

        right = ttk.LabelFrame(body, text="④ 디스크 배정 결과")
        body.add(right, weight=2)
        self.dtree = ttk.Treeview(right, columns=("disk", "st", "cap", "used", "pct", "cnt", "folders"),
                                  show="headings", selectmode="extended", height=12)
        for c, t, w, a in (("disk", "디스크", 70, "w"), ("st", "상태", 70, "w"), ("cap", "용량(GB)", 80, "e"),
                           ("used", "사용(GB)", 80, "e"),
                           ("pct", "채움", 55, "e"), ("cnt", "파일수", 70, "e"), ("folders", "포함 폴더", 360, "w")):
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
        ttk.Button(act, text="굽기 후 검증…", command=self.run_verify).pack(side="left", padx=12)

        cap = ttk.Frame(right)
        cap.pack(fill="x", padx=4, pady=(4, 0))
        ttk.Label(cap, text="선택 디스크 용량:").pack(side="left")
        self.cap_one_var = tk.StringVar()
        ttk.Entry(cap, textvariable=self.cap_one_var, width=8).pack(side="left", padx=4)
        ttk.Label(cap, text="(위 단위)").pack(side="left")
        ttk.Button(cap, text="적용 후 재배정", command=self.apply_disk_capacity).pack(side="left", padx=6)
        ttk.Button(cap, text="기본 용량으로", command=lambda: self.apply_disk_capacity(reset=True)).pack(side="left")
        ttk.Button(cap, text="재배정", command=self.run_realloc).pack(side="left", padx=12)

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
        for f in [s for s in saved if s in subs] + [s for s in subs if s not in saved]:
            cnt, size = stats.get(f, (None, None))
            self.ftree.insert("", "end", values=(CHECK if f in saved else UNCHECK, "", f,
                                                 f"{cnt:,}" if cnt else "", core.fmt(size) if size else ""))
        self.renumber()
        self.sync_caps_entry()
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
        core.set_folders(self.conn, plan, names)
        try:
            core.set_capacities(self.conn, plan, self.parse_caps())   # 입력칸 = 디스크별 용량(스캔 시 저장)
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

    # ------------------------------------------------------------ disks view
    def refresh_disks(self):
        keep = self.dtree.selection()
        self.dtree.delete(*self.dtree.get_children())
        self._restore_sel = keep
        if not self.plan:
            return
        plan = self.plan
        split = dict(core.split_folders(self.conn, plan))
        caps = core.get_capacities(self.conn, plan)
        for no, name, used, cnt, st in self.conn.execute(
                "SELECT disk_no, disk_name, used_bytes, file_count, status FROM disk "
                "WHERE plan_id=? ORDER BY disk_no", (plan["plan_id"],)).fetchall():
            parts = []
            for folder, c, size in core.disk_folders(self.conn, plan, no):
                parts.append(f"{folder} {size / core.GB:.1f}G" + ("◀▶" if folder in split else ""))
            cap = core.capacity_of(caps, plan, no)
            self.dtree.insert("", "end", iid=str(no), tags=(st,), values=(
                name, st, f"{cap / core.GB:.2f}" + ("*" if no in caps else ""),
                f"{used / core.GB:.2f}", f"{used * 100 / cap:.0f}%", f"{cnt:,}",
                " · ".join(parts)))
        again = [i for i in self._restore_sel if self.dtree.exists(i)]
        if again:
            self.dtree.selection_set(again)

    def on_disk_select(self, _ev):
        sel = self.selected_disks()
        if len(sel) == 1 and self.plan:
            cap = core.capacity_of(core.get_capacities(self.conn, self.plan), self.plan, sel[0])
            v = cap / self.unit_bytes()
            self.cap_one_var.set(f"{v:.6g}")

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
                self.say(f"disk{n:02d} 검증 일치 → '검증됨'으로 기록")
            else:
                self.say(f"disk{n:02d} 불일치: 누락 {len(miss)}, 추가 {len(extra)}, 크기다름 {len(diff)}")
                for lbl, lst in (("누락", miss), ("추가", extra), ("크기다름", diff)):
                    for r in lst[:30]:
                        self.say(f"  {lbl}: {r}")
            self.refresh_disks()
        self.run_bg(fn, done, "검증 중…")


if __name__ == "__main__":
    App().mainloop()
