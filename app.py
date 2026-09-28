"""
GifConverter - Interface grafica (arrastar e soltar), com visual proprio.

Janela sem a moldura padrao do Windows: barra de titulo customizada e
arrastavel, widgets estilizados e botao de tema (Dark / Cyberpunk).

Requer FFmpeg (embutido no .exe ou no PATH). Arrastar-e-soltar precisa do
pacote 'tkinterdnd2'; sem ele, clique na area para escolher o arquivo.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk
from tkinter import font as tkfont

import tempfile

from converter import (
    ConversionCancelled,
    ConversionError,
    convert_gif,
    extract_frame,
    extract_thumb,
    find_ffmpeg,
    probe_duration,
)

# Arrastar-e-soltar e opcional.
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _DND_OK = True
except Exception:  # noqa: BLE001
    _DND_OK = False


# Formatos de entrada aceitos (GIF ou video).
INPUT_EXTS = (".gif", ".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v", ".gifv")


# ---------------------------------------------------------------------------
# Temas
# ---------------------------------------------------------------------------
THEMES = {
    "dark": {
        "label": "Dark",
        "font": "Segoe UI",
        "mono": "Consolas",
        "border": "#3a3a48",
        "bg": "#1b1b22",
        "panel": "#24242e",
        "field": "#2e2e3a",
        "fg": "#eaeaf0",
        "muted": "#9a9ab0",
        "accent": "#7c8cff",
        "accent_hi": "#9aa6ff",
        "accent_fg": "#0d0d14",
        "accent2": "#4de0c8",
        "warn": "#f0b23a",
        "danger": "#ff6b6b",
        "ok": "#5fd07a",
        "drop_bg": "#20202a",
        "titlebar": "#15151b",
        "glow": "#7c8cff",
    },
    "cyberpunk": {
        "label": "Cyberpunk",
        "font": "Consolas",
        "mono": "Consolas",
        "border": "#ff2ec4",
        "bg": "#0b0118",
        "panel": "#150430",
        "field": "#1e0a3c",
        "fg": "#f3e9ff",
        "muted": "#9d7bd8",
        "accent": "#ff2ec4",
        "accent_hi": "#ff67d6",
        "accent_fg": "#0b0118",
        "accent2": "#00f0ff",
        "warn": "#ffd23e",
        "danger": "#ff3b6b",
        "ok": "#00f0a8",
        "drop_bg": "#12042a",
        "titlebar": "#0a0114",
        "glow": "#00f0ff",
    },
}


def _round_rect_points(x1, y1, x2, y2, r):
    """Pontos de um retangulo arredondado para create_polygon(smooth=True)."""
    r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
    return [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
        x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]


def _round_window(hwnd) -> None:
    """Arredonda os cantos da janela (Windows 11, via DWM)."""
    if os.name != "nt" or not hwnd:
        return
    try:
        import ctypes
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_ROUND = 2
        val = ctypes.c_int(DWMWCP_ROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(val),
            ctypes.sizeof(val))
    except Exception:  # noqa: BLE001
        pass


class RoundButton(tk.Canvas):
    """Botao com cantos arredondados desenhado em Canvas (o tk.Button e reto)."""

    def __init__(self, parent, text="", command=None, radius=12,
                 font=("Segoe UI", 10), padx=16, pady=9, expand_text=False):
        super().__init__(parent, bd=0, highlightthickness=0, takefocus=0)
        self._text = text
        self._cmd = command
        self._radius = radius
        self._font = font
        self._padx = padx
        self._pady = pady
        self._state = "normal"
        self._hover = False
        self._bg = self._fg = self._hbg = self._hfg = "#333"
        self._dis_fg = "#777"
        f = tkfont.Font(font=font)
        self._th = f.metrics("linespace")
        self._min_w = f.measure(text) + 2 * padx
        h = self._th + 2 * pady
        self.configure(width=self._min_w, height=h)
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)

    # API parecida com tk.Button ----------------------------------------
    def configure(self, **kw):
        if "state" in kw:
            self._state = kw.pop("state")
        if "text" in kw:
            self._text = kw.pop("text")
            self._min_w = tkfont.Font(font=self._font).measure(self._text) + 2 * self._padx
        if kw:
            super().configure(**kw)
        self._draw()
    config = configure

    def __getitem__(self, k):
        if k == "state":
            return self._state
        return super().__getitem__(k)

    def apply_theme(self, t, kind, container_key="bg"):
        base = t[container_key]
        if kind == "accent":
            self._bg, self._fg, self._hbg, self._hfg = (
                t["accent"], t["accent_fg"], t["accent_hi"], t["accent_fg"])
        elif kind == "chip":
            self._bg, self._fg, self._hbg, self._hfg = (
                t["panel"], t["accent2"], t["field"], t["accent"])
        elif kind == "danger":
            self._bg, self._fg, self._hbg, self._hfg = (
                t["panel"], t["danger"], t["danger"], t["accent_fg"])
        else:  # ghost
            self._bg, self._fg, self._hbg, self._hfg = (
                t["panel"], t["fg"], t["field"], t["accent2"])
        self._dis_fg = t["muted"]
        super().configure(bg=base)  # canvas bg = fundo do container (cantos)
        self._draw()

    # interacao ---------------------------------------------------------
    def _on_enter(self, e):
        if self._state != "disabled":
            self._hover = True
            self._draw()

    def _on_leave(self, e):
        self._hover = False
        self._draw()

    def _on_click(self, e):
        if self._state != "disabled" and self._cmd:
            self._cmd()

    def _draw(self):
        self.delete("all")
        w = self.winfo_width() or self._min_w
        h = self.winfo_height() or (self._th + 2 * self._pady)
        disabled = self._state == "disabled"
        fill = self._hbg if (self._hover and not disabled) else self._bg
        txt = self._dis_fg if disabled else (
            self._hfg if self._hover else self._fg)
        self.create_polygon(_round_rect_points(1, 1, w - 1, h - 1, self._radius),
                            smooth=True, fill=fill, outline=fill)
        self.create_text(w // 2, h // 2, text=self._text, fill=txt,
                         font=self._font)


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.selected_file: str | None = None
        self.last_output: str | None = None
        self.cancel_event = None
        self.current_proc = None
        self.theme_name = "dark"
        self._buttons: list[tuple[tk.Button, str]] = []

        root.title("GifConverter")
        try:
            root.overrideredirect(True)  # remove a moldura nativa
            self._borderless = True
        except tk.TclError:
            self._borderless = False

        # janela centralizada
        w, h = 600, 820
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 3)}")

        # borda externa (moldura fina colorida) -> painel interno
        self.outer = tk.Frame(root, bd=0)
        self.outer.pack(fill="both", expand=True)
        self.shell = tk.Frame(self.outer, bd=0)
        self.shell.pack(fill="both", expand=True, padx=2, pady=2)

        self._maximized = False
        self._restore_geom = None

        self._build_titlebar()
        self._build_body()

        self.apply_theme(self.theme_name)
        self._check_ffmpeg()

        # reativa o icone na barra de tarefas (perdido com overrideredirect)
        if self._borderless:
            root.after(60, self._enable_taskbar)

    # ---------- construcao da UI ----------

    def _build_titlebar(self) -> None:
        bar = tk.Frame(self.shell, height=44)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        self.titlebar = bar

        self.title_lbl = tk.Label(
            bar, text="◆  GIFCONVERTER", font=(THEMES["dark"]["font"], 11, "bold")
        )
        self.title_lbl.pack(side="left", padx=14)

        # botoes de janela (direita): min | max | close
        self.close_btn = tk.Button(
            bar, text="✕", command=self.root.destroy, bd=0, cursor="hand2",
            width=3, font=("Segoe UI", 11),
        )
        self.close_btn.pack(side="right", padx=(0, 8))
        self.max_btn = tk.Button(
            bar, text="□", command=self._toggle_max, bd=0, cursor="hand2",
            width=3, font=("Segoe UI", 10),
        )
        self.max_btn.pack(side="right")
        self.min_btn = tk.Button(
            bar, text="—", command=self._minimize, bd=0, cursor="hand2",
            width=3, font=("Segoe UI", 11),
        )
        self.min_btn.pack(side="right")

        # botao de tema (arredondado)
        self.theme_btn = RoundButton(
            bar, text="◑ Cyberpunk", command=self._toggle_theme,
            font=("Segoe UI", 9, "bold"), radius=13, padx=12, pady=6)
        self.theme_btn._container = "titlebar"
        self.theme_btn.pack(side="right", padx=10, pady=8)
        self._buttons.append((self.theme_btn, "chip"))

        # arrastar a janela pela barra (duplo-clique maximiza/restaura)
        for widget in (bar, self.title_lbl):
            widget.bind("<Button-1>", self._start_move)
            widget.bind("<B1-Motion>", self._do_move)
            widget.bind("<Double-Button-1>", lambda e: self._toggle_max())

    def _build_body(self) -> None:
        main = tk.Frame(self.shell)
        main.pack(fill="both", expand=True, padx=22, pady=(6, 16))
        self.main = main

        self.h1 = tk.Label(main, text="GIF  →  mais FPS", font=(THEMES["dark"]["font"], 20, "bold"))
        self.h1.pack(anchor="w")
        self.h2 = tk.Label(
            main,
            text="Interpolacao + upscaling  ·  FFmpeg · RIFE · Real-ESRGAN",
            font=(THEMES["dark"]["font"], 9),
        )
        self.h2.pack(anchor="w", pady=(2, 16))

        # area de drop (painel arredondado em Canvas)
        self._drop_msg = ("Arraste um GIF ou video aqui\n\nou clique para escolher"
                          if _DND_OK else "Clique para escolher um GIF ou video")
        self._drop_fg_key = "muted"
        self.drop = tk.Canvas(main, height=120, bd=0, highlightthickness=0,
                              cursor="hand2")
        self.drop.pack(fill="x", pady=(0, 16))
        self.drop.bind("<Button-1>", lambda e: self.choose_file())
        self.drop.bind("<Configure>", lambda e: self._draw_drop())
        if _DND_OK:
            self.drop.drop_target_register(DND_FILES)
            self.drop.dnd_bind("<<Drop>>", self._on_drop)

        # opcoes (grade de pares rotulo/campo)
        opts = tk.Frame(main)
        opts.pack(fill="x")
        self.opts = opts
        for c in (0, 2):
            opts.columnconfigure(c, weight=0)
        opts.columnconfigure(1, weight=1)
        opts.columnconfigure(3, weight=1)

        self._labels: list[tk.Label] = []
        self._combos: list[ttk.Combobox] = []
        self._entries: list[tk.Entry] = []

        def combo(row, col, text, var, values, width=10, state="readonly"):
            lbl = tk.Label(opts, text=text, font=(THEMES["dark"]["font"], 9))
            lbl.grid(row=row, column=col, sticky="w", padx=(0, 8), pady=7)
            self._labels.append(lbl)
            cb = ttk.Combobox(opts, textvariable=var, values=values, width=width,
                              state=state, style="GC.TCombobox")
            cb.grid(row=row, column=col + 1, sticky="ew", pady=7, padx=(0, 14))
            self._combos.append(cb)
            return cb

        def entry(row, col, text, var, width=10):
            lbl = tk.Label(opts, text=text, font=(THEMES["dark"]["font"], 9))
            lbl.grid(row=row, column=col, sticky="w", padx=(0, 8), pady=7)
            self._labels.append(lbl)
            en = tk.Entry(opts, textvariable=var, width=width, bd=0,
                          relief="flat", font=(THEMES["dark"]["font"], 10),
                          highlightthickness=1)
            en.grid(row=row, column=col + 1, sticky="ew", pady=7, padx=(0, 14),
                    ipady=3)
            self._entries.append(en)
            return en

        self.fps_var = tk.IntVar(value=60)
        combo(0, 0, "FPS", self.fps_var, [24, 30, 48, 50, 60, 90, 120], 8)
        self.quality_var = tk.StringVar(value="alta")
        combo(0, 2, "Qualidade", self.quality_var, ["alta", "rapida"], 8)

        self.format_var = tk.StringVar(value="mp4")
        fc = combo(1, 0, "Formato", self.format_var, ["mp4", "webm", "gif"], 8)
        fc.bind("<<ComboboxSelected>>", self._on_format_change)
        self.upscale_var = tk.StringVar(value="1x")
        combo(1, 2, "Upscale", self.upscale_var, ["1x", "2x", "3x", "4x"], 8)

        self.engine_var = tk.StringVar(value="classico")
        ec = combo(2, 0, "Upscaler", self.engine_var, ["classico", "ia"], 8)
        ec.bind("<<ComboboxSelected>>", self._on_engine_change)
        self.model_var = tk.StringVar(value="anime")
        self.model_combo = combo(2, 2, "Modelo IA", self.model_var,
                                 ["anime", "foto", "ilustracao"], 8, state="disabled")

        self.interp_var = tk.StringVar(value="ffmpeg")
        ic = combo(3, 0, "Interpolar", self.interp_var, ["ffmpeg", "rife"], 8)
        ic.bind("<<ComboboxSelected>>", self._on_interp_change)

        self.width_var = tk.StringVar(value="Original")
        combo(3, 2, "Largura max", self.width_var,
              ["Original", "240", "320", "480", "640", "800"], 8)

        # corte (opcional)
        self.start_var = tk.StringVar(value="0")
        entry(4, 0, "Inicio (s)", self.start_var, 8)
        self.dur_var = tk.StringVar(value="")
        entry(4, 2, "Duracao (s)", self.dur_var, 8)

        self.cut_btn = RoundButton(
            opts, text="✂  Editar corte (visual)", command=self.open_trimmer,
            font=(THEMES["dark"]["font"], 9), radius=13, pady=7, padx=12)
        self.cut_btn.configure(state="disabled")
        self.cut_btn.grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self._buttons.append((self.cut_btn, "chip"))

        self.trim_note = tk.Label(
            opts, text="Duracao vazia = ate o fim. Largura reduz so se maior.",
            font=(THEMES["dark"]["font"], 8),
        )
        self.trim_note.grid(row=6, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self._labels.append(self.trim_note)

        # nota
        self.format_note = tk.Label(
            main,
            text="MP4/WebM = 60fps reais. GIF nao passa de ~50fps.",
            font=(THEMES["dark"]["font"], 8), anchor="w", justify="left",
            wraplength=540,
        )
        self.format_note.pack(fill="x", pady=(10, 14))

        # botoes de acao (arredondados)
        actions = tk.Frame(main)
        actions.pack(fill="x")
        self.convert_btn = RoundButton(
            actions, text="⚡  CONVERTER", command=self.start_conversion,
            font=(THEMES["dark"]["font"], 11, "bold"), radius=14, pady=11)
        self.convert_btn.configure(state="disabled")
        self.convert_btn.pack(side="left", fill="x", expand=True)
        self._buttons.append((self.convert_btn, "accent"))

        self.cancel_btn = RoundButton(
            actions, text="Cancelar", command=self.cancel_conversion,
            font=(THEMES["dark"]["font"], 9, "bold"), radius=14, pady=11, padx=14)
        self.cancel_btn.configure(state="disabled")
        self.cancel_btn.pack(side="left", padx=(8, 0))
        self._buttons.append((self.cancel_btn, "danger"))

        self.open_btn = RoundButton(
            actions, text="Abrir arquivo", command=self.open_output,
            font=(THEMES["dark"]["font"], 9), radius=14, pady=11, padx=12)
        self.open_btn.configure(state="disabled")
        self.open_btn.pack(side="left", padx=(8, 0))
        self._buttons.append((self.open_btn, "ghost"))

        self.folder_btn = RoundButton(
            actions, text="Pasta", command=self.open_folder,
            font=(THEMES["dark"]["font"], 9), radius=14, pady=11, padx=12)
        self.folder_btn.configure(state="disabled")
        self.folder_btn.pack(side="left", padx=(8, 0))
        self._buttons.append((self.folder_btn, "ghost"))

        # progresso (barra determinada + porcentagem)
        progbox = tk.Frame(main)
        progbox.pack(fill="x", pady=(14, 8))
        self.progress = ttk.Progressbar(progbox, mode="determinate", maximum=100,
                                        style="GC.Horizontal.TProgressbar")
        self.progress.pack(side="left", fill="x", expand=True)
        self.pct_label = tk.Label(progbox, text="0%", width=6,
                                  font=(THEMES["dark"]["mono"], 10, "bold"))
        self.pct_label.pack(side="left", padx=(10, 0))

        # status
        self.status = tk.Label(main, text="", font=(THEMES["dark"]["font"], 9),
                               anchor="w", justify="left", wraplength=540)
        self.status.pack(fill="x")

        # rodape / assinatura
        self.footer = tk.Label(main, text="Rafael Martins · 2026",
                               font=(THEMES["dark"]["font"], 8))
        self.footer.pack(side="bottom", anchor="e", pady=(10, 0))

    # ---------- tema ----------

    def _draw_drop(self) -> None:
        if not hasattr(self, "T"):
            return
        t = self.T
        c = self.drop
        c.delete("all")
        c.configure(bg=t["bg"])
        w = c.winfo_width() or 520
        h = c.winfo_height() or 120
        # painel arredondado com contorno em destaque
        c.create_polygon(_round_rect_points(3, 3, w - 3, h - 3, 20), smooth=True,
                         fill=t["drop_bg"], outline=t["accent"], width=2)
        c.create_text(w // 2, h // 2, text=self._drop_msg,
                      fill=t[self._drop_fg_key], font=(t["mono"], 12),
                      justify="center")

    def _toggle_theme(self) -> None:
        self.theme_name = "cyberpunk" if self.theme_name == "dark" else "dark"
        self.apply_theme(self.theme_name)

    def apply_theme(self, name: str) -> None:
        t = THEMES[name]
        self.T = t
        f, mono = t["font"], t["mono"]

        self.outer.configure(bg=t["border"])
        self.shell.configure(bg=t["bg"])
        self.titlebar.configure(bg=t["titlebar"])
        self.title_lbl.configure(bg=t["titlebar"], fg=t["accent"], font=(f, 11, "bold"))
        self.main.configure(bg=t["bg"])
        self.opts.configure(bg=t["bg"])

        # botao proximo tema mostra o OUTRO tema
        nxt = "Cyberpunk" if name == "dark" else "Dark"
        self.theme_btn.configure(text=f"◑ {nxt}")

        # janela: min/max/close
        for b in (self.min_btn, self.max_btn, self.close_btn):
            b.configure(bg=t["titlebar"], fg=t["muted"],
                        activebackground=t["panel"], activeforeground=t["fg"])
        self._hover(self.close_btn, t["titlebar"], t["danger"], t["accent_fg"])
        self._hover(self.min_btn, t["titlebar"], t["panel"], t["fg"])
        self._hover(self.max_btn, t["titlebar"], t["panel"], t["fg"])

        # titulos
        self.h1.configure(bg=t["bg"], fg=t["fg"], font=(f, 20, "bold"))
        self.h2.configure(bg=t["bg"], fg=t["muted"], font=(f, 9))

        # drop (painel arredondado)
        self._draw_drop()

        # rotulos e nota/status/footer
        for lbl in self._labels:
            lbl.configure(bg=t["bg"], fg=t["muted"], font=(f, 9))
        for en in self._entries:
            en.configure(bg=t["field"], fg=t["fg"], insertbackground=t["accent"],
                         disabledbackground=t["field"], disabledforeground=t["muted"],
                         highlightbackground=t["border"], highlightcolor=t["accent"])
        self.format_note.configure(bg=t["bg"], fg=t["muted"], font=(f, 8))
        self.footer.configure(bg=t["bg"], fg=t["muted"], font=(f, 8))
        self.pct_label.configure(bg=t["bg"], fg=t["accent"], font=(mono, 10, "bold"))
        # status mantem cor semantica corrente
        cur = self.status.cget("text")
        self.status.configure(bg=t["bg"])
        if not cur:
            self.status.configure(fg=t["muted"])

        # botoes estilizados
        for b, kind in self._buttons:
            self._style_button(b, kind)

        # widgets ttk (combobox + progressbar) via style clam
        self._style_ttk(t)

        # cor de fundo dos frames de acao
        for child in self.main.winfo_children():
            if isinstance(child, tk.Frame):
                child.configure(bg=t["bg"])

    def _style_button(self, b, kind: str) -> None:
        t = self.T
        if isinstance(b, RoundButton):
            b.apply_theme(t, kind, getattr(b, "_container", "bg"))
            return
        if kind == "accent":
            b.configure(bg=t["accent"], fg=t["accent_fg"],
                        activebackground=t["accent_hi"], activeforeground=t["accent_fg"])
            self._hover(b, t["accent"], t["accent_hi"], t["accent_fg"])
        elif kind == "ghost":
            b.configure(bg=t["panel"], fg=t["fg"],
                        activebackground=t["field"], activeforeground=t["accent2"])
            self._hover(b, t["panel"], t["field"], t["accent2"])
        elif kind == "chip":
            b.configure(bg=t["panel"], fg=t["accent2"],
                        activebackground=t["field"], activeforeground=t["accent"])
            self._hover(b, t["panel"], t["field"], t["accent"])
        elif kind == "danger":
            b.configure(bg=t["panel"], fg=t["danger"],
                        activebackground=t["danger"], activeforeground=t["accent_fg"])
            self._hover(b, t["panel"], t["danger"], t["accent_fg"])

    def _hover(self, b: tk.Button, base: str, hi: str, hi_fg: str) -> None:
        # (re)liga eventos de hover, guardando as cores atuais
        b.unbind("<Enter>")
        b.unbind("<Leave>")
        base_fg = b.cget("fg")
        b.bind("<Enter>", lambda e: b.configure(bg=hi, fg=hi_fg)
               if str(b["state"]) != "disabled" else None)
        b.bind("<Leave>", lambda e: b.configure(bg=base, fg=base_fg)
               if str(b["state"]) != "disabled" else None)

    def _style_ttk(self, t: dict) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "GC.TCombobox",
            fieldbackground=t["field"], background=t["field"], foreground=t["fg"],
            arrowcolor=t["accent"], bordercolor=t["border"], lightcolor=t["border"],
            darkcolor=t["border"], selectbackground=t["field"],
            selectforeground=t["fg"], padding=5,
        )
        style.map("GC.TCombobox",
                  fieldbackground=[("readonly", t["field"])],
                  foreground=[("disabled", t["muted"])],
                  arrowcolor=[("disabled", t["muted"])])
        # lista suspensa da combobox
        self.root.option_add("*TCombobox*Listbox.background", t["field"])
        self.root.option_add("*TCombobox*Listbox.foreground", t["fg"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", t["accent"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", t["accent_fg"])
        style.configure("GC.Horizontal.TProgressbar",
                        troughcolor=t["panel"], background=t["accent"],
                        bordercolor=t["panel"], lightcolor=t["accent"],
                        darkcolor=t["accent"])

    # ---------- janela sem moldura ----------

    def _start_move(self, e) -> None:
        self._mx, self._my = e.x, e.y

    def _do_move(self, e) -> None:
        if self._maximized:
            return  # nao arrasta maximizado
        x = self.root.winfo_x() + e.x - self._mx
        y = self.root.winfo_y() + e.y - self._my
        self.root.geometry(f"+{x}+{y}")

    def _hwnd(self):
        import ctypes
        # GA_ROOT = 2 -> HWND real do topo (mesmo com overrideredirect)
        return ctypes.windll.user32.GetAncestor(self.root.winfo_id(), 2)

    def _enable_taskbar(self) -> None:
        """Faz a janela sem moldura aparecer na barra de tarefas (Windows)."""
        if os.name != "nt":
            return
        try:
            import ctypes
            GWL_EXSTYLE = -20
            WS_EX_APPWINDOW = 0x00040000
            WS_EX_TOOLWINDOW = 0x00000080
            u = ctypes.windll.user32
            hwnd = self._hwnd()
            style = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
            style = (style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
            u.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
            _round_window(hwnd)  # cantos arredondados (Win11)
            # re-exibe para a barra de tarefas registrar a mudanca
            self.root.withdraw()
            self.root.after(10, self.root.deiconify)
            self.root.after(30, lambda: _round_window(self._hwnd()))
        except Exception:  # noqa: BLE001
            pass

    def _workarea(self):
        """Area util do monitor (exclui a barra de tarefas)."""
        try:
            import ctypes

            class RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

            r = RECT()
            ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)
            return r.left, r.top, r.right, r.bottom
        except Exception:  # noqa: BLE001
            return (0, 0, self.root.winfo_screenwidth(),
                    self.root.winfo_screenheight())

    def _toggle_max(self) -> None:
        if not self._maximized:
            self._restore_geom = self.root.geometry()
            l, t, r, b = self._workarea()
            self.root.geometry(f"{r - l}x{b - t}+{l}+{t}")
            self._maximized = True
            self.max_btn.configure(text="❐")
        else:
            if self._restore_geom:
                self.root.geometry(self._restore_geom)
            self._maximized = False
            self.max_btn.configure(text="□")

    def _minimize(self) -> None:
        try:
            import ctypes
            ctypes.windll.user32.ShowWindow(self._hwnd(), 6)  # SW_MINIMIZE
        except Exception:  # noqa: BLE001
            self.root.iconify()

    # ---------- ffmpeg ----------

    def _check_ffmpeg(self) -> None:
        try:
            find_ffmpeg()
        except ConversionError as e:
            self.set_status(str(e), error=True)

    def set_status(self, text: str, error: bool = False) -> None:
        color = self.T["danger"] if error else self.T["ok"]
        self.status.configure(text=text, fg=color, bg=self.T["bg"])

    # ---------- selecao de arquivo ----------

    def _on_drop(self, event) -> None:
        path = event.data.strip().strip("{}")
        self.set_file(path)

    def choose_file(self) -> None:
        patterns = " ".join(f"*{e}" for e in INPUT_EXTS)
        path = filedialog.askopenfilename(
            title="Escolha um GIF ou video",
            filetypes=[("GIF e videos", patterns), ("Todos os arquivos", "*.*")])
        if path:
            self.set_file(path)

    def set_file(self, path: str) -> None:
        if not path.lower().endswith(INPUT_EXTS):
            self.set_status(
                "Formato nao suportado. Use GIF, MP4, WebM, MOV, MKV, AVI...",
                error=True)
            return
        self.selected_file = path
        name = path.replace("\\", "/").split("/")[-1]
        self._drop_msg = f"✓  {name}"
        self._drop_fg_key = "accent2"
        self._draw_drop()
        self.convert_btn.configure(state="normal")
        self.cut_btn.configure(state="normal")
        # Sugere GIF como saida quando a entrada e um video.
        if not path.lower().endswith(".gif"):
            self.format_var.set("gif")
            self._on_format_change()
            self.set_status("Video carregado. Saida em GIF selecionada. "
                            "Ajuste as opcoes e converta.")
        else:
            self.set_status("Pronto para converter.")

    # ---------- handlers das opcoes ----------

    def _on_interp_change(self, event=None) -> None:
        if self.interp_var.get() == "rife":
            self.set_status("Interpolacao por IA (RIFE): fluidez muito melhor, "
                            "porem mais lenta. Precisa de GPU com Vulkan.")
        else:
            self.set_status("Interpolacao FFmpeg (minterpolate): rapida.")

    def _on_engine_change(self, event=None) -> None:
        if self.engine_var.get() == "ia":
            self.model_combo.configure(state="readonly")
            if self.upscale_var.get() == "1x":
                self.upscale_var.set("2x")
            self.upscale_combo_values(["2x", "3x", "4x"])
            self.set_status("Upscaling por IA (Real-ESRGAN): melhor qualidade, "
                            "porem mais lento.")
        else:
            self.model_combo.configure(state="disabled")
            self.upscale_combo_values(["1x", "2x", "3x", "4x"])
            self.set_status("Upscaling classico (Lanczos + nitidez).")

    def upscale_combo_values(self, values) -> None:
        # a combobox de upscale e a 4a criada (index 3)
        self._combos[3].configure(values=values)

    def _on_format_change(self, event=None) -> None:
        if self.format_var.get() == "gif":
            self.format_note.configure(
                text="Atencao: GIF sera limitado a 50fps (acima disso fica em "
                     "camera lenta). Use MP4/WebM para 60fps reais.",
                fg=self.T["warn"])
        else:
            self.format_note.configure(
                text="MP4/WebM = 60fps reais, na velocidade certa e arquivo menor.",
                fg=self.T["muted"])

    # ---------- conversao ----------

    def _parse_trim(self):
        """Le inicio/duracao/largura dos campos. Levanta ValueError se invalido."""
        s_txt = self.start_var.get().strip().replace(",", ".")
        d_txt = self.dur_var.get().strip().replace(",", ".")
        start = float(s_txt) if s_txt else 0.0
        duration = float(d_txt) if d_txt else None
        if start < 0 or (duration is not None and duration <= 0):
            raise ValueError("tempos invalidos")
        w = self.width_var.get()
        max_width = None if w == "Original" else int(w)
        return start, duration, max_width

    def start_conversion(self) -> None:
        if not self.selected_file:
            return
        try:
            self._start, self._duration, self._max_width = self._parse_trim()
        except ValueError:
            self.set_status("Inicio/Duracao invalidos. Use numeros em segundos "
                            "(ex.: Inicio 2, Duracao 5).", error=True)
            return
        self.cancel_event = threading.Event()
        self.current_proc = None
        self.convert_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.open_btn.configure(state="disabled")
        self.folder_btn.configure(state="disabled")
        self.progress.configure(value=0)
        self.pct_label.configure(text="0%")
        self.set_status("Convertendo... isso pode demorar um pouco.")
        threading.Thread(target=self._run_conversion, daemon=True).start()

    def _on_percent(self, pct: float) -> None:
        self.root.after(0, lambda: self._set_pct(pct))

    def _set_pct(self, pct: float) -> None:
        self.progress.configure(value=pct)
        self.pct_label.configure(text=f"{pct:.0f}%")

    def cancel_conversion(self) -> None:
        if not self.cancel_event:
            return
        self.cancel_event.set()
        self.cancel_btn.configure(state="disabled")
        self.set_status("Cancelando...")
        # mata o processo em execucao imediatamente
        proc = self.current_proc
        if proc is not None:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def _register_proc(self, proc) -> None:
        # chamado da thread de conversao a cada subprocesso iniciado
        self.current_proc = proc

    def _run_conversion(self) -> None:
        try:
            out = convert_gif(
                self.selected_file,
                fps=int(self.fps_var.get()),
                quality=self.quality_var.get(),
                output_format=self.format_var.get(),
                upscale=float(self.upscale_var.get().rstrip("x")),
                upscale_engine=self.engine_var.get(),
                ai_model=self.model_var.get(),
                interp_engine=self.interp_var.get(),
                start=self._start,
                duration=self._duration,
                max_width=self._max_width,
                on_progress=self._on_ffmpeg_line,
                on_notice=self._on_notice,
                cancel=self.cancel_event,
                on_process=self._register_proc,
                on_percent=self._on_percent,
            )
            self.root.after(0, lambda: self._done(out))
        except ConversionCancelled:
            self.root.after(0, self._cancelled_ui)
        except ConversionError as e:
            msg = str(e)
            self.root.after(0, lambda: self._failed(msg))
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            self.root.after(0, lambda: self._failed(f"Erro inesperado: {msg}"))

    def _on_ffmpeg_line(self, line: str) -> None:
        line = line.strip()
        if "frame=" in line:
            self.root.after(0, lambda: self.set_status(f"Convertendo... {line}"))
        elif line.endswith("%") and line[:1].isdigit():
            self.root.after(0, lambda: self.set_status(f"IA processando... {line}"))

    def _on_notice(self, message: str) -> None:
        self.root.after(0, lambda: self.set_status(message, error=False))

    def _done(self, out_path: str) -> None:
        self._set_pct(100)
        self.convert_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.last_output = out_path
        self.open_btn.configure(state="normal")
        self.folder_btn.configure(state="normal")
        name = out_path.replace("\\", "/").split("/")[-1]
        self.set_status(f"✓  Pronto! Salvo como: {name}")

    def _failed(self, message: str) -> None:
        self.convert_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.set_status(message, error=True)

    def _cancelled_ui(self) -> None:
        self.progress.configure(value=0)
        self.pct_label.configure(text="0%")
        self.convert_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.current_proc = None
        self.set_status("Conversao cancelada.", error=True)

    # ---------- trimmer visual ----------

    def open_trimmer(self) -> None:
        if not self.selected_file:
            return
        try:
            cur_start = float((self.start_var.get() or "0").replace(",", "."))
        except ValueError:
            cur_start = 0.0
        try:
            cur_dur = self.dur_var.get().strip().replace(",", ".")
            cur_dur = float(cur_dur) if cur_dur else None
        except ValueError:
            cur_dur = None
        TrimDialog(self.root, self.T, self.selected_file,
                   cur_start, cur_dur, self._apply_trim)

    def _apply_trim(self, start: float, end: float) -> None:
        def fmt(v):
            return f"{v:.2f}".rstrip("0").rstrip(".")
        self.start_var.set(fmt(start))
        self.dur_var.set(fmt(max(0.0, end - start)))
        self.set_status(
            f"Corte definido: {start:.2f}s ate {end:.2f}s "
            f"(duracao {end - start:.2f}s).")

    # ---------- abrir resultado ----------

    def open_output(self) -> None:
        if not self.last_output or not os.path.isfile(self.last_output):
            self.set_status("Arquivo nao encontrado.", error=True)
            return
        try:
            if os.name == "nt":
                os.startfile(self.last_output)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", self.last_output])
            else:
                subprocess.Popen(["xdg-open", self.last_output])
        except Exception as e:  # noqa: BLE001
            self.set_status(f"Nao consegui abrir o arquivo: {e}", error=True)

    def open_folder(self) -> None:
        if not self.last_output or not os.path.isfile(self.last_output):
            self.set_status("Arquivo nao encontrado.", error=True)
            return
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,",
                                  os.path.normpath(self.last_output)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", self.last_output])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(self.last_output)])
        except Exception as e:  # noqa: BLE001
            self.set_status(f"Nao consegui abrir a pasta: {e}", error=True)


class TrimDialog:
    """Trimmer visual: tira de miniaturas + marcadores de inicio/fim
    arrastaveis e preview do quadro (estilo editor do WhatsApp)."""

    N = 8          # numero de miniaturas
    CW, CH = 64, 48
    MARGIN = 14
    PREV_W = 380

    def __init__(self, parent, theme, path, cur_start, cur_dur, on_apply):
        self.T = theme
        self.path = path
        self.on_apply = on_apply
        self.tmp = Path(tempfile.mkdtemp(prefix="gctrim_"))
        self.thumb_imgs: list = []
        self.preview_img = None
        self._active = None
        self._pending = None

        t = theme
        self.dur = probe_duration(path) or 0.0

        win = tk.Toplevel(parent)
        self.win = win
        win.title("Cortar - GifConverter")
        win.configure(bg=t["bg"])
        win.resizable(False, False)
        win.transient(parent)
        win.protocol("WM_DELETE_WINDOW", self._close)

        pad = tk.Frame(win, bg=t["bg"])
        pad.pack(fill="both", expand=True, padx=16, pady=14)

        if self.dur <= 0:
            tk.Label(pad, text="Nao consegui ler a duracao deste arquivo.",
                     bg=t["bg"], fg=t["danger"],
                     font=(t["font"], 10)).pack()
            tk.Button(pad, text="Fechar", command=self._close, bd=0,
                      bg=t["panel"], fg=t["fg"], padx=12, pady=6).pack(pady=10)
            return

        # limites iniciais
        self.t_start = max(0.0, min(cur_start or 0.0, self.dur))
        if cur_dur and cur_dur > 0:
            self.t_end = min(self.dur, self.t_start + cur_dur)
        else:
            self.t_end = self.dur
        if self.t_end <= self.t_start:
            self.t_end = self.dur

        tk.Label(pad, text="Arraste os marcadores para escolher o trecho",
                 bg=t["bg"], fg=t["fg"], font=(t["font"], 12, "bold")).pack(anchor="w")

        # preview (dimensiona pela imagem; placeholder ate carregar)
        prevbox = tk.Frame(pad, bg=t["drop_bg"], width=self.PREV_W,
                           height=int(self.PREV_W * 9 / 16))
        prevbox.pack(pady=(10, 8))
        prevbox.pack_propagate(False)
        self.preview = tk.Label(prevbox, bg=t["drop_bg"], bd=0,
                                text="carregando preview...", fg=t["muted"],
                                font=(t["font"], 9))
        self.preview.pack(fill="both", expand=True)

        # labels de tempo
        self.time_lbl = tk.Label(pad, bg=t["bg"], fg=t["accent2"],
                                 font=(t["mono"], 10, "bold"))
        self.time_lbl.pack()

        # filmstrip
        stripw = 2 * self.MARGIN + self.N * self.CW
        self.canvas = tk.Canvas(pad, width=stripw, height=self.CH + 4,
                                bg=t["panel"], highlightthickness=1,
                                highlightbackground=t["border"])
        self.canvas.pack(pady=(10, 4))
        self.strip_x0 = self.MARGIN
        self.strip_w = self.N * self.CW
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)

        self.loading = self.canvas.create_text(
            stripw // 2, (self.CH + 4) // 2, text="carregando miniaturas...",
            fill=t["muted"], font=(t["font"], 9))

        # botoes
        btns = tk.Frame(pad, bg=t["bg"])
        btns.pack(fill="x", pady=(12, 0))
        tk.Button(btns, text="Aplicar", command=self._apply, bd=0,
                  bg=t["accent"], fg=t["accent_fg"], cursor="hand2",
                  font=(t["font"], 10, "bold"), padx=16, pady=7,
                  activebackground=t["accent_hi"]).pack(side="right")
        tk.Button(btns, text="Cancelar", command=self._close, bd=0,
                  bg=t["panel"], fg=t["fg"], cursor="hand2",
                  font=(t["font"], 10), padx=14, pady=7,
                  activebackground=t["field"]).pack(side="right", padx=(0, 8))

        self._update_time_label()
        # gera miniaturas em segundo plano
        threading.Thread(target=self._load_thumbs, daemon=True).start()
        # preview inicial
        self._request_preview(self.t_start)

        win.update_idletasks()
        px = parent.winfo_rootx() + 40
        py = parent.winfo_rooty() + 30
        win.geometry(f"+{px}+{py}")
        win.grab_set()
        win.after(60, self._round_self)

    def _round_self(self):
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetAncestor(self.win.winfo_id(), 2)
            _round_window(hwnd)
        except Exception:  # noqa: BLE001
            pass

    # ----- miniaturas -----

    def _load_thumbs(self):
        paths = []
        for i in range(self.N):
            t = self.dur * (i + 0.5) / self.N
            out = self.tmp / f"th{i}.png"
            try:
                extract_thumb(self.path, t, out, self.CW, self.CH)
            except Exception:  # noqa: BLE001
                pass
            paths.append(out)
        self.win.after(0, lambda: self._draw_thumbs(paths))

    def _draw_thumbs(self, paths):
        if not self.win.winfo_exists():
            return
        self.canvas.delete(self.loading)
        for i, p in enumerate(paths):
            if not p.is_file():
                continue
            try:
                img = tk.PhotoImage(file=str(p))
            except Exception:  # noqa: BLE001
                continue
            self.thumb_imgs.append(img)
            self.canvas.create_image(self.strip_x0 + i * self.CW, 2,
                                     anchor="nw", image=img)
        self._draw_handles()

    # ----- mapeamento tempo<->x -----

    def _t2x(self, t):
        return self.strip_x0 + (t / self.dur) * self.strip_w

    def _x2t(self, x):
        frac = (x - self.strip_x0) / self.strip_w
        return max(0.0, min(1.0, frac)) * self.dur

    # ----- desenho dos marcadores -----

    def _draw_handles(self):
        t = self.T
        c = self.canvas
        c.delete("ov")
        sx, ex = self._t2x(self.t_start), self._t2x(self.t_end)
        h = self.CH + 4
        # escurece fora da selecao
        c.create_rectangle(0, 0, sx, h, fill=t["bg"], stipple="gray50",
                           width=0, tags="ov")
        c.create_rectangle(ex, 0, 2 * self.MARGIN + self.strip_w, h, fill=t["bg"],
                           stipple="gray50", width=0, tags="ov")
        # borda da selecao
        c.create_rectangle(sx, 1, ex, h - 1, outline=t["accent"], width=2,
                           tags="ov")
        # pegadores
        c.create_rectangle(sx - 4, 0, sx + 4, h, fill=t["ok"], width=0, tags="ov")
        c.create_rectangle(ex - 4, 0, ex + 4, h, fill=t["danger"], width=0, tags="ov")

    def _update_time_label(self):
        self.time_lbl.configure(
            text=f"inicio {self.t_start:6.2f}s   |   fim {self.t_end:6.2f}s   "
                 f"|   duracao {self.t_end - self.t_start:6.2f}s")

    # ----- interacao -----

    def _on_press(self, e):
        sx, ex = self._t2x(self.t_start), self._t2x(self.t_end)
        self._active = "start" if abs(e.x - sx) <= abs(e.x - ex) else "end"
        self._on_drag(e)

    def _on_drag(self, e):
        if not self._active:
            return
        t = self._x2t(e.x)
        gap = max(0.05, self.dur / 200)
        if self._active == "start":
            self.t_start = min(t, self.t_end - gap)
            self.t_start = max(0.0, self.t_start)
        else:
            self.t_end = max(t, self.t_start + gap)
            self.t_end = min(self.dur, self.t_end)
        self._draw_handles()
        self._update_time_label()
        self._preview_nearest(self.t_start if self._active == "start" else self.t_end)

    def _on_release(self, e):
        if not self._active:
            return
        self._request_preview(self.t_start if self._active == "start" else self.t_end)
        self._active = None

    # ----- preview -----

    def _preview_nearest(self, t):
        # feedback rapido: amplia a miniatura mais proxima
        if not self.thumb_imgs:
            return
        idx = int(min(self.N - 1, max(0, t / self.dur * self.N)))
        if idx < len(self.thumb_imgs):
            try:
                self.preview_img = self.thumb_imgs[idx].zoom(5)
                self.preview.configure(image=self.preview_img, text="")
            except Exception:  # noqa: BLE001
                pass

    def _request_preview(self, t):
        # extrai o quadro exato (nitido) em segundo plano
        if self._pending:
            try:
                self.win.after_cancel(self._pending)
            except Exception:  # noqa: BLE001
                pass
        self._pending = self.win.after(
            60, lambda: threading.Thread(
                target=self._do_preview, args=(t,), daemon=True).start())

    def _do_preview(self, t):
        out = self.tmp / "prev.png"
        try:
            extract_frame(self.path, t, out, width=self.PREV_W)
        except Exception:  # noqa: BLE001
            return
        self.win.after(0, lambda: self._show_preview(out))

    def _show_preview(self, out):
        if not self.win.winfo_exists() or not out.is_file():
            return
        try:
            self.preview_img = tk.PhotoImage(file=str(out))
            self.preview.configure(image=self.preview_img, text="")
        except Exception:  # noqa: BLE001
            pass

    # ----- final -----

    def _apply(self):
        self.on_apply(self.t_start, self.t_end)
        self._close()

    def _close(self):
        try:
            self.win.grab_release()
        except Exception:  # noqa: BLE001
            pass
        self.win.destroy()
        shutil_rmtree(self.tmp)


def shutil_rmtree(p):
    import shutil
    shutil.rmtree(p, ignore_errors=True)


def main() -> None:
    root = TkinterDnD.Tk() if _DND_OK else tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
