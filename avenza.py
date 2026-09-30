#!/usr/bin/env python3
"""
Avenza - wallpaper animado para Windows (estilo Wallpaper Engine).

Uma janela Qt é acoplada ao WorkerW do Windows, ficando atrás dos ícones da
área de trabalho. O controle é feito por CLI e por um ícone na bandeja.

Instalação:  na pasta do projeto, rode:  pip install -e .
Uso:         avenza --help
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import random
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client, Listener
from pathlib import Path

APP = "Avenza"
PIPE = r"\\.\pipe\AvenzaCLI"
AUTHKEY = b"avenza-local"
CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / APP
CONFIG_FILE = CONFIG_DIR / "config.json"

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
GIF_EXT = {".gif"}
VIDEO_EXT = {".mp4", ".webm", ".mkv", ".avi", ".mov", ".wmv"}
MEDIA_EXT = IMAGE_EXT | GIF_EXT | VIDEO_EXT
SPI_EXT = {".png", ".jpg", ".jpeg", ".bmp"}  # formatos aceitos pelo Windows como wallpaper nativo
FITS = ("fill", "fit", "stretch", "center")
SPEEDS = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0)
SPEED_MIN, SPEED_MAX = 0.1, 4.0
DAEMON_ONLY = {"pause", "resume", "quit", "status"}

DEFAULTS = {
    "library": [],
    "current": None,
    "pause_when_away": True,
    "muted": True,
    "volume": 50,
    "fit": "fill",
    "speed": 1.0,
}

# Classes de janela que contam como "estou na área de trabalho"
DESKTOP_CLASSES = {
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
    "NotifyIconOverflowWindow", "TopLevelWindowForOverflowXamlIsland",
}


# --------------------------------------------------------------------------
# Configuração
# --------------------------------------------------------------------------
class Config:
    def __init__(self):
        self.data = dict(DEFAULTS)
        try:
            self.data.update(json.loads(CONFIG_FILE.read_text("utf-8")))
        except (OSError, ValueError):
            pass

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def save(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), "utf-8")


def res(ok: bool = True, msg: str = "", **extra) -> dict:
    return {"ok": ok, "msg": msg, **extra}


def parse_bool(v) -> bool:
    s = str(v).strip().lower()
    if s in ("1", "true", "on", "yes", "sim"):
        return True
    if s in ("0", "false", "off", "no", "nao", "não"):
        return False
    raise ValueError(f"valor booleano inválido: {v!r} (use on/off)")


def expand_media(paths) -> list[str]:
    out: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser().resolve()
        if p.is_dir():
            out += sorted(f for f in p.rglob("*") if f.suffix.lower() in MEDIA_EXT)
        elif p.is_file() and p.suffix.lower() in MEDIA_EXT:
            out.append(p)
    return [str(p) for p in out]


def find_target(cfg: Config, target: str) -> str | None:
    lib = cfg["library"]
    t = str(target)
    if t.isdigit() and 1 <= int(t) <= len(lib):
        return lib[int(t) - 1]
    p = str(Path(t).expanduser().resolve())
    if p in lib:
        return p
    for item in lib:
        if Path(item).name.lower() == t.lower():
            return item
    if os.path.isfile(p) and Path(p).suffix.lower() in MEDIA_EXT:
        return p
    return None


# --------------------------------------------------------------------------
# Iniciar com o Windows (registro HKCU\...\Run)
# --------------------------------------------------------------------------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_args() -> list[str]:
    """Comando para iniciar o app em segundo plano (sem console)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "run"]
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return [str(pyw if pyw.exists() else exe), str(Path(__file__).resolve()), "run"]


def autostart_get() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP)
            return True
    except OSError:
        return False


def autostart_set(on: bool) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, subprocess.list2cmdline(launch_args()))
        else:
            try:
                winreg.DeleteValue(k, APP)
            except FileNotFoundError:
                pass


# --------------------------------------------------------------------------
# Comandos que mexem só na configuração (usados pelo daemon e pela CLI offline)
# --------------------------------------------------------------------------
def config_command(cfg: Config, cmd: str, a: dict) -> dict:
    lib: list[str] = cfg["library"]

    if cmd == "list":
        return res(True, data={"library": lib, "current": cfg["current"]})

    if cmd == "add":
        new = [p for p in expand_media(a["paths"]) if p not in lib]
        lib.extend(new)
        reload = False
        if new and not cfg["current"]:
            cfg["current"], reload = new[0], True
        cfg.save()
        return res(True, f"{len(new)} item(ns) adicionado(s).", reload=reload)

    if cmd == "remove":
        t = find_target(cfg, a["target"])
        if not t or t not in lib:
            return res(False, "Item não encontrado na biblioteca.")
        lib.remove(t)
        reload = cfg["current"] == t
        if reload:
            cfg["current"] = None
        cfg.save()
        return res(True, "Removido.", reload=reload)

    if cmd == "set":
        t = find_target(cfg, a["target"])
        if not t:
            return res(False, "Arquivo não encontrado ou formato não suportado.")
        if t not in lib:
            lib.append(t)
        cfg["current"] = t
        cfg.save()
        return res(True, f"Wallpaper: {Path(t).name}", reload=True)

    if cmd in ("next", "prev", "random"):
        if not lib:
            return res(False, "A biblioteca está vazia. Use o comando 'add'.")
        idx = lib.index(cfg["current"]) if cfg["current"] in lib else -1
        if cmd == "random":
            choices = [p for p in lib if p != cfg["current"]] or lib
            new = random.choice(choices)
        else:
            new = lib[(idx + (1 if cmd == "next" else -1)) % len(lib)]
        cfg["current"] = new
        cfg.save()
        return res(True, f"Wallpaper: {Path(new).name}", reload=True)

    if cmd == "config":
        key, value = a.get("key"), a.get("value")
        if key is None:
            return res(True, data={k: v for k, v in cfg.data.items() if k != "library"})
        if key not in ("pause_when_away", "muted", "volume", "fit", "speed"):
            return res(False, "Chaves válidas: pause_when_away, muted, volume, fit, speed")
        if value is None:
            return res(True, data={key: cfg[key]})
        try:
            if key in ("pause_when_away", "muted"):
                cfg[key] = parse_bool(value)
            elif key == "volume":
                cfg[key] = max(0, min(100, int(value)))
            elif key == "speed":
                cfg[key] = round(max(SPEED_MIN, min(SPEED_MAX, float(str(value).replace(",", ".").rstrip("xX")))), 2)
            else:
                if value not in FITS:
                    raise ValueError(f"fit deve ser um de: {', '.join(FITS)}")
                cfg[key] = value
        except ValueError as e:
            return res(False, str(e))
        cfg.save()
        return res(True, f"{key} = {cfg[key]}")

    if cmd == "autostart":
        state = a["state"]
        if state == "status":
            return res(True, "Ativado." if autostart_get() else "Desativado.")
        autostart_set(state == "on")
        return res(True, "Iniciar com o Windows: " + ("ativado." if state == "on" else "desativado."))

    return res(False, f"Comando desconhecido: {cmd}")


# --------------------------------------------------------------------------
# Cliente IPC (CLI -> daemon)
# --------------------------------------------------------------------------
def send(cmd: str, args: dict | None = None) -> dict | None:
    try:
        with Client(PIPE, family="AF_PIPE", authkey=AUTHKEY) as c:
            c.send((cmd, args or {}))
            return c.recv()
    except (OSError, EOFError, AuthenticationError):
        return None


def dispatch(cmd: str, args: dict) -> int:
    r = send(cmd, args)
    if r is None:  # daemon não está rodando
        if cmd == "status":
            print("Parado.")
            return 0
        if cmd in DAEMON_ONLY:
            print("O aplicativo não está em execução. Inicie com: avenza start")
            return 1
        r = config_command(Config(), cmd, args)
        if cmd in ("set", "next", "prev", "random") and r["ok"]:
            r["msg"] += " (será aplicado na próxima inicialização)"

    data = r.get("data")
    if cmd == "list" and data is not None:
        if not data["library"]:
            print("Biblioteca vazia.")
        for i, p in enumerate(data["library"], 1):
            print(f"{'*' if p == data['current'] else ' '} {i}. {p}")
    elif data is not None:
        for k, v in data.items():
            print(f"{k}: {v}")
    if r.get("msg"):
        print(r["msg"])
    return 0 if r["ok"] else 1


def start_detached() -> int:
    if send("ping"):
        print("Avenza já está em execução.")
        return 0
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        launch_args(), creationflags=flags, close_fds=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        time.sleep(0.1)
        if send("ping"):
            print("Avenza iniciado (ícone na bandeja).")
            return 0
    print("Não foi possível confirmar a inicialização.")
    return 1


# --------------------------------------------------------------------------
# Daemon: janela no WorkerW + bandeja
# --------------------------------------------------------------------------
def run_daemon() -> int:
    if send("ping"):
        print("Avenza já está em execução.")
        return 0

    from PyQt6.QtCore import QObject, QPoint, QRect, Qt, QTimer, QUrl, pyqtSignal
    from PyQt6.QtGui import (QActionGroup, QColor, QCursor, QFont, QGuiApplication, QIcon,
                             QMovie, QPainter, QPixmap)
    from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PyQt6.QtMultimediaWidgets import QVideoWidget
    from PyQt6.QtWidgets import QApplication, QFileDialog, QMenu, QSystemTrayIcon, QWidget

    # ---- Win32 -----------------------------------------------------------
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    HWND, UINT = wintypes.HWND, wintypes.UINT
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, HWND, wintypes.LPARAM)

    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = HWND
    user32.FindWindowExW.argtypes = [HWND, HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowExW.restype = HWND
    user32.SendMessageTimeoutW.argtypes = [HWND, UINT, wintypes.WPARAM, wintypes.LPARAM,
                                           UINT, UINT, ctypes.POINTER(ctypes.c_size_t)]
    user32.SendMessageTimeoutW.restype = wintypes.LPARAM
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.SetParent.argtypes = [HWND, HWND]
    user32.SetParent.restype = HWND
    user32.GetParent.argtypes = [HWND]
    user32.GetParent.restype = HWND
    user32.GetAncestor.argtypes = [HWND, UINT]
    user32.GetAncestor.restype = HWND
    user32.SetForegroundWindow.argtypes = [HWND]
    user32.IsWindow.argtypes = [HWND]
    user32.GetClientRect.argtypes = [HWND, ctypes.POINTER(wintypes.RECT)]
    user32.SetWindowPos.argtypes = [HWND, HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, UINT]
    user32.GetForegroundWindow.restype = HWND
    user32.GetClassNameW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.SystemParametersInfoW.argtypes = [UINT, UINT, wintypes.LPCWSTR, UINT]

    def find_workerw():
        progman = user32.FindWindowW("Progman", None)
        if not progman:
            return None
        # Pede ao Progman para criar o WorkerW que fica atrás dos ícones
        out = ctypes.c_size_t()
        user32.SendMessageTimeoutW(progman, 0x052C, 0xD, 0x1, 0, 1000, ctypes.byref(out))
        # Windows 11 24H2+: o WorkerW é filho do Progman
        child = user32.FindWindowExW(progman, None, "WorkerW", None)
        if child:
            return child
        # Layout clássico: WorkerW irmão do WorkerW que contém o SHELLDLL_DefView
        found = []

        def cb(hwnd, _):
            if user32.FindWindowExW(hwnd, None, "SHELLDLL_DefView", None):
                w = user32.FindWindowExW(None, hwnd, "WorkerW", None)
                if w:
                    found.append(w)
            return True

        user32.EnumWindows(WNDENUMPROC(cb), 0)
        return found[0] if found else None

    def attach_to_workerw(hwnd):
        parent = find_workerw()
        if not parent:
            return None
        user32.SetParent(hwnd, parent)
        r = wintypes.RECT()
        user32.GetClientRect(parent, ctypes.byref(r))
        # SWP_NOACTIVATE | SWP_SHOWWINDOW: cobre toda a área virtual (multi-monitor)
        user32.SetWindowPos(hwnd, None, 0, 0, r.right - r.left, r.bottom - r.top, 0x0010 | 0x0040)
        return parent

    # ---- Janela do wallpaper --------------------------------------------
    class WallpaperWindow(QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                                | Qt.WindowType.WindowDoesNotAcceptFocus)
            self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            self.setGeometry(0, 0, 640, 360)
            self.kind = None
            self.pix = None
            self.movie = None
            self.fit = "fill"
            self.speed = 1.0
            self.video = QVideoWidget(self)
            self.video.hide()
            self.audio = QAudioOutput(self)
            self.player = QMediaPlayer(self)
            self.player.setAudioOutput(self.audio)
            self.player.setVideoOutput(self.video)
            self.player.setLoops(QMediaPlayer.Loops.Infinite)
            # O Qt pode descartar a taxa ao carregar/retomar a mídia: reaplica.
            self.player.mediaStatusChanged.connect(lambda _: self._sync_rate())
            self.player.playbackStateChanged.connect(lambda _: self._sync_rate())

        def set_fit(self, fit):
            self.fit = fit
            modes = {
                "fill": Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                "fit": Qt.AspectRatioMode.KeepAspectRatio,
                "center": Qt.AspectRatioMode.KeepAspectRatio,
                "stretch": Qt.AspectRatioMode.IgnoreAspectRatio,
            }
            self.video.setAspectRatioMode(modes[fit])
            self.update()

        def set_audio(self, muted, volume):
            self.audio.setMuted(muted)
            self.audio.setVolume(volume / 100)

        def _sync_rate(self):
            if self.kind == "video" and abs(self.player.playbackRate() - self.speed) > 0.001:
                self.player.setPlaybackRate(self.speed)

        def set_speed(self, rate):
            self.speed = rate
            self.player.setPlaybackRate(rate)
            if self.movie:
                self.movie.setSpeed(int(rate * 100))  # 100 = velocidade normal

        def clear(self):
            """Libera a mídia atual (e a memória associada)."""
            self.player.stop()
            self.player.setSource(QUrl())
            self.video.hide()
            if self.movie:
                self.movie.stop()
                self.movie.deleteLater()
            self.movie = self.pix = self.kind = None
            self.update()

        def load(self, path):
            self.clear()
            ext = Path(path).suffix.lower()
            if ext in VIDEO_EXT:
                self.kind = "video"
                self.video.show()
                self.player.setSource(QUrl.fromLocalFile(path))
                self.player.play()
            elif ext in GIF_EXT:
                self.kind = "gif"
                self.movie = QMovie(path)
                self.movie.frameChanged.connect(lambda _: self.update())
                self.movie.start()
            else:
                self.kind = "image"
                self.pix = QPixmap(path)
            self.set_speed(self.speed)
            self.update()

        def set_paused(self, paused):
            if self.kind == "video":
                self.player.pause() if paused else self.player.play()
            elif self.kind == "gif" and self.movie:
                self.movie.setPaused(paused)

        def resizeEvent(self, e):
            self.video.setGeometry(self.rect())

        def paintEvent(self, e):
            p = QPainter(self)
            p.fillRect(self.rect(), Qt.GlobalColor.black)
            pm = self.movie.currentPixmap() if self.movie else self.pix
            if pm is None or pm.isNull():
                return
            W, H, w, h = self.width(), self.height(), pm.width(), pm.height()
            if self.fit == "stretch":
                target = QRect(0, 0, W, H)
            else:
                s = {"fill": max(W / w, H / h), "fit": min(W / w, H / h)}.get(self.fit, 1.0)
                tw, th = int(w * s), int(h * s)
                target = QRect((W - tw) // 2, (H - th) // 2, tw, th)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawPixmap(target, pm)

    def make_icon():
        pm = QPixmap(64, 64)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#3b82f6"))
        p.drawRoundedRect(4, 4, 56, 56, 14, 14)
        p.setPen(QColor("white"))
        p.setFont(QFont("Segoe UI", 28, QFont.Weight.Bold))
        p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "W")
        p.end()
        return QIcon(pm)

    # ---- Engine ----------------------------------------------------------
    class Engine(QObject):
        request = pyqtSignal(object)

        def __init__(self, app):
            super().__init__()
            self.app = app
            self.cfg = Config()
            self.manual_pause = False
            self.paused = False
            self.on_desktop = True
            self.parent_hwnd = None
            self.new_window()

            self.tray = QSystemTrayIcon(make_icon(), app)
            self.tray.setToolTip(APP)
            self.menu = QMenu()
            self.tray.activated.connect(self.on_tray)
            self.tray.show()

            self.request.connect(self.on_request)
            threading.Thread(target=self.serve, daemon=True).start()

            self.poll_timer = QTimer(self)
            self.poll_timer.timeout.connect(self.poll)
            self.poll_timer.start(400)
            self.watch_timer = QTimer(self)
            self.watch_timer.timeout.connect(self.watchdog)
            self.watch_timer.start(3000)

            app.aboutToQuit.connect(self.shutdown)
            self.load_current()

        # -- janela
        def new_window(self):
            self.win = WallpaperWindow()
            self.apply_settings()

        def attach(self):
            self.parent_hwnd = attach_to_workerw(int(self.win.winId()))

        def apply_settings(self):
            self.win.set_fit(self.cfg["fit"])
            self.win.set_audio(self.cfg["muted"], self.cfg["volume"])
            self.win.set_speed(self.cfg["speed"])
            self.update_state()

        def load_current(self):
            path = self.cfg["current"]
            if not path or not os.path.isfile(path):
                self.win.clear()
                self.win.hide()
                return
            self.win.load(path)
            self.win.show()
            self.attach()
            if Path(path).suffix.lower() in SPI_EXT:
                # Também define como wallpaper nativo: permanece ao encerrar o app
                user32.SystemParametersInfoW(0x0014, 0, path, 0x03)
            self.paused = False
            self.update_state()

        def watchdog(self):
            """Reanexa se o Explorer reiniciar (o WorkerW é recriado)."""
            if self.menu.isVisible():  # nunca mexe em janelas com o menu aberto
                return
            hwnd = int(self.win.winId())
            if not user32.IsWindow(hwnd):
                self.win.deleteLater()
                self.new_window()
                self.load_current()
                return
            if self.win.isVisible() and (
                not self.parent_hwnd
                or not user32.IsWindow(self.parent_hwnd)
                or user32.GetAncestor(hwnd, 1) != self.parent_hwnd  # GA_PARENT
            ):
                self.attach()

        # -- pausa automática
        def poll(self):
            fg = user32.GetForegroundWindow()
            if not fg:
                return
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
            if pid.value == os.getpid():  # menu da bandeja etc.: mantém o estado
                return
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(fg, buf, 256)
            on = buf.value in DESKTOP_CLASSES
            if on != self.on_desktop:
                self.on_desktop = on
                self.update_state()

        def pause_reason(self):
            if self.manual_pause:
                return "manual"
            if self.cfg["pause_when_away"] and not self.on_desktop:
                return "fora da área de trabalho"
            return None

        def update_state(self):
            should = self.pause_reason() is not None
            if should != self.paused:
                self.paused = should
                self.win.set_paused(should)

        # -- IPC
        def serve(self):
            try:
                listener = Listener(PIPE, family="AF_PIPE", authkey=AUTHKEY)
            except OSError:
                return
            while True:
                try:
                    conn = listener.accept()
                except Exception:
                    continue
                with conn:
                    try:
                        cmd, args = conn.recv()
                        q: queue.Queue = queue.Queue()
                        self.request.emit((cmd, args, q))
                        conn.send(q.get(timeout=10))
                    except Exception:
                        pass

        def on_request(self, item):
            cmd, args, q = item
            try:
                q.put(self.execute(cmd, args))
            except Exception as e:  # nunca deixar o cliente pendurado
                q.put(res(False, f"Erro: {e}"))

        def execute(self, cmd, a):
            if cmd == "ping":
                return res(True, "pong")
            if cmd == "quit":
                QTimer.singleShot(100, self.app.quit)
                return res(True, "Encerrando.")
            if cmd in ("pause", "resume"):
                self.manual_pause = cmd == "pause"
                self.update_state()
                return res(True, "Animação pausada." if self.manual_pause else "Animação retomada.")
            if cmd == "status":
                return res(True, data={
                    "atual": self.cfg["current"],
                    "pausado": self.paused,
                    "motivo": self.pause_reason(),
                    "biblioteca": len(self.cfg["library"]),
                })
            r = config_command(self.cfg, cmd, a)
            if r.get("reload"):
                self.load_current()
            self.apply_settings()
            return r

        # -- bandeja
        def on_tray(self, reason):
            R = QSystemTrayIcon.ActivationReason
            if reason in (R.Trigger, R.Context):  # clique esquerdo ou direito
                self.show_menu()

        def show_menu(self):
            """Abre o menu ancorado acima do ícone (cai para baixo só se não couber)."""
            self.populate()
            m = self.menu
            size = m.sizeHint()
            geo = self.tray.geometry()
            cur = QCursor.pos()
            anchor = geo if (geo.isValid() and not geo.isEmpty()) else QRect(cur.x(), cur.y(), 1, 1)
            screen = QGuiApplication.screenAt(anchor.center()) or QGuiApplication.primaryScreen()
            avail = screen.availableGeometry()
            x = max(avail.left(), min(anchor.center().x() - size.width() // 2,
                                      avail.right() - size.width()))
            y = min(anchor.top(), avail.bottom()) - size.height()
            if y < avail.top():  # barra no topo: sem espaço acima
                y = max(anchor.bottom(), avail.top())
            m.popup(QPoint(x, y))
            m.activateWindow()
            user32.SetForegroundWindow(int(m.winId()))  # fecha ao clicar fora

        def add_dialog(self):
            flt = "Mídia (" + " ".join("*" + e for e in sorted(MEDIA_EXT)) + ")"
            files, _ = QFileDialog.getOpenFileNames(None, "Adicionar wallpapers", "", flt)
            if files:
                self.execute("add", {"paths": files})

        def populate(self):
            m = self.menu
            m.clear()
            lib, cur = self.cfg["library"], self.cfg["current"]

            wp = m.addMenu("Trocar wallpaper")
            if not lib:
                wp.addAction("(biblioteca vazia)").setEnabled(False)
            for i, p in enumerate(lib):
                act = wp.addAction(Path(p).name)
                act.setCheckable(True)
                act.setChecked(p == cur)
                act.triggered.connect(lambda _=False, i=i: self.execute("set", {"target": str(i + 1)}))
            wp.addSeparator()
            wp.addAction("Próximo").triggered.connect(lambda: self.execute("next", {}))
            wp.addAction("Aleatório").triggered.connect(lambda: self.execute("random", {}))
            wp.addAction("Adicionar arquivos…").triggered.connect(self.add_dialog)

            pause = m.addAction("Retomar animação" if self.manual_pause else "Pausar animação")
            pause.triggered.connect(
                lambda: self.execute("resume" if self.manual_pause else "pause", {}))

            sp = m.addMenu("Velocidade da animação")
            sgroup = QActionGroup(sp)
            sgroup.setExclusive(True)
            for sv in SPEEDS:
                act = sp.addAction(f"{sv:g}x" + (" (normal)" if sv == 1.0 else ""))
                act.setCheckable(True)
                act.setChecked(abs(self.cfg["speed"] - sv) < 0.01)
                sgroup.addAction(act)
                act.triggered.connect(
                    lambda _=False, sv=sv: self.execute("config", {"key": "speed", "value": str(sv)}))

            st = m.addMenu("Configurações")
            auto = st.addAction("Iniciar com o Windows")
            auto.setCheckable(True)
            auto.setChecked(autostart_get())
            auto.triggered.connect(
                lambda v: self.execute("autostart", {"state": "on" if v else "off"}))
            for label, key in (("Pausar fora da área de trabalho", "pause_when_away"),
                               ("Silenciar áudio", "muted")):
                act = st.addAction(label)
                act.setCheckable(True)
                act.setChecked(self.cfg[key])
                act.triggered.connect(
                    lambda v, k=key: self.execute("config", {"key": k, "value": "on" if v else "off"}))
            fit_menu = st.addMenu("Ajuste da imagem")
            group = QActionGroup(fit_menu)
            group.setExclusive(True)
            names = {"fill": "Preencher", "fit": "Ajustar", "stretch": "Esticar", "center": "Centralizar"}
            for f in FITS:
                act = fit_menu.addAction(names[f])
                act.setCheckable(True)
                act.setChecked(self.cfg["fit"] == f)
                group.addAction(act)
                act.triggered.connect(
                    lambda _=False, f=f: self.execute("config", {"key": "fit", "value": f}))
            st.addSeparator()
            st.addAction("Abrir pasta de configuração").triggered.connect(self.open_config_dir)

            m.addSeparator()
            m.addAction("Sair").triggered.connect(lambda: self.execute("quit", {}))

        def open_config_dir(self):
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(CONFIG_DIR)  # type: ignore[attr-defined]

        def shutdown(self):
            self.win.clear()
            self.tray.hide()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    engine = Engine(app)  # noqa: F841  (mantém a referência viva)
    return app.exec()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="avenza",
        description="Wallpaper animado para Windows (WorkerW), controlado por CLI e bandeja.",
    )
    sub = p.add_subparsers(dest="cmd", required=True, metavar="comando")

    sub.add_parser("start", help="inicia o app em segundo plano (ícone na bandeja)")
    sub.add_parser("run", help="executa em primeiro plano (usado pelo autostart)")
    sub.add_parser("quit", aliases=["stop"], help="encerra o app")
    sub.add_parser("status", help="mostra o estado do app")
    sub.add_parser("list", help="lista a biblioteca de wallpapers")

    s = sub.add_parser("add", help="adiciona arquivos ou pastas à biblioteca")
    s.add_argument("paths", nargs="+")
    s = sub.add_parser("remove", help="remove da biblioteca (número, nome ou caminho)")
    s.add_argument("target")
    s = sub.add_parser("set", help="define o wallpaper (número, nome ou caminho)")
    s.add_argument("target")

    sub.add_parser("next", help="próximo wallpaper")
    sub.add_parser("prev", help="wallpaper anterior")
    sub.add_parser("random", help="wallpaper aleatório")
    sub.add_parser("pause", help="pausa a animação manualmente")
    sub.add_parser("resume", help="retoma a animação")

    s = sub.add_parser("config", help="mostra ou altera configurações")
    s.add_argument("key", nargs="?", help="pause_when_away | muted | volume | fit | speed")
    s.add_argument("value", nargs="?")

    s = sub.add_parser("autostart", help="iniciar com o Windows")
    s.add_argument("state", choices=["on", "off", "status"])
    return p


def main() -> int:
    args = build_parser().parse_args()
    cmd = "quit" if args.cmd == "stop" else args.cmd
    if cmd == "run":
        return run_daemon()
    if cmd == "start":
        return start_detached()
    payload = {
        "add": lambda: {"paths": args.paths},
        "remove": lambda: {"target": args.target},
        "set": lambda: {"target": args.target},
        "config": lambda: {"key": args.key, "value": args.value},
        "autostart": lambda: {"state": args.state},
    }.get(cmd, lambda: {})()
    return dispatch(cmd, payload)


if __name__ == "__main__":
    sys.exit(main())
