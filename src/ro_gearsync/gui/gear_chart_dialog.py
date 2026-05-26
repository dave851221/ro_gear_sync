"""Matplotlib gear-distribution dashboard.

Opened from the member-management dialog's 📊 裝評分析 button.

Two views (segmented button at top):

  * 全公會 — strip plot: every member as one dot, x-axis is profession,
    y-axis is peak gear score. Lets the user see at a glance which
    profession bands sit where.
  * 裝評變化 — line / scatter combo: each member is one line connecting
    their per-day gear-score samples. Lets the user spot growth
    trajectories or sudden drops across multiple captures.

Both views are interactive — hovering near a dot pops the member's
name + gear in the status line beneath the plot. No mean/median
overlays because the guild caps at ~150 members and the user wants
to look at individual data points, not aggregates.

The window keeps the standard Windows resize/maximise/minimise
controls so the user can blow the chart up to full-screen.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

import customtkinter as ctk

if TYPE_CHECKING:
    from ..storage import GuildScoresWorkbook


class GearChartDialog(ctk.CTkToplevel):
    def __init__(
        self,
        master: ctk.CTk,
        *,
        workbook: "GuildScoresWorkbook",
    ) -> None:
        super().__init__(master)
        self.workbook = workbook
        self.title("裝評分析")
        self.geometry("960x720")
        self.minsize(720, 520)
        # NOTE: deliberately NOT calling self.transient(master) — Windows
        # window managers hide the maximise button on transient
        # Toplevels, and the user explicitly wants to be able to
        # full-screen this chart. Leaving the window as a top-level
        # peer keeps the resize / maximise controls intact.
        # ``-topmost`` keeps the chart above the member-management
        # dialog (and the main app) so it doesn't get buried when the
        # user clicks back into the parent window to inspect data.
        # Unlike transient(), this does NOT hide the maximise button.
        try:
            self.attributes("-topmost", True)
        except Exception:
            pass

        # State
        self._view_mode: str = "全公會"
        # Filter state — both default to "show everyone". The dropdown
        # widgets created in _build_ui write back to these.
        self._filter_profession: str = "全部職業"
        self._filter_gear_range: str = "全部範圍"
        # Gear-range buckets are computed from the current peak-gear
        # distribution; populated in _build_ui before the dropdown is
        # constructed so its values list is correct on first render.
        self._gear_range_buckets: dict[str, tuple[int, int]] = {}
        # 裝評變化-only knobs (UI widgets get hooked up in _build_ui).
        #   * granularity = 日線圖 (one point per capture day) or
        #     週線圖 (one point per ISO-week, value = max gear in week).
        #   * date_from / date_to clip the timeline to a sub-range.
        #   * show_names overlays the member's correct_nickname on
        #     every plotted point (default off — labels stack badly
        #     for 100+ members; combine with filters when enabling).
        self._granularity: str = "日線圖"
        self._date_from: str | None = None
        self._date_to: str | None = None
        self._show_names: bool = False
        self._scatter_xy: list[tuple[float, float]] = []
        self._scatter_records: list = []  # parallel: PlayerRecord per point
        self._scatter_meta: list = []     # parallel: str (extra hover info)

        try:
            self._build_ui()
        except ModuleNotFoundError as exc:
            ctk.CTkLabel(
                self,
                text=(
                    f"無法載入 matplotlib：{exc}\n\n"
                    "請執行：\n    .venv\\Scripts\\pip install matplotlib"
                ),
                font=ctk.CTkFont(size=14),
                text_color="#cc3333",
                justify="left",
            ).pack(padx=24, pady=40, fill="both", expand=True)

    # ------------------------------------------------------------ setup

    def _build_ui(self) -> None:
        import matplotlib
        matplotlib.use("TkAgg")
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        import matplotlib.font_manager as _fm

        # Try CJK fonts so member names render properly.
        for cand in (
            "Microsoft JhengHei", "Microsoft YaHei", "PMingLiU",
            "MingLiU", "SimHei", "Noto Sans CJK TC",
        ):
            try:
                _fm.findfont(cand, fallback_to_default=False)
                matplotlib.rcParams["font.family"] = [cand]
                break
            except Exception:
                continue
        matplotlib.rcParams["axes.unicode_minus"] = False

        # ----- top bar: counts + view switcher --------------------------
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=12, pady=(12, 4))

        n_records = len(self.workbook.records)
        n_with_gear = sum(1 for r in self.workbook.records if r.peak_gear_score)
        days = self.workbook.capture_days
        ctk.CTkLabel(
            top,
            text=(
                f"工作簿：{self.workbook.path.name}  ｜  "
                f"成員 {n_records} 位（{n_with_gear} 位有裝評）  ｜  "
                f"資料日期 {len(days)} 天"
                + (f"（{days[0]} ~ {days[-1]}）" if days else "")
            ),
            font=ctk.CTkFont(size=11),
            text_color=("#444444", "#cccccc"),
        ).pack(anchor="w")

        switcher_row = ctk.CTkFrame(top, fg_color="transparent")
        switcher_row.pack(anchor="w", pady=(8, 0), fill="x")
        self.view_switcher = ctk.CTkSegmentedButton(
            switcher_row,
            values=["全公會", "裝評變化"],
            command=self._on_view_change,
        )
        self.view_switcher.set("全公會")
        self.view_switcher.pack(side="left")

        # ----- filters: profession + gear-range -----------------------
        # Both filters apply to either view (AND-combined). For 裝評變化
        # this is the main motivation — 150 overlapping lines are
        # unreadable; narrowing by profession + gear band makes
        # individual trajectories legible again.
        professions = sorted(
            {
                (r.profession or "").strip() or "未分類"
                for r in self.workbook.records
            },
            key=lambda k: (k == "未分類", k),
        )
        self._gear_range_buckets = self._compute_gear_buckets()

        ctk.CTkLabel(switcher_row, text="職業：").pack(side="left", padx=(20, 4))
        self.profession_filter = ctk.CTkOptionMenu(
            switcher_row,
            values=["全部職業"] + professions,
            command=self._on_filter_change,
            width=120,
        )
        self.profession_filter.set("全部職業")
        self.profession_filter.pack(side="left")

        ctk.CTkLabel(switcher_row, text="裝評範圍：").pack(side="left", padx=(16, 4))
        self.gear_filter = ctk.CTkOptionMenu(
            switcher_row,
            values=["全部範圍"] + list(self._gear_range_buckets.keys()),
            command=self._on_filter_change,
            width=180,
        )
        self.gear_filter.set("全部範圍")
        self.gear_filter.pack(side="left")

        # ----- 裝評變化-only controls (second row) ----------------------
        # 模式 + 日期 only matter for 裝評變化; they live in their own
        # sub-frame ``self._prog_only_frame`` so we can hide their
        # contents on the 全公會 tab without making the toolbar (and
        # therefore the window) shrink. The sub-frame's size is frozen
        # via ``pack_propagate(False)`` once tk has measured the
        # natural layout — see ``_freeze_prog_only_size`` further down.
        prog_row = ctk.CTkFrame(top, fg_color="transparent")
        prog_row.pack(anchor="w", pady=(8, 0), fill="x")

        self._prog_only_frame = ctk.CTkFrame(prog_row, fg_color="transparent")
        self._prog_only_frame.pack(side="left")

        mode_label = ctk.CTkLabel(self._prog_only_frame, text="模式：")
        mode_label.pack(side="left")
        self.granularity_switcher = ctk.CTkSegmentedButton(
            self._prog_only_frame,
            values=["日線圖", "週線圖"],
            command=self._on_granularity_change,
        )
        self.granularity_switcher.set("日線圖")
        self.granularity_switcher.pack(side="left", padx=(4, 0))

        # Date-range dropdowns — values mirror workbook.capture_days so
        # filtering is exact-string. "" sentinel means "no end clip".
        days_for_picker = self.workbook.capture_days or []
        date_values = days_for_picker if days_for_picker else ["—"]
        date_label = ctk.CTkLabel(self._prog_only_frame, text="日期：")
        date_label.pack(side="left", padx=(16, 4))
        self.date_from_menu = ctk.CTkOptionMenu(
            self._prog_only_frame,
            values=date_values,
            command=self._on_date_from_change,
            width=120,
        )
        if days_for_picker:
            self.date_from_menu.set(days_for_picker[0])
            self._date_from = days_for_picker[0]
        else:
            self.date_from_menu.configure(state="disabled")
        self.date_from_menu.pack(side="left")
        between_label = ctk.CTkLabel(self._prog_only_frame, text="至")
        between_label.pack(side="left", padx=(6, 6))
        self.date_to_menu = ctk.CTkOptionMenu(
            self._prog_only_frame,
            values=date_values,
            command=self._on_date_to_change,
            width=120,
        )
        if days_for_picker:
            self.date_to_menu.set(days_for_picker[-1])
            self._date_to = days_for_picker[-1]
        else:
            self.date_to_menu.configure(state="disabled")
        self.date_to_menu.pack(side="left")

        # Cache (widget, pack_info) for each child so we can re-pack
        # in the same order after a hide. pack_info() must be captured
        # before any pack_forget call — once forgotten, tk drops the
        # configuration and re-packing with no args goes to defaults.
        self._prog_only_children = [
            (w, w.pack_info()) for w in (
                mode_label, self.granularity_switcher,
                date_label, self.date_from_menu,
                between_label, self.date_to_menu,
            )
        ]

        # 顯示名字 checkbox — applies to BOTH 全公會 and 裝評變化, so it
        # lives in prog_row directly (outside _prog_only_frame) and stays
        # interactive on either tab.
        self.show_names_var = ctk.BooleanVar(value=False)
        self.show_names_chk = ctk.CTkCheckBox(
            prog_row,
            text="顯示名字",
            variable=self.show_names_var,
            command=self._on_show_names_change,
        )
        self.show_names_chk.pack(side="left", padx=(20, 0))

        # Once tk has laid out the toolbar, lock the prog-only frame at
        # its natural width so hiding its children later doesn't pull
        # the checkbox leftward (which would also shrink the window
        # vertically when the row becomes shorter than the checkbox
        # alone needs). after_idle fires AFTER the first paint pass so
        # winfo_reqwidth has the correct value.
        self.after_idle(self._freeze_prog_only_size)

        # ----- matplotlib canvas ----------------------------------------
        self.fig = Figure(figsize=(9.0, 5.5), dpi=100)
        self.ax = self.fig.add_subplot(1, 1, 1)
        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=12, pady=(4, 4))
        self.canvas.mpl_connect("motion_notify_event", self._on_hover)

        # ----- hover readout --------------------------------------------
        self.hover_label = ctk.CTkLabel(
            self,
            text="（將滑鼠移到散點上以查看成員名稱）",
            text_color=("#444444", "#bbbbbb"),
            font=ctk.CTkFont(size=12),
        )
        self.hover_label.pack(fill="x", padx=12, pady=(2, 4))

        ctk.CTkButton(
            self, text="關閉", width=120, command=self.destroy,
        ).pack(pady=(2, 12))

        self._draw()

    # ------------------------------------------------------------ filters

    def _compute_gear_buckets(self) -> dict[str, tuple[int, int]]:
        """Pick sensible round-number gear-score buckets from current data.

        The step size adapts to the observed span so the menu lands at
        roughly 4–6 buckets regardless of how wide the guild's range
        is. Bucket boundaries are inclusive-lo / exclusive-hi.
        """
        values = [
            r.peak_gear_score for r in self.workbook.records if r.peak_gear_score
        ]
        if not values:
            return {}
        lo, hi = min(values), max(values)
        span = max(hi - lo, 1)
        if span <= 40_000:
            step = 10_000
        elif span <= 100_000:
            step = 20_000
        elif span <= 250_000:
            step = 50_000
        elif span <= 600_000:
            step = 100_000
        elif span <= 1_500_000:
            step = 250_000
        else:
            step = 500_000
        start = (lo // step) * step
        end = ((hi // step) + 1) * step
        buckets: dict[str, tuple[int, int]] = {}
        for i in range((end - start) // step):
            b_lo = start + i * step
            b_hi = start + (i + 1) * step
            buckets[f"{b_lo:,} – {b_hi:,}"] = (b_lo, b_hi)
        return buckets

    def _on_filter_change(self, _value: str) -> None:
        self._filter_profession = self.profession_filter.get()
        self._filter_gear_range = self.gear_filter.get()
        self._draw()

    def _on_granularity_change(self, value: str) -> None:
        self._granularity = value
        self._draw()

    def _on_date_from_change(self, value: str) -> None:
        self._date_from = value
        # Keep from <= to. If user picks a later "from" than the
        # current "to", auto-bump "to" up to match — easier than
        # validating with a popup mid-interaction.
        if self._date_to and value > self._date_to:
            self._date_to = value
            self.date_to_menu.set(value)
        self._draw()

    def _on_date_to_change(self, value: str) -> None:
        self._date_to = value
        if self._date_from and value < self._date_from:
            self._date_from = value
            self.date_from_menu.set(value)
        self._draw()

    def _on_show_names_change(self) -> None:
        self._show_names = bool(self.show_names_var.get())
        self._draw()

    def _passes_filter(self, rec) -> bool:
        """Return True iff rec satisfies BOTH active filter selections."""
        if self._filter_profession != "全部職業":
            prof = (rec.profession or "").strip() or "未分類"
            if prof != self._filter_profession:
                return False
        if self._filter_gear_range != "全部範圍":
            bucket = self._gear_range_buckets.get(self._filter_gear_range)
            if bucket is None:
                return True
            peak = rec.peak_gear_score or 0
            lo, hi = bucket
            if not (lo <= peak < hi):
                return False
        return True

    # ------------------------------------------------------------ events

    def _freeze_prog_only_size(self) -> None:
        """Pin ``_prog_only_frame``'s natural dimensions.

        Called once after the first paint so winfo_reqwidth returns the
        real size of the inner widgets. From then on, hiding the
        children doesn't shrink the frame — it acts as a reserved
        rectangle that keeps the 顯示名字 checkbox (and the window
        height itself) glued in place across view toggles.
        """
        try:
            self.update_idletasks()
            w = self._prog_only_frame.winfo_reqwidth()
            h = self._prog_only_frame.winfo_reqheight()
            if w <= 1 or h <= 1:
                # Layout not ready yet — try again next idle tick.
                self.after(50, self._freeze_prog_only_size)
                return
            self._prog_only_frame.configure(width=w, height=h)
            self._prog_only_frame.pack_propagate(False)
            # Initial view is 全公會 so the 模式 + 日期 widgets should
            # already be hidden when the dialog first appears. The
            # frozen frame keeps the reserved width regardless.
            if self._view_mode != "裝評變化":
                self._set_prog_only_visible(False)
        except Exception:
            pass

    def _set_prog_only_visible(self, visible: bool) -> None:
        """Show or hide the 裝評變化-only widgets, keeping the toolbar
        width identical so the parent window does not resize."""
        if visible:
            for w, info in self._prog_only_children:
                # Re-pack only if it was actually forgotten. Calling
                # pack on an already-packed widget moves it to the end,
                # which would scramble the order.
                if not w.winfo_ismapped():
                    args = {k: v for k, v in info.items() if k != "in"}
                    w.pack(**args)
        else:
            for w, _info in self._prog_only_children:
                if w.winfo_ismapped():
                    w.pack_forget()

    def _on_view_change(self, value: str) -> None:
        self._view_mode = value
        # 模式 + 日期 controls are only meaningful for 裝評變化. Hide
        # them on 全公會 so the toolbar isn't visually cluttered with
        # inert widgets — but keep the surrounding frame at its frozen
        # size so the window itself doesn't change dimensions.
        self._set_prog_only_visible(value == "裝評變化")
        self._draw()

    def _on_hover(self, event) -> None:
        if event.inaxes != self.ax or not self._scatter_xy:
            return
        if event.x is None or event.y is None:
            return
        try:
            points_pixel = self.ax.transData.transform(self._scatter_xy)
        except Exception:
            return
        best_i = -1
        best_d = float("inf")
        for i, (px, py) in enumerate(points_pixel):
            d = (px - event.x) ** 2 + (py - event.y) ** 2
            if d < best_d:
                best_d = d
                best_i = i
        # ~12 px hit radius — generous so dense clusters still respond.
        if best_i < 0 or best_d > 144:
            self.hover_label.configure(
                text="（將滑鼠移到散點上以查看成員名稱）",
                text_color=("#444444", "#bbbbbb"),
            )
            return
        rec = self._scatter_records[best_i]
        prof = (rec.profession or "").strip() or "未分類"
        id_str = f"#{rec.player_id}" if rec.player_id is not None else "#?"
        name = rec.correct_nickname or "(未填)"
        meta = self._scatter_meta[best_i] if self._scatter_meta else ""
        if meta:
            text = f"{id_str}   {name}   ｜   {prof}   ｜   {meta}"
        else:
            gear = rec.peak_gear_score or 0
            text = f"{id_str}   {name}   ｜   {prof}   ｜   最高裝評 {gear:,}"
        self.hover_label.configure(
            text=text,
            text_color=("#000000", "#ffffff"),
        )

    # ------------------------------------------------------------ drawing

    def _draw(self) -> None:
        self.ax.clear()
        self._scatter_xy = []
        self._scatter_records = []
        self._scatter_meta = []
        if self._view_mode == "全公會":
            self._draw_guild()
        else:
            self._draw_progression()
        self.fig.tight_layout()
        self.canvas.draw_idle()

    def _draw_guild(self) -> None:
        """Strip plot — x = profession, y = peak_gear_score."""
        import matplotlib

        all_recs = [r for r in self.workbook.records if r.peak_gear_score]
        if not all_recs:
            self.ax.text(
                0.5, 0.5, "（沒有任何成員有裝評資料）",
                ha="center", va="center", transform=self.ax.transAxes,
            )
            self.ax.set_axis_off()
            return

        # Canonical colour map built from ALL recs so each profession
        # keeps the same colour regardless of which filter is active.
        all_profs = sorted(
            {(r.profession or "").strip() or "未分類" for r in all_recs},
            key=lambda k: (k == "未分類", k),
        )
        cmap = matplotlib.colormaps.get_cmap("tab10")
        prof_to_colour = {p: cmap(i % 10) for i, p in enumerate(all_profs)}

        recs = [r for r in all_recs if self._passes_filter(r)]
        if not recs:
            self.ax.text(
                0.5, 0.5, "（沒有符合篩選條件的成員）",
                ha="center", va="center", transform=self.ax.transAxes,
            )
            self.ax.set_axis_off()
            return

        by_prof: dict[str, list] = {}
        for r in recs:
            prof = (r.profession or "").strip() or "未分類"
            by_prof.setdefault(prof, []).append(r)

        # x-axis only spans the professions that actually have visible
        # dots — when the user picks a single profession the dots end
        # up centred instead of pinned to the left edge of an 8-tick
        # axis padded with empty columns.
        visible_profs = sorted(by_prof.keys(), key=lambda k: (k == "未分類", k))
        prof_to_x = {p: i for i, p in enumerate(visible_profs)}

        rng = random.Random(42)
        for prof, members in by_prof.items():
            x_idx = prof_to_x[prof]
            xs = []
            ys = []
            for r in members:
                x = x_idx + rng.uniform(-0.22, 0.22)
                y = int(r.peak_gear_score)
                self._scatter_xy.append((x, y))
                self._scatter_records.append(r)
                self._scatter_meta.append("")
                xs.append(x)
                ys.append(y)
            self.ax.scatter(
                xs, ys,
                s=50, alpha=0.75,
                color=prof_to_colour[prof],
                edgecolors="white", linewidths=0.5,
                label=f"{prof} ({len(members)})",
            )
            # Optional name overlay — same checkbox that toggles labels
            # on the 裝評變化 chart. Will overlap heavily for unfiltered
            # views; users are expected to combine this with the
            # 職業 / 裝評範圍 filters when enabling.
            if self._show_names:
                for r, x_val, y_val in zip(members, xs, ys):
                    name = r.correct_nickname or "(未填)"
                    self.ax.annotate(
                        name,
                        xy=(x_val, y_val),
                        xytext=(3, 4),
                        textcoords="offset points",
                        fontsize=8,
                        color=prof_to_colour[prof],
                        alpha=0.9,
                        clip_on=True,
                    )

        self.ax.set_xticks(range(len(visible_profs)))
        self.ax.set_xticklabels(visible_profs, rotation=15, ha="right")
        # Pad the x-axis a bit so single-profession views aren't a
        # razor-thin column hugging the y-axis. ±0.5 around the lone
        # tick mirrors how matplotlib pads multi-tick categorical axes.
        if visible_profs:
            self.ax.set_xlim(-0.5, len(visible_profs) - 0.5)
        self.ax.set_ylabel("最高裝評")
        self.ax.set_title("全公會裝評分布（每點 = 一位成員）", fontsize=12)
        self.ax.grid(axis="y", linestyle="--", alpha=0.4)
        self.ax.yaxis.set_major_formatter(lambda v, _p: f"{int(v):,}")
        if len(by_prof) > 1:
            self.ax.legend(fontsize=9, loc="upper right", framealpha=0.85)

    @staticmethod
    def _week_key(day_str: str) -> str:
        """Return ``"YYYY-Www"`` ISO key for grouping captures by week."""
        try:
            d = datetime.strptime(day_str, "%Y-%m-%d").date()
        except ValueError:
            return day_str
        iso_year, iso_week, _ = d.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"

    @staticmethod
    def _week_label(week_key: str) -> str:
        """Render a week key as ``"M/D~M/D"`` (Mon~Sun of that ISO week)."""
        try:
            year_str, w_str = week_key.split("-W")
            iso_year, iso_week = int(year_str), int(w_str)
        except ValueError:
            return week_key
        # ISO week 1 = the week containing the first Thursday. The Monday
        # of any ISO week can be computed via fromisocalendar (3.8+).
        try:
            start: date = date.fromisocalendar(iso_year, iso_week, 1)
        except ValueError:
            return week_key
        end = start + timedelta(days=6)
        return f"{start.month}/{start.day}~{end.month}/{end.day}"

    def _draw_progression(self) -> None:
        """Per-member trajectory: x = capture day (or week), y = gear score.

        Each member becomes one thin line through the days where they
        have a recorded gear score. Members are colour-coded by
        profession. Granularity, date range and name-overlay are all
        driven by the controls in the top bar (``_granularity``,
        ``_date_from`` / ``_date_to``, ``_show_names``).
        """
        import matplotlib

        all_days = list(self.workbook.capture_days)
        all_recs = [r for r in self.workbook.records if r.gear_scores]
        if not all_days or not all_recs:
            self.ax.text(
                0.5, 0.5, "（沒有足夠的日期紀錄畫變化圖）",
                ha="center", va="center", transform=self.ax.transAxes,
            )
            self.ax.set_axis_off()
            return

        # Apply the date-range clip first. The dropdown values are the
        # workbook's own capture_days strings so lex compare == date
        # compare (ISO-8601).
        d_from = self._date_from or all_days[0]
        d_to = self._date_to or all_days[-1]
        days = [d for d in all_days if d_from <= d <= d_to]
        if not days:
            self.ax.text(
                0.5, 0.5, "（選取的日期範圍內沒有紀錄）",
                ha="center", va="center", transform=self.ax.transAxes,
            )
            self.ax.set_axis_off()
            return

        # Canonical profession→colour map (built from ALL recs) so the
        # legend colours stay consistent regardless of filter state.
        all_profs = sorted(
            {(r.profession or "").strip() or "未分類" for r in all_recs},
            key=lambda k: (k == "未分類", k),
        )
        cmap = matplotlib.colormaps.get_cmap("tab10")
        prof_colour = {p: cmap(i % 10) for i, p in enumerate(all_profs)}

        recs = [r for r in all_recs if self._passes_filter(r)]
        if not recs:
            self.ax.text(
                0.5, 0.5, "（沒有符合篩選條件的成員）",
                ha="center", va="center", transform=self.ax.transAxes,
            )
            self.ax.set_axis_off()
            return

        # Build the x-axis. In 日線圖 each capture day is one tick; in
        # 週線圖 days collapse to ISO weeks and each member's value
        # for the week is the MAX over their captures that week.
        if self._granularity == "週線圖":
            # Preserve the chronological order of distinct weeks present
            # in the clipped day range.
            seen: dict[str, None] = {}
            for d in days:
                seen[self._week_key(d)] = None
            x_keys = list(seen.keys())
            x_labels = [self._week_label(k) for k in x_keys]
        else:
            x_keys = list(days)
            x_labels = []
            for d in days:
                try:
                    _, m, dy = d.split("-")
                    x_labels.append(f"{int(m)}/{int(dy)}")
                except ValueError:
                    x_labels.append(d)
        key_to_x = {k: i for i, k in enumerate(x_keys)}

        # Map (member → {x_index: max_gear}) honouring both the clip
        # and the granularity. Sample iteration is O(days × members)
        # which is fine for the project's ~150 members × ~30 days.
        member_samples: dict[int, list[tuple[int, int]]] = {}
        for rec_idx, r in enumerate(recs):
            buckets: dict[int, int] = {}
            for d, v in r.gear_scores.items():
                if not (d_from <= d <= d_to):
                    continue
                key = self._week_key(d) if self._granularity == "週線圖" else d
                x_i = key_to_x.get(key)
                if x_i is None:
                    continue
                # In 週線圖 mode multiple captures can land in the same
                # week — keep the max.
                if x_i not in buckets or v > buckets[x_i]:
                    buckets[x_i] = v
            if buckets:
                member_samples[rec_idx] = sorted(buckets.items())

        # Professions actually shown after filtering — drives the legend.
        profs = sorted(
            {(r.profession or "").strip() or "未分類" for r in recs},
            key=lambda k: (k == "未分類", k),
        )

        for rec_idx, samples in member_samples.items():
            r = recs[rec_idx]
            prof = (r.profession or "").strip() or "未分類"
            colour = prof_colour[prof]
            xs = [s[0] for s in samples]
            ys = [s[1] for s in samples]
            # Thin connecting line — low alpha so 150 lines stay legible.
            self.ax.plot(xs, ys, color=colour, linewidth=0.9, alpha=0.45)
            # Scatter the actual sample points so hover can latch on.
            self.ax.scatter(
                xs, ys, s=30, alpha=0.75,
                color=colour, edgecolors="white", linewidths=0.4,
            )
            for x_i, y_i in samples:
                self._scatter_xy.append((x_i, y_i))
                self._scatter_records.append(r)
                tick_label = x_labels[x_i]
                # 週線圖 hover text reflects "this is the week max".
                if self._granularity == "週線圖":
                    self._scatter_meta.append(f"{tick_label} 週最高 {y_i:,}")
                else:
                    self._scatter_meta.append(f"{tick_label} 裝評 {y_i:,}")

            # Optional in-chart name annotation. We anchor each label
            # slightly above-right of the point so adjacent lines don't
            # overlap the text. With many members enabled labels WILL
            # stack; that's why this is opt-in and the user is expected
            # to combine it with the filter dropdowns.
            if self._show_names:
                name = r.correct_nickname or "(未填)"
                for x_i, y_i in samples:
                    self.ax.annotate(
                        name,
                        xy=(x_i, y_i),
                        xytext=(3, 4),
                        textcoords="offset points",
                        fontsize=8,
                        color=colour,
                        alpha=0.9,
                        clip_on=True,
                    )

        self.ax.set_xticks(range(len(x_keys)))
        # Rotate weekly labels more aggressively since "5/19~5/25" runs
        # wider than the daily "5/19" form.
        rotation = 25 if self._granularity == "週線圖" else 20
        self.ax.set_xticklabels(x_labels, rotation=rotation)
        # Mirror the 全公會 padding trick — when only one tick remains
        # (single day or single week) the lone scatter point would
        # otherwise hug the y-axis.
        if x_keys:
            self.ax.set_xlim(-0.5, len(x_keys) - 0.5)
        self.ax.set_ylabel("裝評")
        title_suffix = "週最大值" if self._granularity == "週線圖" else "每日"
        self.ax.set_title(
            f"成員裝評變化（每條線 = 一位成員，{title_suffix}，依職業著色）",
            fontsize=12,
        )
        self.ax.grid(axis="y", linestyle="--", alpha=0.4)
        self.ax.yaxis.set_major_formatter(lambda v, _p: f"{int(v):,}")
        # Legend by profession (one swatch per colour, not per member).
        from matplotlib.lines import Line2D
        legend_items = [
            Line2D([0], [0], color=prof_colour[p], lw=2, label=p)
            for p in profs
        ]
        if legend_items and len(profs) > 1:
            self.ax.legend(
                handles=legend_items, fontsize=9,
                loc="upper left", framealpha=0.85,
            )
