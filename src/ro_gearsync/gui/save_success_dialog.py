"""Small modal shown after the user confirms the post-scan review and
the workbook has been written to disk.

We hand-roll this instead of using ``tkinter.messagebox.showinfo``
because the user wants the "精煉都會上，打怪掉紅裝！" line to stand
out — bigger font, bold weight, and a saturated colour. The built-in
messagebox can't do styled rich text.

Used by :class:`RoGearSyncApp._on_review_confirmed`.
"""
from __future__ import annotations

from typing import Callable

import customtkinter as ctk


# Red per spec — RO "紅裝" reference, more festive than the green
# +delta colour used elsewhere. Matches COLOR_DOWN in app.py.
_BLESSING_COLOR = "#c1351c"
_BLESSING_LINE = "精煉都會上，打怪掉紅裝！"


class SaveSuccessDialog(ctk.CTkToplevel):
    """A blocking-ish "saved successfully" toast with a styled blessing."""

    def __init__(
        self,
        master: ctk.CTk,
        *,
        workbook_path: str,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(master)
        self.on_close = on_close

        self.title("已寫入 Excel")
        self.geometry("520x300")
        self.resizable(False, False)
        # Centred relative to the parent window — Toplevel doesn't auto-
        # centre on Windows so we do it manually.
        try:
            self.update_idletasks()
            mx = master.winfo_rootx() + master.winfo_width() // 2
            my = master.winfo_rooty() + master.winfo_height() // 2
            self.geometry(f"+{mx - 260}+{my - 150}")
        except Exception:
            pass
        self.transient(master)
        # No grab_set — keep the success toast non-modal so the user
        # can still browse the live table beneath while reading it.

        self._build_ui(workbook_path)
        self.protocol("WM_DELETE_WINDOW", self._dismiss)

        # ``transient(master)`` keeps the toast above the MAIN app, but
        # the review dialog is also a Toplevel of the main app and
        # would otherwise sit on top of (and obscure) this success
        # message. ``-topmost`` plus an explicit lift / focus_force
        # guarantees the toast surfaces above the review dialog even
        # when the review dialog has ``-topmost`` of its own.
        try:
            self.attributes("-topmost", True)
        except Exception:
            pass
        try:
            self.lift()
            # after_idle schedules the focus once the window has
            # actually been mapped — calling focus_force on an
            # unmapped Toplevel is a no-op on Windows.
            self.after_idle(self._focus_to_front)
        except Exception:
            pass

    def _focus_to_front(self) -> None:
        try:
            self.lift()
            self.focus_force()
        except Exception:
            pass

    def _build_ui(self, workbook_path: str) -> None:
        self.grid_columnconfigure(0, weight=1)

        # ✅ headline — green (matches "completed" status everywhere
        # else; the blessing colour below is red per spec).
        ctk.CTkLabel(
            self,
            text="✅  已成功寫入 Excel",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color="#1d8a3d",
        ).grid(row=0, column=0, padx=24, pady=(24, 4))

        # Workbook path subtitle
        ctk.CTkLabel(
            self,
            text=workbook_path,
            font=ctk.CTkFont(size=11),
            text_color=("#666666", "#aaaaaa"),
            wraplength=460,
        ).grid(row=1, column=0, padx=24, pady=(0, 18))

        # Thanks line
        ctk.CTkLabel(
            self,
            text="感謝您的寶貴時間，",
            font=ctk.CTkFont(size=14),
        ).grid(row=2, column=0, padx=24, pady=(0, 4))

        # "祝您" + emphasised blessing on the same logical row.
        # Two separate labels stacked so the blessing line can be both
        # larger and a different colour from the leading 祝您.
        ctk.CTkLabel(
            self,
            text="祝您",
            font=ctk.CTkFont(size=14),
        ).grid(row=3, column=0, padx=24, pady=(0, 2))

        ctk.CTkLabel(
            self,
            text=_BLESSING_LINE,
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color=_BLESSING_COLOR,
        ).grid(row=4, column=0, padx=24, pady=(0, 18))

        ctk.CTkButton(
            self, text="關閉", width=120,
            command=self._dismiss,
        ).grid(row=5, column=0, padx=24, pady=(0, 20))

    def _dismiss(self) -> None:
        if self.on_close is not None:
            try:
                self.on_close()
            except Exception:
                pass
        self.destroy()
