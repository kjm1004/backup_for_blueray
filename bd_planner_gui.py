#!/usr/bin/env python3
"""BD-Planner GUI (tkinter). bd_planner.py 와 같은 폴더에 두고 실행한다."""
import json
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
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bd_planner_gui_state.json")
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
        self.restore_state()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.job = None            # 실행 중인 백그라운드 작업의 진행 상태(아래 '작업 진행 표시' 참고)
        self._bar_ind = False
        self.after(100, self._poll)
        self.after(1000, self._tick)

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
        ttk.Button(top, text="설정 저장…", command=self.save_settings).grid(row=2, column=0, **pad)
        ttk.Button(top, text="설정 불러오기…", command=self.load_settings).grid(row=2, column=1, columnspan=2, sticky="w", **pad)
        ttk.Label(top, text="※ ①~⑤ 설정(원본·이름·용량·오버헤드·기본 미디어·폴더 선택/순서·디스크별 미디어·스테이징 위치)을 파일로 저장/복원합니다. "
                            "스캔·배정·이력은 저장하지 않습니다.",
                  foreground="#555").grid(row=2, column=3, columnspan=6, sticky="w", **pad)
        ttk.Label(top, text="기본 미디어").grid(row=3, column=0, **pad)
        self.defmedia_var = tk.StringVar(value=NO_MEDIA)
        self.defmedia_cb = ttk.Combobox(top, textvariable=self.defmedia_var, width=30, state="readonly")
        self.defmedia_cb.grid(row=3, column=1, columnspan=4, sticky="we", **pad)
        ttk.Button(top, text="미디어 종류 설정…", command=self.edit_media_types).grid(row=3, column=5, columnspan=2, **pad)
        ttk.Label(top, text="※ 지정 없는 디스크는 이 미디어(100G/50G/25G)의 용량·오버헤드를 씁니다. '(없음)'이면 위 숫자(디스크 용량·오버헤드)를 사용.",
                  foreground="#555").grid(row=4, column=0, columnspan=9, sticky="w", **pad)

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

        md = ttk.Frame(right)
        md.pack(fill="x", padx=4, pady=(4, 0))
        ttk.Label(md, text="선택 디스크 미디어:").pack(side="left")
        self.media_var = tk.StringVar()
        self.media_cb = ttk.Combobox(md, textvariable=self.media_var, width=16, state="readonly")
        self.media_cb.pack(side="left", padx=4)
        ttk.Button(md, text="적용 후 재배정", command=self.apply_media).pack(side="left")
        ttk.Button(md, text="선택 번호~끝까지", command=lambda: self.apply_media(to_end=True)).pack(side="left", padx=4)
        ttk.Button(md, text="해제", command=lambda: self.apply_media(clear=True)).pack(side="left")
        ttk.Button(md, text="재배정", command=self.run_realloc).pack(side="left", padx=8)
        self.media_info = tk.StringVar()
        ttk.Label(md, textvariable=self.media_info, foreground="#555").pack(side="left", padx=8)

        hv = ttk.Frame(right)
        hv.pack(fill="x", padx=4, pady=(4, 0))
        ttk.Label(hv, text="이력·검증:").pack(side="left")
        ttk.Button(hv, text="백업 완료 기록…", command=self.record_burn).pack(side="left", padx=6)
        ttk.Button(hv, text="스테이징↔미디어 비교…", command=self.run_media_compare).pack(side="left")
        ttk.Button(hv, text="복사 후 비교…", command=self.run_copy_compare).pack(side="left", padx=6)
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
        ttk.Label(out, text="임시 복사 위치").grid(row=2, column=0, padx=6, pady=4)
        self.temp_var = tk.StringVar()
        ttk.Entry(out, textvariable=self.temp_var, width=26).grid(row=2, column=1, padx=4)
        ttk.Button(out, text="찾아보기…", command=self.browse_temp).grid(row=2, column=2, padx=4)
        self.deltmp_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(out, text="일치하면 임시 복사본 삭제", variable=self.deltmp_var).grid(row=2, column=3, padx=8)
        ttk.Label(out, text="※ '복사 후 비교'용. 미디어 크기만큼 여유 공간이 필요하며, 이 위치 아래 _bd_verify 폴더에만 씁니다. "
                            "스테이징·미디어와 겹치면 안 됩니다.",
                  foreground="#555").grid(row=3, column=0, columnspan=4, sticky="w", padx=6, pady=(0, 4))
        ttk.Label(out, text="ISO 저장 위치").grid(row=4, column=0, padx=6, pady=4)
        self.iso_var = tk.StringVar()
        ttk.Entry(out, textvariable=self.iso_var, width=26).grid(row=4, column=1, padx=4)
        ttk.Button(out, text="찾아보기…", command=self.browse_iso).grid(row=4, column=2, padx=4)
        ttk.Button(out, text="ISO 만들기 (검증 먼저)…", command=self.run_make_iso).grid(row=4, column=3, padx=8)
        ttk.Label(out, text="※ 스테이징 → UDF(2.50) ISO. 사전 검증을 통과해야만 만들고, 만든 뒤 라벨·리비전·파일 시스템을 다시 확인합니다. Windows 전용.",
                  foreground="#555").grid(row=5, column=0, columnspan=4, sticky="w", padx=6, pady=(0, 4))

        self.refresh_media_widgets()
        self.log = tk.Text(right, height=9, wrap="word")
        self.log.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self.status = tk.StringVar(value="준비")
        bar = ttk.Frame(self)
        bar.pack(fill="x", side="bottom")
        self.pct_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.pct_var, width=7, anchor="e").pack(side="right", padx=(0, 6))
        self.pbar = ttk.Progressbar(bar, orient="horizontal", length=220, mode="determinate", maximum=100)
        self.pbar.pack(side="right", padx=4, pady=2)
        ttk.Label(bar, textvariable=self.status, relief="sunken", anchor="w").pack(side="left", fill="x", expand=True)

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

    def browse_iso(self):
        d = filedialog.askdirectory(title="ISO 저장 위치 선택(원본과 다른 빠른 디스크, 여유 공간 필요)")
        if d:
            self.iso_var.set(os.path.normpath(d))

    def browse_temp(self):
        d = filedialog.askdirectory(title="임시 복사 위치 선택(여유 공간이 큰 하드디스크)")
        if d:
            self.temp_var.set(os.path.normpath(d))

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
        self._job_start(label)

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
                    if self.job:
                        self.job["text"] = res
                        self._refresh_status()
                    continue
                if kind == "pct":
                    if self.job:
                        self.job["pct"] = res
                        self.job["eta"].update(res)
                        self._set_bar(res)
                        self._refresh_status()
                    continue
                self.busy = False
                self._job_end(failed=(kind == "err"))
                if kind == "err":
                    self.say(f"[오류] {res}")
                    messagebox.showerror("오류", str(res))
                else:
                    done(res)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    # ------------------------------------------------------------ 작업 진행 표시 (공통)
    # run_bg 로 실행하는 모든 작업이 자동으로 받는다: 하단에 '경과 시간', 총량을 아는 작업은 진행 막대·퍼센트·'남은 시간'.
    #  - 상태 문구 갱신:  self.q.put(("prog", None, "문구"))
    #  - 진행률(0~100):   self.q.put(("pct", None, 퍼센트))   ← 보내면 진행 막대와 남은 시간이 켜진다
    #  - 진행률을 모르는 작업(스캔 등)은 문구만 보내면 '경과 시간'만 표시되고 막대는 움직이는 표시로 바뀐다.
    # 새 기능을 추가할 때도 run_bg 로 실행하고 위 두 메시지(또는 post_progress)만 보내면 동일하게 표시된다.
    def _job_start(self, label):
        self.job = {"label": label, "text": label, "pct": None, "eta": core.Eta()}
        self._set_bar(None)
        self._refresh_status()

    def _job_end(self, failed=False):
        j, self.job = self.job, None
        self._set_bar(0, idle=True)
        self.status.set("준비")
        if j:
            el, _ = j["eta"].snapshot()
            if el >= 3:
                self.say(f"⏱ {j['label']}: {'중단됨, ' if failed else ''}총 {core.fmt_duration(el)} 소요")

    def _set_bar(self, pct, idle=False):
        if pct is None:                                   # 진행률을 아직 모름 → 움직이는 막대
            if not self._bar_ind:
                self.pbar.configure(mode="indeterminate")
                self.pbar.start(60)
                self._bar_ind = True
            self.pct_var.set("")
            return
        if self._bar_ind:
            self.pbar.stop()
            self.pbar.configure(mode="determinate")
            self._bar_ind = False
        self.pbar["value"] = pct
        self.pct_var.set("" if idle else f"{pct:.1f}%")

    def _refresh_status(self):
        j = self.job
        if not j:
            return
        el, rem = j["eta"].snapshot()
        parts = [j["text"], f"경과 {core.fmt_duration(el)}"]
        if j["pct"] is not None:
            parts.append("남은 약 " + (core.fmt_duration(rem) if rem is not None else "계산 중…"))
        self.status.set("  ·  ".join(parts))

    def _tick(self):
        """1초마다 경과·남은 시간을 갱신(진행 메시지가 뜸해도 시간은 계속 흐른다)."""
        if self.job:
            self._refresh_status()
        self.after(1000, self._tick)

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
            self.say(f"예상 디스크 수(기본 용량 기준): 십진 {dec}장 / 이진 {bi}장 → 미디어 설정 반영 실제 배정 {nd}장")
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
            self.media_var.set(sp.media_label(sel[0]))
            self.media_info.set(f"사용 가능 {core.fmt(sp.cap(sel[0]))} · 파일당 오버헤드 {sp.ovh(sel[0]):,}B")

    # ------------------------------------------------------------ settings save / load
    def ui_values(self):
        return {"unit": self.unit_var.get(), "capacity_text": self.cap_var.get(), "stage": self.stage_var.get(),
                "temp": self.temp_var.get(), "delete_temp_when_ok": bool(self.deltmp_var.get()),
                "iso_dir": self.iso_var.get()}

    def set_ui_values(self, ui):
        if ui.get("unit") in ("GB(십진)", "GiB(이진)"):
            self.unit_var.set(ui["unit"])
        if ui.get("capacity_text"):
            self.cap_var.set(ui["capacity_text"])
        if ui.get("stage") is not None:
            self.stage_var.set(ui["stage"])
        if ui.get("temp") is not None:
            self.temp_var.set(ui["temp"])
        if ui.get("delete_temp_when_ok") is not None:
            self.deltmp_var.set(bool(ui["delete_temp_when_ok"]))
        if ui.get("iso_dir") is not None:
            self.iso_var.set(ui["iso_dir"])

    def save_settings(self):
        try:
            plan = self.ensure_plan()
            self.save_default_media()
        except ValueError as e:
            if str(e) != "취소됨":
                messagebox.showerror("오류", str(e))
            return
        path = filedialog.asksaveasfilename(
            title="설정 저장", defaultextension=".json", initialfile=f"{plan['name']}_설정.json",
            filetypes=[("BD-Planner 설정", "*.json"), ("모든 파일", "*.*")])
        if not path:
            return
        try:
            core.write_settings(path, core.export_settings(self.conn, plan, self.checked(),
                                                           {"ui": self.ui_values()}))
        except OSError as e:
            messagebox.showerror("오류", f"저장할 수 없습니다:\n{e}")
            return
        self.say(f"설정 저장: {path}  (폴더 {len(self.checked())}개 선택, 디스크별 미디어 "
                 f"{len(core.get_disk_media(self.conn, plan))}개)")

    def load_settings(self):
        path = filedialog.askopenfilename(title="설정 불러오기",
                                          filetypes=[("BD-Planner 설정", "*.json"), ("모든 파일", "*.*")])
        if not path:
            return
        try:
            data = core.read_settings(path)
        except SystemExit as e:
            messagebox.showerror("오류", str(e))
            return
        p = data["plan"]
        root = p.get("root", "")
        if not os.path.isdir(root):
            messagebox.showwarning("원본 폴더 없음", f"저장된 원본 폴더를 이 PC에서 찾을 수 없습니다:\n{root}\n"
                                   "현재 PC의 원본 폴더를 선택해 주세요.")
            root = filedialog.askdirectory(title="원본 폴더 선택")
            if not root:
                return
            root = os.path.normpath(root)
        # ① 화면 값
        self.root_var.set(root)
        self.name_var.set(p.get("name", ""))
        ui = data.get("ui") or {}
        unit_gib = ui.get("unit", "GB(십진)").startswith("GiB")
        self.unit_var.set("GiB(이진)" if unit_gib else "GB(십진)")
        cap = p.get("capacity_bytes")
        self.cap_var.set(ui.get("capacity_text") or (f"{cap / (core.GIB if unit_gib else core.GB):.6g}" if cap else "90"))
        self.ovh_var.set(str(p.get("overhead_per_file", 4096)))
        self.set_ui_values({"stage": ui.get("stage"), "temp": ui.get("temp"),
                            "delete_temp_when_ok": ui.get("delete_temp_when_ok"), "iso_dir": ui.get("iso_dir")})
        # 계획 생성/갱신(원본이 달라지면 확인)
        try:
            self.ensure_plan()
        except ValueError as e:
            if str(e) != "취소됨":
                messagebox.showerror("오류", str(e))
            return
        apply_types = False
        diff = core.media_types_differ(self.conn, data)
        if diff:
            apply_types = messagebox.askyesno(
                "미디어 종류 값 다름",
                "저장된 미디어 종류 값이 현재와 다릅니다: " + ", ".join(diff) +
                "\n(모든 계획에 공통으로 적용되는 값입니다.)\n\n저장된 값으로 바꿀까요?")
        try:
            plan, notes = core.apply_settings(self.conn, data, self.name_var.get().strip(), root, apply_types)
        except SystemExit as e:
            messagebox.showerror("오류", str(e))
            return
        self.refresh_media_widgets()
        self.load_folders()                       # DB 선택(스캔 전이면 방금 반영됨)을 목록에 표시
        self.apply_folder_selection(data.get("folders") or [])   # 스캔 결과가 있어도 화면 선택·순서는 복원
        self.say(f"설정 불러옴: {path}")
        for n in notes:
            self.say(f"  ※ {n}")
        if core.get_disk_media(self.conn, plan) or core.get_setting(self.conn, plan, "default_media"):
            self.say("  ※ 미디어 지정을 배정에 반영하려면 [재배정]을 누르세요.")

    def apply_folder_selection(self, names):
        """목록에서 names 를 ☑ 하고 그 순서대로 맨 위에 올린다. 나머지는 ☐ 로 아래에."""
        rows = {self.ftree.item(i, "values")[2]: list(self.ftree.item(i, "values")) for i in self.ftree.get_children()}
        chosen = [n for n in names if n in rows]
        missing = [n for n in names if n not in rows]
        order = chosen + [n for n in rows if n not in chosen]
        self.ftree.delete(*self.ftree.get_children())
        for n in order:
            v = rows[n]
            v[0] = CHECK if n in chosen else UNCHECK
            self.ftree.insert("", "end", values=v)
        self.scan_sum = None
        self.renumber()
        if missing:
            self.say("  ※ 원본에 없는 폴더는 건너뜀: " + ", ".join(missing))

    def restore_state(self):
        """마지막으로 쓴 ① 화면 값을 복원하고, 원본이 있으면 폴더 목록을 자동으로 읽는다."""
        try:
            with open(STATE_PATH, encoding="utf-8") as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            return
        for key, var in (("root", self.root_var), ("name", self.name_var), ("overhead", self.ovh_var)):
            if st.get(key):
                var.set(st[key])
        if st.get("capacity_text"):
            self.cap_var.set(st["capacity_text"])
        self.set_ui_values({"unit": st.get("unit"), "stage": st.get("stage"), "temp": st.get("temp"),
                            "delete_temp_when_ok": st.get("delete_temp_when_ok"), "iso_dir": st.get("iso_dir")})
        if st.get("root") and st.get("name") and os.path.isdir(st["root"]):
            self.after(300, self.load_folders)

    def on_close(self):
        try:
            with open(STATE_PATH, "w", encoding="utf-8") as fh:
                json.dump({"root": self.root_var.get(), "name": self.name_var.get(),
                           "overhead": self.ovh_var.get(), **self.ui_values()}, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass
        self.destroy()

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
        if clear:                       # 해제하면 예전에 CLI 로 지정한 직접 용량도 함께 지움
            for n in nos:
                core.set_capacity(self.conn, self.plan, n, None)
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

    # ------------------------------------------------------------ re-allocate
    def run_realloc(self, changed=None):
        """스캔 없이 현재 미디어 설정으로 다시 배정."""
        if not self.plan:
            messagebox.showinfo("확인", "먼저 스캔 + 배정 계산을 실행하세요.")
            return
        plan = self.plan
        if not self.conn.execute("SELECT 1 FROM file_info WHERE plan_id=? LIMIT 1", (plan["plan_id"],)).fetchone():
            messagebox.showinfo("확인", "스캔 결과가 없습니다. 먼저 스캔 + 배정 계산을 실행하세요.")
            return
        try:
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

    def post_progress(self, label, p, last):
        """compare/copy 진행 정보(dict)를 하단 표시줄(파일명·건수·용량)과 진행 막대로 보낸다(0.3초마다)."""
        if time.time() - last[0] <= 0.3:
            return
        last[0] = time.time()
        step = p.get("step")
        name = core.short_name(p.get("file", ""), 70)
        text = (f"{label}" + (f" [{step}]" if step else "") + f" · {p['files_done']:,}/{p['files_total']:,}개 · "
                f"{core.fmt(p['bytes_done'])} · {name}")
        self.q.put(("prog", None, text))
        self.q.put(("pct", None, p["percent"]))

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
            def prog(p):
                self.post_progress("미디어 비교 중", p, last)
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

    def run_copy_compare(self):
        """미디어 → 임시 위치 복사 → 복사본↔스테이징 전체 비교."""
        n = self.one_disk()
        if n is None:
            return
        stage, temp = self.stage_var.get().strip(), self.temp_var.get().strip()
        if not stage or not temp:
            messagebox.showinfo("확인", "스테이징 위치와 임시 복사 위치를 먼저 지정하세요(⑤ 출력).")
            return
        stage_dir = os.path.join(stage, f"disk{n:02d}")
        if not os.path.isdir(stage_dir):
            messagebox.showerror("오류", f"스테이징 폴더가 없습니다:\n{stage_dir}\n먼저 내보내기를 하세요.")
            return
        if not os.path.isdir(temp):
            messagebox.showerror("오류", f"임시 복사 위치에 접근할 수 없습니다:\n{temp}")
            return
        media = filedialog.askdirectory(title=f"disk{n:02d} 를 구운 미디어(드라이브) 선택")
        if not media:
            return
        if not messagebox.askyesno(
                "복사 후 비교",
                f"1) 미디어의 파일을 모두 임시 위치로 복사합니다(읽기 오류는 파일별로 기록).\n"
                f"2) 복사본을 스테이징과 전체(바이트) 비교합니다.\n\n임시 위치: {temp}\\_bd_verify\n"
                "미디어 크기만큼 여유 공간이 필요하고 시간이 오래 걸립니다. 계속할까요?"):
            return
        plan, delete_ok = self.plan, bool(self.deltmp_var.get())
        last = [0.0]

        def fn(conn):
            def prog(p):
                self.post_progress("복사 후 비교 중", p, last)
            return core.verify_via_copy(conn, core.get_plan(conn, plan["name"]), n, stage_dir, media, temp,
                                        None, delete_ok, prog)

        def done(res):
            self.say(f"disk{n:02d} 복사 후 비교: {res['verdict']} — {core.summarize_compare(res)} "
                     f"(복사 {res['copy_seconds'] / 60:.1f}분)")
            for r, e in res["copy_errors"][:30]:
                self.say(f"  디스크 읽기 실패: {r} ({e})")
            for key, label in (("missing", "복사본에 없음"), ("extra", "복사본에만 있음"),
                               ("content_diff", "내용 다름"), ("hidden_missing", "숨김/시스템(미포함)")):
                for r in res[key][:30]:
                    self.say(f"  {label}: {r}")
            for r, ss, ds in res["size_diff"][:30]:
                self.say(f"  크기 다름: {r} (스테이징 {ss:,} / 복사본 {ds:,})")
            self.say(f"  임시 복사본: {res['temp_path']} ({'보존됨' if res['kept'] else '삭제함'})")
            self.refresh_disks()
            if res["ok"]:
                messagebox.showinfo("검증 완료", f"disk{n:02d}: 미디어 내용이 스테이징과 모두 일치합니다. ('검증됨')")
            else:
                messagebox.showwarning("불일치", f"disk{n:02d}: {core.summarize_compare(res)}\n"
                                       "임시 복사본은 남겨 두었습니다. 목록은 로그를 보세요.")
        self.run_bg(fn, done, "복사 후 비교 중…")

    def run_make_iso(self):
        """① 사전 검증(쓰기 없음) → ② 결과를 보고 확인 → ③ 생성 → ④ 생성물 검증."""
        n = self.one_disk()
        if n is None:
            return
        stage, iso_dir = self.stage_var.get().strip(), self.iso_var.get().strip()
        if not stage or not iso_dir:
            messagebox.showinfo("확인", "스테이징 위치와 ISO 저장 위치를 먼저 지정하세요(⑤ 출력).")
            return
        f = self.form(f"disk{n:02d} ISO 설정", [("label", "UDF 볼륨 라벨 (UDF 2.50)")], {"label": f"disk{n:02d}"})
        if not f:
            return
        label = f["label"]
        err = core.validate_label(label)
        if err:
            messagebox.showerror("라벨 오류", err)
            return
        iso_path = os.path.join(iso_dir, f"disk{n:02d}.iso")
        overwrite = False
        if os.path.exists(iso_path):
            if not messagebox.askyesno("기존 파일", f"이미 있습니다:\n{iso_path}\n\n덮어쓸까요?", default="no"):
                return
            overwrite = True
        plan = self.plan

        def lines(checks):
            return "\n".join(f"{'✔' if ok else ('–' if ok is None else '✖')} {t}: {m}" for t, ok, m in checks)

        def fn_pre(conn):          # 쓰지 않고 검증만
            return core.make_iso(conn, core.get_plan(conn, plan["name"]), n, stage, iso_path, label,
                                 overwrite, check_only=True)

        def done_pre(res):
            pre = res["preflight"]
            self.say(f"disk{n:02d} ISO 사전 검증: {'통과' if pre['ok'] else '실패'}")
            for t, ok, m in pre["checks"]:
                self.say(f"  {'✔' if ok else ('–' if ok is None else '✖')} {t}: {m}")
            if not pre["ok"]:
                messagebox.showwarning("사전 검증 실패", lines(pre["checks"]) + "\n\nISO 를 만들지 않았습니다.")
                return
            if not pre["can_build"]:
                messagebox.showinfo("생성 불가", lines(pre["checks"]) +
                                    "\n\nIMAPI 로 UDF 2.50 을 확인할 수 없어 만들지 않았습니다(Windows PowerShell 필요).")
                return
            if not messagebox.askyesno("사전 검증 통과", lines(pre["checks"]) +
                                       f"\n\n{iso_path}\n라벨 '{label}', UDF 2.50 으로 ISO 를 만들까요?"):
                self.say("ISO 생성을 취소했습니다.")
                return
            last = [0.0]

            def fn_build(conn):
                self.q.put(("prog", None, "ISO 준비 중(검증·파일 목록 구성)…"))

                def prog(p):
                    if time.time() - last[0] > 0.3:
                        last[0] = time.time()
                        self.q.put(("prog", None, f"ISO 생성 중… {p['percent']:.1f}% · "
                                                  f"{core.fmt(p['bytes_done'])} / {core.fmt(p['bytes_total'])}"))
                        self.q.put(("pct", None, p["percent"]))
                return core.make_iso(conn, core.get_plan(conn, plan["name"]), n, stage, iso_path, label,
                                     overwrite, False, prog, None)

            def done_build(r):
                self.say(f"disk{n:02d} ISO 생성 후 검증: {'통과' if r['ok'] else '실패'} — {iso_path}")
                for t, ok, m in r["post"]:
                    self.say(f"  {'✔' if ok else '✖'} {t}: {m}")
                self.refresh_disks()
                if r["ok"]:
                    messagebox.showinfo("ISO 완료", lines(r["post"]) + f"\n\n{iso_path}\n\n다음: ImgBurn Write 모드로 이 ISO 를 굽고 Verify 하세요.")
                else:
                    messagebox.showwarning("생성물 검증 실패", lines(r["post"]) + "\n\n이 ISO 로 굽지 마세요.")
            self.run_bg(fn_build, done_build, "ISO 생성 중…")
        self.run_bg(fn_pre, done_pre, "ISO 사전 검증 중…")

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
            counts = dict(conn.execute("SELECT disk_no, file_count FROM disk WHERE plan_id=?", (p["plan_id"],)))
            total = sum(counts.get(n, 0) for n in nos) or 1      # 선택한 모든 디스크를 합친 파일 수
            base, last, out = [0], [0.0], []
            for n in nos:
                def prog(info, n=n):
                    if time.time() - last[0] < 0.3:
                        return
                    last[0] = time.time()
                    done_n = base[0] + info["files_done"]
                    self.q.put(("prog", None, f"disk{n:02d} 하드링크 생성 · {done_n:,}/{total:,}개 · "
                                              f"{core.short_name(info['file'], 50)}"))
                    self.q.put(("pct", None, 100.0 * (done_n - 1) / total))
                out.append(core.export_disk(conn, p, n, stage, progress=prog))
                base[0] += counts.get(n, 0)
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
