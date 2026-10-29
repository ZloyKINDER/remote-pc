"""Tkinter host window for RemotePC."""

from __future__ import annotations

import asyncio
import ctypes
import ipaddress
import logging
import os
import queue
import ssl
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import messagebox, ttk

from aiohttp import web

from . import __version__, admin_task, auth, autostart
from .config import HostConfig, default_config_path
from .h264 import find_ffmpeg
from .server import RemotePCServer, ServerSettings
from .tls import default_cert_dir, ensure_cert
from .winapi import enable_dpi_awareness, get_monitors, local_ipv4_addresses

log = logging.getLogger("remotepc")

BG = "#081018"
PANEL = "#101b26"
PANEL_2 = "#0c1620"
LINE = "#203141"
TEXT = "#e9f6f2"
MUTED = "#8ba6b7"
ACCENT = "#3bd9b2"
DANGER = "#ff6d7c"


class QueueLogHandler(logging.Handler):
    def __init__(self, events: queue.Queue) -> None:
        super().__init__()
        self.events = events
        self.setFormatter(
            logging.Formatter(
                "%(asctime)s  %(levelname)-7s %(message)s",
                datefmt="%H:%M:%S",
            )
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.events.put(("log", (record.levelname, self.format(record))))
        except Exception:
            self.handleError(record)


class HostRuntime:
    """Runs aiohttp in a private event loop on a worker thread."""

    def __init__(self, config: HostConfig, events: queue.Queue) -> None:
        self.config = config
        self.events = events
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.runner: web.AppRunner | None = None
        self.server: RemotePCServer | None = None
        self._stop_lock = threading.Lock()

    def start(self) -> None:
        self.thread = threading.Thread(
            target=self._thread_main,
            name="remotepc-host",
            daemon=True,
        )
        self.thread.start()

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self.loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._start_async())
            self.events.put(("started", self._info))
            loop.run_forever()
        except Exception as exc:
            log.exception("server start failed")
            self.events.put(("error", str(exc)))
        finally:
            try:
                if self.runner is not None:
                    loop.run_until_complete(self.runner.cleanup())
            except Exception:
                log.debug("runner cleanup failed", exc_info=True)
            self.runner = None
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
            self.loop = None
            self.events.put(("stopped", None))

    async def _start_async(self) -> None:
        cfg = self.config
        enable_dpi_awareness()
        monitors = get_monitors()
        if not monitors:
            raise RuntimeError("Не найден ни один монитор.")
        settings = ServerSettings(
            fps=cfg.fps,
            max_width=cfg.max_width,
            quality=cfg.quality,
            codec="h264" if (not cfg.demo and find_ffmpeg()) else "jpeg",
            demo=cfg.demo,
            tls=cfg.tls,
        )
        fingerprint = None
        cert_path: Path | None = None
        ssl_context = None
        if cfg.tls:
            cert_dir = Path(cfg.cert_dir) if cfg.cert_dir else default_cert_dir()
            extra = [cfg.public_ip] if cfg.public_ip else []
            try:
                cert_path, key_path, fingerprint = ensure_cert(cert_dir, extra)
            except OSError as exc:
                fallback = Path.cwd() / ".certs"
                self.events.put(
                    ("log", ("WARNING", f"Сертификат в {cert_dir} не создан ({exc}); использую {fallback}"))
                )
                cert_path, key_path, fingerprint = ensure_cert(fallback, extra)
                cert_dir = fallback
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
            ssl_context.load_cert_chain(cert_path, key_path)

        server = RemotePCServer(cfg.password, monitors, settings)
        app = server.make_app()
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=3.0)
        await runner.setup()
        site = web.TCPSite(
            runner,
            host=cfg.bind,
            port=cfg.port,
            ssl_context=ssl_context,
        )
        await site.start()
        self.server = server
        self.runner = runner
        scheme = "https" if cfg.tls else "http"
        wildcard = cfg.bind in ("0.0.0.0", "::", "")
        addresses = local_ipv4_addresses() if wildcard else [cfg.bind]
        urls: list[str] = []
        if cfg.public_ip:
            host = f"[{cfg.public_ip}]" if ":" in cfg.public_ip else cfg.public_ip
            urls.append(f"{scheme}://{host}:{cfg.port}")
        for address in addresses:
            url = f"{scheme}://{address}:{cfg.port}"
            if url not in urls:
                urls.append(url)
        if not urls:
            urls.append(f"{scheme}://<IP-этого-ПК>:{cfg.port}")
        open_host = (
            "127.0.0.1" if wildcard or cfg.bind in ("127.0.0.1", "localhost") else cfg.bind
        )
        self._info = {
            "urls": urls,
            "open_url": f"{scheme}://{open_host}:{cfg.port}/",
            "fingerprint": fingerprint,
            "cert_dir": str(cert_path.parent) if cert_path else "",
            "tls": cfg.tls,
        }

    async def _shutdown_async(self) -> None:
        runner = self.runner
        self.runner = None
        if runner is not None:
            await runner.cleanup()
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.loop.stop)

    def stop(self, timeout: float = 6.0) -> None:
        with self._stop_lock:
            loop = self.loop
            if loop is None:
                return
            try:
                future = asyncio.run_coroutine_threadsafe(self._shutdown_async(), loop)
                future.result(timeout)
            except Exception:
                log.debug("server stop failed", exc_info=True)
            if self.thread is not None:
                self.thread.join(timeout)


class HostApp:
    def __init__(
        self,
        root: tk.Tk,
        config_path: Path | None = None,
        autostart_mode: bool = False,
    ) -> None:
        self.root = root
        self.autostart_mode = autostart_mode
        self.config_path = (config_path or default_config_path()).resolve()
        self.config = HostConfig.load(self.config_path)
        if not self.config.password:
            self.config.new_password()
        self.events: queue.Queue = queue.Queue()
        self.runtime: HostRuntime | None = None
        self.running = False
        self.log_handler: QueueLogHandler | None = None
        self.open_url = ""
        self._build_style()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll_events)
        self._load_form()
        self._append_log("INFO", f"RemotePC {__version__} · Windows host")
        self._append_log("INFO", f"Конфиг: {self.config_path}")
        self._append_log("INFO", "Проверьте параметры и нажмите «Запустить».")
        if self.config.autostart_server:
            self._append_log("INFO", "Автозапуск сервера включён.")
            self.root.after(350, self._auto_start_server)

    def _auto_start_server(self) -> None:
        if self.runtime is not None or self.running:
            return
        self._start()
        if self.autostart_mode:
            self.root.after(900, self._minimize_after_start)

    def _minimize_after_start(self) -> None:
        if self.runtime is None:
            return
        try:
            self.root.iconify()
        except tk.TclError:
            pass

    def _build_style(self) -> None:
        self.root.title(f"RemotePC {__version__}")
        self.root.geometry("1060x760")
        self.root.minsize(860, 640)
        self.root.configure(bg=BG)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=TEXT, fieldbackground=PANEL_2)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=PANEL, relief="flat")
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", background=PANEL, foreground=MUTED)
        style.configure("Card.TLabel", background=PANEL, foreground=TEXT)
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=("Segoe UI Semibold", 18))
        style.configure("Status.TLabel", background=BG, foreground=ACCENT, font=("Segoe UI Semibold", 10))
        style.configure("TButton", background=PANEL_2, foreground=TEXT, borderwidth=1)
        style.map("TButton", background=[("active", LINE)])
        style.configure("Accent.TButton", background=ACCENT, foreground="#061014", font=("Segoe UI Semibold", 10))
        style.map("Accent.TButton", background=[("active", "#63e7c7"), ("disabled", "#244b43")])
        style.configure("Danger.TButton", foreground=DANGER)
        style.configure("TCheckbutton", background=PANEL, foreground=TEXT)
        style.map("TCheckbutton", background=[("active", PANEL)])
        style.configure("TEntry", fieldbackground=PANEL_2, foreground=TEXT, insertcolor=TEXT)
        style.configure("TSpinbox", fieldbackground=PANEL_2, foreground=TEXT, arrowcolor=TEXT)
        style.configure(
            "TCombobox",
            fieldbackground=PANEL_2,
            background=PANEL_2,
            foreground=TEXT,
            arrowcolor=TEXT,
            bordercolor=LINE,
            lightcolor=LINE,
            darkcolor=LINE,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", PANEL_2), ("disabled", PANEL)],
            foreground=[("readonly", TEXT), ("disabled", MUTED)],
            selectbackground=[("readonly", PANEL_2)],
            selectforeground=[("readonly", TEXT)],
            background=[("readonly", PANEL_2), ("active", LINE)],
        )
        self.root.option_add("*TCombobox*Listbox.background", PANEL_2)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", LINE)
        self.root.option_add("*TCombobox*Listbox.selectForeground", TEXT)
        style.configure("TLabelframe", background=PANEL, foreground=MUTED, bordercolor=LINE)
        style.configure("TLabelframe.Label", background=PANEL, foreground=MUTED)
        style.configure("TScrollbar", background=PANEL_2, troughcolor=BG, arrowcolor=MUTED)

    def _build_ui(self) -> None:
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=22, pady=(18, 8))
        tk.Label(
            header,
            text="RemotePC",
            bg=BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 20),
        ).pack(side="left")
        tk.Label(
            header,
            text=f"  {__version__}  ·  хост",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 10),
        ).pack(side="left", pady=(6, 0))
        self.status_var = tk.StringVar(value="Остановлен")
        self.status_label = tk.Label(
            header,
            textvariable=self.status_var,
            bg=BG,
            fg=MUTED,
            font=("Segoe UI Semibold", 10),
        )
        self.status_label.pack(side="right")

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=22, pady=(0, 18))
        body.columnconfigure(0, weight=0, minsize=400)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        left_outer = tk.Frame(
            body, bg=PANEL, highlightbackground=LINE, highlightthickness=1
        )
        left_outer.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        left_outer.rowconfigure(0, weight=1)
        left_outer.columnconfigure(0, weight=1)
        left_canvas = tk.Canvas(
            left_outer,
            bg=PANEL,
            highlightthickness=0,
            bd=0,
            width=380,
        )
        left_canvas.grid(row=0, column=0, sticky="nsew")
        left_scroll = ttk.Scrollbar(
            left_outer, orient="vertical", command=left_canvas.yview
        )
        left_scroll.grid(row=0, column=1, sticky="ns")
        left_canvas.configure(yscrollcommand=left_scroll.set)
        left = tk.Frame(left_canvas, bg=PANEL)
        left_window = left_canvas.create_window((0, 0), window=left, anchor="nw")
        left.columnconfigure(1, weight=1)

        def _left_region(_event=None) -> None:
            left_canvas.configure(scrollregion=left_canvas.bbox("all"))

        def _left_width(event) -> None:
            left_canvas.itemconfigure(left_window, width=event.width)
            _left_region()

        left.bind("<Configure>", _left_region)
        left_canvas.bind("<Configure>", _left_width)
        left_bottom = tk.Frame(left_outer, bg=PANEL)
        left_bottom.grid(row=1, column=0, columnspan=2, sticky="ew")

        title = tk.Label(
            left,
            text="Параметры хоста",
            bg=PANEL,
            fg=TEXT,
            font=("Segoe UI Semibold", 11),
        )
        title.grid(row=0, column=0, columnspan=3, sticky="w", padx=16, pady=(14, 10))

        self.var_password = tk.StringVar()
        self.var_show = tk.BooleanVar(value=False)
        self.var_port = tk.StringVar()
        self.var_max_width = tk.StringVar()
        self.var_fps = tk.StringVar()
        self.var_quality = tk.StringVar()
        self.var_tls = tk.BooleanVar(value=True)
        self.var_demo = tk.BooleanVar(value=False)
        self.var_start_windows = tk.BooleanVar(value=False)
        self.var_autostart_server = tk.BooleanVar(value=False)
        self.var_run_as_admin = tk.BooleanVar(value=False)
        self.var_public_ip = tk.StringVar()
        self.var_bind = tk.StringVar()
        self.entries: list[tk.Entry] = []
        self.combos: list[ttk.Combobox] = []

        row = 1
        self._label(left, "Пароль", row)
        self.password_entry = tk.Entry(
            left,
            textvariable=self.var_password,
            show="*",
            bg=PANEL_2,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            highlightbackground=LINE,
            highlightcolor=ACCENT,
            highlightthickness=1,
            font=("Consolas", 10),
        )
        self.password_entry.grid(row=row, column=1, sticky="ew", padx=6, pady=4)
        show = tk.Checkbutton(
            left,
            text="Показать",
            variable=self.var_show,
            command=self._toggle_password,
            bg=PANEL,
            fg=MUTED,
            selectcolor=PANEL_2,
            activebackground=PANEL,
            activeforeground=TEXT,
            font=("Segoe UI", 8),
        )
        show.grid(row=row, column=2, sticky="w", padx=(0, 8), pady=4)
        row += 1
        self.gen_btn = tk.Button(
            left,
            text="Сгенерировать пароль",
            command=self._generate_password,
            bg=PANEL_2,
            fg=ACCENT,
            activebackground=LINE,
            activeforeground=TEXT,
            relief="flat",
            font=("Segoe UI", 9),
            cursor="hand2",
        )
        self.gen_btn.grid(row=row, column=1, columnspan=2, sticky="w", padx=6, pady=(0, 8))
        self._entry_row(left, "Порт", self.var_port, row + 1)
        row += 1
        self._entry_row(left, "Макс. ширина", self.var_max_width, row + 1)
        row += 1
        self._combo_row(
            left,
            "FPS (все кодеки)",
            self.var_fps,
            row + 1,
            ("15", "30", "45", "60"),
        )
        row += 1
        self._entry_row(left, "Качество", self.var_quality, row + 1)
        row += 1
        self._entry_row(left, "Публичный IP", self.var_public_ip, row + 1)
        row += 1
        self._entry_row(left, "Слушать", self.var_bind, row + 1)
        row += 2

        self.tls_check = tk.Checkbutton(
            left,
            text="HTTPS (TLS)",
            variable=self.var_tls,
            bg=PANEL,
            fg=TEXT,
            selectcolor=PANEL_2,
            activebackground=PANEL,
            activeforeground=TEXT,
            anchor="w",
        )
        self.tls_check.grid(row=row, column=1, sticky="w", padx=6, pady=4)
        row += 1
        self.demo_check = tk.Checkbutton(
            left,
            text="Демо-экран (тест)",
            variable=self.var_demo,
            bg=PANEL,
            fg=TEXT,
            selectcolor=PANEL_2,
            activebackground=PANEL,
            activeforeground=TEXT,
            anchor="w",
        )
        self.demo_check.grid(row=row, column=1, sticky="w", padx=6, pady=(4, 4))
        row += 1
        self.start_windows_check = tk.Checkbutton(
            left,
            text="Старт с Windows",
            variable=self.var_start_windows,
            bg=PANEL,
            fg=TEXT,
            selectcolor=PANEL_2,
            activebackground=PANEL,
            activeforeground=TEXT,
            anchor="w",
        )
        self.start_windows_check.grid(row=row, column=1, sticky="w", padx=6, pady=2)
        row += 1
        self.autostart_server_check = tk.Checkbutton(
            left,
            text="Автозапуск сервера",
            variable=self.var_autostart_server,
            bg=PANEL,
            fg=TEXT,
            selectcolor=PANEL_2,
            activebackground=PANEL,
            activeforeground=TEXT,
            anchor="w",
        )
        self.autostart_server_check.grid(row=row, column=1, sticky="w", padx=6, pady=(2, 2))
        row += 1
        self.admin_check = tk.Checkbutton(
            left,
            text="С правами администратора",
            variable=self.var_run_as_admin,
            command=self._admin_toggled,
            bg=PANEL,
            fg=TEXT,
            selectcolor=PANEL_2,
            activebackground=PANEL,
            activeforeground=TEXT,
            anchor="w",
        )
        self.admin_check.grid(row=row, column=1, sticky="w", padx=6, pady=(2, 6))
        row += 1

        note = tk.Label(
            left,
            text=(
                "Обе галочки: после входа в Windows сервер запустится сам, "
                "окно свернётся в панель задач. Достаточно открыть сайт.\n"
                "Для прав администратора потребуется один раз подтвердить UAC; "
                "дальше задача запускается при входе без запроса.\n"
                "Пароль хранится в remote-pc.json в зашифрованном виде (Windows DPAPI)."
            ),
            bg=PANEL,
            fg=MUTED,
            wraplength=350,
            justify="left",
            font=("Segoe UI", 8),
        )
        note.grid(row=row, column=0, columnspan=3, sticky="w", padx=16, pady=(0, 10))
        row += 1

        buttons = tk.Frame(left_bottom, bg=PANEL)
        buttons.pack(fill="x", padx=12, pady=(8, 4))
        buttons.columnconfigure(0, weight=1)
        buttons.columnconfigure(1, weight=1)
        buttons.columnconfigure(2, weight=1)
        self.start_btn = ttk.Button(buttons, text="Запустить", style="Accent.TButton", command=self._start)
        self.start_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.stop_btn = ttk.Button(buttons, text="Остановить", style="Danger.TButton", command=self._stop)
        self.stop_btn.grid(row=0, column=1, sticky="ew", padx=4)
        self.save_btn = ttk.Button(buttons, text="Сохранить", command=self._save)
        self.save_btn.grid(row=0, column=2, sticky="ew", padx=(4, 0))
        row += 1

        self.addresses = tk.Label(
            left_bottom,
            text="Адреса появятся после запуска.",
            bg=PANEL,
            fg=MUTED,
            justify="left",
            anchor="w",
            wraplength=370,
            font=("Consolas", 8),
        )
        self.addresses.pack(fill="x", padx=16, pady=(2, 4))
        actions = tk.Frame(left_bottom, bg=PANEL)
        actions.pack(fill="x", padx=12, pady=(0, 10))
        self.open_btn = ttk.Button(actions, text="Открыть в браузере", command=self._open_browser)
        self.open_btn.pack(side="left")
        self.open_btn.state(["disabled"])
        self.copy_btn = ttk.Button(actions, text="Копировать адрес", command=self._copy_address)
        self.copy_btn.pack(side="left", padx=6)
        self.copy_btn.state(["disabled"])
        self.config_btn = ttk.Button(actions, text="Папка конфига", command=self._open_config_folder)
        self.config_btn.pack(side="left")

        right = tk.Frame(body, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)
        log_header = tk.Frame(right, bg=PANEL)
        log_header.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 6))
        tk.Label(
            log_header,
            text="Журнал",
            bg=PANEL,
            fg=TEXT,
            font=("Segoe UI Semibold", 11),
        ).pack(side="left")
        self.config_label = tk.Label(
            log_header,
            text=self.config_path.name,
            bg=PANEL,
            fg=MUTED,
            font=("Consolas", 8),
        )
        self.config_label.pack(side="right")
        self.config_label.configure(cursor="hand2")
        self.config_label.bind("<Button-1>", lambda _event: self._open_config_folder())
        log_frame = tk.Frame(right, bg=PANEL_2)
        log_frame.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 10))
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(
            log_frame,
            bg=PANEL_2,
            fg="#bcd4df",
            insertbackground=TEXT,
            relief="flat",
            wrap="word",
            font=("Consolas", 8),
            padx=10,
            pady=8,
            state="disabled",
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.tag_config("ERROR", foreground=DANGER)
        self.log_text.tag_config("WARNING", foreground="#ffc46b")
        self.log_text.tag_config("INFO", foreground="#bcd4df")
        self.log_text.tag_config("DEBUG", foreground=MUTED)

        def _wheel(event) -> str:
            left_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"

        def _bind_wheel(widget) -> None:
            widget.bind("<MouseWheel>", _wheel)
            for child in widget.winfo_children():
                _bind_wheel(child)

        _bind_wheel(left)

    def _label(self, parent: tk.Widget, text: str, row: int) -> None:
        tk.Label(
            parent,
            text=text,
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI", 9),
            anchor="w",
        ).grid(row=row, column=0, sticky="w", padx=(16, 0), pady=3)

    def _entry_row(
        self, parent: tk.Widget, text: str, variable: tk.StringVar, row: int
    ) -> None:
        self._label(parent, text, row)
        entry = tk.Entry(
            parent,
            textvariable=variable,
            bg=PANEL_2,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            highlightbackground=LINE,
            highlightcolor=ACCENT,
            highlightthickness=1,
            font=("Segoe UI", 9),
        )
        entry.grid(row=row, column=1, columnspan=2, sticky="ew", padx=(6, 12), pady=3)
        self.entries.append(entry)

    def _combo_row(
        self,
        parent: tk.Widget,
        text: str,
        variable: tk.StringVar,
        row: int,
        values: tuple[str, ...],
    ) -> None:
        self._label(parent, text, row)
        combo = ttk.Combobox(
            parent,
            textvariable=variable,
            values=values,
            state="readonly",
            font=("Segoe UI", 9),
        )
        combo.grid(row=row, column=1, columnspan=2, sticky="ew", padx=(6, 12), pady=3)
        self.combos.append(combo)

    def _toggle_password(self) -> None:
        self.password_entry.configure(show="" if self.var_show.get() else "*")

    def _generate_password(self) -> None:
        self.var_password.set(auth.generate_password(20))
        self.var_show.set(True)
        self._toggle_password()

    def _admin_toggled(self) -> None:
        if self.var_run_as_admin.get():
            # Admin elevation only matters for the Windows-logon task, and the
            # expected result is an already running server after login.
            self.var_start_windows.set(True)
            self.var_autostart_server.set(True)

    def _load_form(self) -> None:
        cfg = self.config
        self.var_password.set(cfg.password)
        self.var_port.set(str(cfg.port))
        self.var_max_width.set(str(cfg.max_width))
        self.var_fps.set(str(cfg.fps) if str(cfg.fps) in ("15", "30", "45", "60") else "60")
        self.var_quality.set(str(cfg.quality))
        self.var_tls.set(cfg.tls)
        self.var_demo.set(cfg.demo)
        self.var_start_windows.set(cfg.start_with_windows)
        self.var_autostart_server.set(cfg.autostart_server)
        self.var_run_as_admin.set(cfg.run_as_admin)
        if cfg.run_as_admin:
            self.var_start_windows.set(True)
            self.var_autostart_server.set(True)
        self.var_public_ip.set(cfg.public_ip)
        self.var_bind.set(cfg.bind)
        self._set_running(False)

    def _read_form(self) -> HostConfig | None:
        def number(var: tk.StringVar, name: str, low: int, high: int) -> int | None:
            try:
                value = int(var.get().strip())
            except ValueError:
                messagebox.showerror("RemotePC", f"{name}: нужно целое число.")
                return None
            if not (low <= value <= high):
                messagebox.showerror("RemotePC", f"{name}: допустимо от {low} до {high}.")
                return None
            return value

        port = number(self.var_port, "Порт", 1, 65535)
        max_width = number(self.var_max_width, "Макс. ширина", 640, 7680)
        fps = number(self.var_fps, "FPS", 5, 60)
        quality = number(self.var_quality, "Качество", 30, 95)
        if None in (port, max_width, fps, quality):
            return None
        cfg = HostConfig(
            password=self.var_password.get(),
            port=port or 8765,
            bind=self.var_bind.get().strip() or "0.0.0.0",
            max_width=max_width or 3840,
            fps=fps or 30,
            quality=quality or 85,
            tls=self.var_tls.get(),
            demo=self.var_demo.get(),
            public_ip=self.var_public_ip.get().strip(),
            cert_dir=self.config.cert_dir,
            start_with_windows=self.var_start_windows.get(),
            autostart_server=self.var_autostart_server.get(),
            run_as_admin=self.var_run_as_admin.get(),
        ).normalized()
        if cfg.run_as_admin:
            cfg.start_with_windows = True
            cfg.autostart_server = True
            self.var_start_windows.set(True)
            self.var_autostart_server.set(True)
        error = cfg.validate()
        if error:
            messagebox.showerror("RemotePC", error)
            return None
        return cfg

    def _save(self) -> HostConfig | None:
        cfg = self._read_form()
        if cfg is None:
            return None
        try:
            cfg.save(self.config_path)
        except OSError as exc:
            messagebox.showerror("RemotePC", f"Не удалось сохранить конфиг:\n{exc}")
            return None
        want_admin = cfg.start_with_windows and cfg.run_as_admin
        admin_installed = admin_task.is_installed()
        if want_admin and not admin_installed:
            self._set_status("Автозапуск: подтвердите UAC…", "#ffc46b")
            self.root.update()
            ok, error = admin_task.run_elevated("install", self.config_path)
            if ok:
                admin_installed = True
                self._append_log("INFO", "Задача администратора создана.")
            else:
                messagebox.showwarning(
                    "RemotePC",
                    "Не удалось создать задачу с правами администратора:\n"
                    f"{error}\n\nБудет включён обычный автозапуск без прав администратора.",
                )
                self._append_log("WARNING", f"Задача администратора не создана: {error}")

        if want_admin and admin_installed:
            try:
                autostart.set_start_with_windows(False)
            except OSError as exc:
                self._append_log("WARNING", f"Не удалось убрать обычный автозапуск: {exc}")
            self._append_log(
                "INFO",
                "Автозапуск Windows включён с правами администратора (Task Scheduler).",
            )
        else:
            # Admin was turned off, or its task could not be created.
            if admin_installed and not want_admin:
                self._set_status("Удаление задачи администратора…", "#ffc46b")
                self.root.update()
                ok, error = admin_task.run_elevated("remove")
                if ok:
                    admin_installed = False
                    self._append_log("INFO", "Задача администратора удалена.")
                else:
                    messagebox.showwarning(
                        "RemotePC",
                        "Не удалось удалить задачу администратора:\n"
                        f"{error}\n\nАвтозапуск с правами администратора остаётся активным.",
                    )
                    self._append_log("WARNING", f"Задача администратора не удалена: {error}")
            if not admin_installed:
                try:
                    autostart.set_start_with_windows(cfg.start_with_windows, self.config_path)
                except OSError as exc:
                    messagebox.showwarning(
                        "RemotePC",
                        f"Конфиг сохранён, но автозапуск Windows не настроен:\n{exc}",
                    )
                self._append_log(
                    "INFO",
                    "Автозапуск Windows включён." if cfg.start_with_windows else "Автозапуск Windows выключен.",
                )
        self._set_status("Остановлен", MUTED)
        self.config = cfg
        self._append_log("INFO", f"Конфиг сохранён: {self.config_path}")
        return cfg

    def _install_log_handler(self) -> None:
        if self.log_handler is not None:
            return
        self.log_handler = QueueLogHandler(self.events)
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(self.log_handler)
        logging.getLogger("dxcam").setLevel(logging.WARNING)

    def _remove_log_handler(self) -> None:
        if self.log_handler is None:
            return
        logging.getLogger().removeHandler(self.log_handler)
        self.log_handler = None

    def _start(self) -> None:
        if self.running or self.runtime is not None:
            return
        cfg = self._save()
        if cfg is None:
            return
        self._set_status("Запуск…", ACCENT)
        self._set_running(True)
        self._install_log_handler()
        self.runtime = HostRuntime(cfg, self.events)
        self.runtime.start()

    def _stop(self) -> None:
        if self.runtime is None:
            return
        self._set_status("Остановка…", "#ffc46b")
        self.stop_btn.state(["disabled"])
        threading.Thread(target=self._stop_worker, daemon=True).start()

    def _stop_worker(self) -> None:
        runtime = self.runtime
        if runtime is not None:
            runtime.stop()
        self.events.put(("stopped", None))

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    level, message = payload
                    self._append_log(level, message)
                elif kind == "started":
                    self._on_started(payload)
                elif kind == "error":
                    self._on_error(str(payload))
                elif kind == "stopped":
                    self._on_stopped()
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _append_log(self, level: str, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n", level if level in ("INFO", "WARNING", "ERROR", "DEBUG") else "INFO")
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 1200:
            self.log_text.delete("1.0", "200.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_started(self, info: dict[str, Any]) -> None:
        self.running = True
        self.open_url = str(info.get("open_url") or "")
        urls = info.get("urls") or []
        self.addresses.configure(text="\n".join(urls))
        self._set_status("Работает", ACCENT)
        self.status_label.configure(fg=ACCENT)
        self.start_btn.state(["disabled"])
        self.stop_btn.state(["!disabled"])
        self.open_btn.state(["!disabled"])
        self.copy_btn.state(["!disabled"])
        self._set_fields_enabled(False)
        self._append_log("INFO", "Сервер запущен.")
        if info.get("fingerprint"):
            self._append_log("INFO", f"Сертификат: {info['fingerprint']}")
        if not info.get("tls"):
            self._append_log("WARNING", "TLS выключен: трафик и экран передаются открыто.")

    def _on_error(self, message: str) -> None:
        self.running = False
        self._set_status("Ошибка запуска", DANGER)
        self.status_label.configure(fg=DANGER)
        self._set_running(False)
        self.runtime = None
        self._remove_log_handler()
        messagebox.showerror("RemotePC", message)

    def _on_stopped(self) -> None:
        if not self.running:
            self._set_running(False)
            self.runtime = None
            self._remove_log_handler()
            self._set_status("Остановлен", MUTED)
            self.status_label.configure(fg=MUTED)
            return
        self.running = False
        self.runtime = None
        self._remove_log_handler()
        self._set_running(False)
        self._set_status("Остановлен", MUTED)
        self.status_label.configure(fg=MUTED)
        self._append_log("INFO", "Сервер остановлен.")

    def _set_running(self, starting: bool) -> None:
        # Keep fields locked while starting/running; enabled only when idle.
        running = starting or self.running
        self.start_btn.state(["disabled"] if running else ["!disabled"])
        self.stop_btn.state(["!disabled"] if self.runtime is not None else ["disabled"])
        self.save_btn.state(["disabled"] if running else ["!disabled"])
        self._set_fields_enabled(not running)
        if not running:
            self.open_btn.state(["disabled"])
            self.copy_btn.state(["disabled"])

    def _set_fields_enabled(self, enabled: bool) -> None:
        for widget in (
            self.password_entry,
            self.tls_check,
            self.demo_check,
            self.start_windows_check,
            self.autostart_server_check,
            self.admin_check,
        ):
            widget.configure(state="normal" if enabled else "disabled")
        for widget in self.entries:
            widget.configure(state="normal" if enabled else "disabled")
        for widget in self.combos:
            widget.configure(state="readonly" if enabled else "disabled")
        self.gen_btn.configure(state="normal" if enabled else "disabled")

    def _set_status(self, text: str, color: str) -> None:
        self.status_var.set(text)
        self.status_label.configure(fg=color)

    def _open_browser(self) -> None:
        if self.open_url:
            webbrowser.open(self.open_url)

    def _copy_address(self) -> None:
        text = self.addresses.cget("text")
        line = text.splitlines()[0] if text else ""
        if line:
            self.root.clipboard_clear()
            self.root.clipboard_append(line)
            self._append_log("INFO", f"Адрес скопирован: {line}")

    def _open_config_folder(self) -> None:
        try:
            os.startfile(str(self.config_path.parent))
        except Exception as exc:
            messagebox.showerror("RemotePC", str(exc))

    def _on_close(self) -> None:
        if self.runtime is not None:
            if not messagebox.askyesno("RemotePC", "Остановить сервер и выйти?"):
                return
            # Block briefly so ffmpeg/session cleanup is not lost when the
            # process exits.  This is a deliberate exception to the async GUI.
            self.runtime.stop()
            self.runtime = None
            self._remove_log_handler()
        else:
            # Save edits even if the server was never started.
            self._save()
        self.root.destroy()


def run_gui(config_path: Path | None = None, autostart: bool = False) -> int:
    if sys.platform != "win32":
        raise SystemExit("RemotePC host works only on Windows.")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    mutex = kernel32.CreateMutexW(None, False, "Local\\RemotePC.Host")
    if not mutex:
        raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        user32 = ctypes.WinDLL("user32")
        user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        user32.FindWindowW.restype = ctypes.c_void_p
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        hwnd = user32.FindWindowW(None, f"RemotePC {__version__}")
        if hwnd:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
        kernel32.CloseHandle(mutex)
        return 0
    enable_dpi_awareness()
    root = tk.Tk()
    HostApp(root, config_path, autostart_mode=autostart)
    root.mainloop()
    kernel32.CloseHandle(mutex)
    return 0
