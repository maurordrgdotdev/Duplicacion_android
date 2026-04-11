#!/usr/bin/env python3
"""
Duplicación Android: busca hosts con ADB TCP (5555), conecta y lanza scrcpy (opcional -S apaga la pantalla).

.app (macOS): al conectar, la ventana de la lista se oculta por completo (withdraw); la app sigue
en el Dock como «Duplicación Android». El espejo se arranca con «open -a» al .app anidado LSUIElement
para que macOS no trate scrcpy como app suelta (menos iconos fantasma). Instancia única 127.0.0.1:49819.

CLI: python3 DuplicacionAndroid.py  |  macOS con ventana: añade --gui
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

TITLE = "Duplicación Android"
_SINGLETON_PORT = 49819


def _frozen() -> bool:
    return getattr(sys, "frozen", False) is True


def _ensure_path_for_bundled_app() -> None:
    if not _frozen():
        return
    extra = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    os.environ["PATH"] = extra + os.pathsep + os.environ.get("PATH", "")


def app_resources_dir() -> Path:
    if _frozen():
        exe = Path(sys.executable).resolve()
        if exe.parent.name == "MacOS":
            return exe.parent.parent / "Resources"
        return exe.parent.parent
    return Path(__file__).resolve().parent


def bundled_dir() -> Path:
    return app_resources_dir() / "bundled"


def macos_executable_dir() -> Optional[Path]:
    if not _frozen():
        return None
    exe = Path(sys.executable).resolve()
    if exe.parent.name == "MacOS":
        return exe.parent
    return None


def main_bundle_contents_dir() -> Optional[Path]:
    md = macos_executable_dir()
    if md is None:
        return None
    return md.parent


def nested_mirror_launcher_path() -> Optional[Path]:
    contents = main_bundle_contents_dir()
    if contents is None:
        return None
    p = (
        contents
        / "Frameworks"
        / "DuplicacionAndroidEspejo.app"
        / "Contents"
        / "MacOS"
        / "DuplicacionEspejo"
    )
    if p.is_file():
        return p.resolve()
    return None


def nested_mirror_app_bundle_path() -> Optional[Path]:
    """Ruta al .app hijo (LSUIElement); «open -a» respeta LSUIElement pero el proceso open miente con el exit code."""
    contents = main_bundle_contents_dir()
    if contents is None:
        return None
    p = contents / "Frameworks" / "DuplicacionAndroidEspejo.app"
    if p.is_dir():
        return p.resolve()
    return None


def nested_mirror_scrcpy_binary_path() -> Optional[Path]:
    """Mach-O scrcpy dentro del .app espejo (mismo binario que lanza el script DuplicacionEspejo)."""
    contents = main_bundle_contents_dir()
    if contents is None:
        return None
    p = (
        contents
        / "Frameworks"
        / "DuplicacionAndroidEspejo.app"
        / "Contents"
        / "MacOS"
        / "DuplicacionAndroidMirror"
    )
    if p.is_file():
        return p.resolve()
    return None


def resolve_adb() -> Optional[str]:
    p = bundled_dir() / "adb"
    if p.is_file():
        return str(p.resolve())
    _ensure_path_for_bundled_app()
    return shutil.which("adb")


def resolve_android_sdk_root() -> Optional[Path]:
    for key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        raw = os.environ.get(key, "").strip()
        if raw:
            p = Path(raw).expanduser()
            if p.is_dir():
                return p.resolve()
    home = Path.home()
    for guess in (home / "Library/Android/sdk", home / "Android/Sdk"):
        if guess.is_dir():
            return guess.resolve()
    return None


def resolve_emulator_executable() -> Optional[str]:
    sdk = resolve_android_sdk_root()
    if sdk:
        exe = sdk / "emulator" / "emulator"
        if exe.is_file():
            return str(exe.resolve())
    _ensure_path_for_bundled_app()
    return shutil.which("emulator")


def listar_avds_del_sdk() -> tuple[List[str], Optional[str]]:
    """Nombres de AVD según «emulator -list-avds»."""
    emu = resolve_emulator_executable()
    if not emu:
        return [], None
    r = subprocess.run(
        [emu, "-list-avds"],
        capture_output=True,
        text=True,
        timeout=45,
    )
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        return [], err or "emulator -list-avds falló"
    names = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    return names, None


def avd_nombre_desde_serial_emulador(adb_bin: str, serial: str) -> Optional[str]:
    if not serial.startswith("emulator-"):
        return None
    try:
        r = subprocess.run(
            [adb_bin, "-s", serial, "emu", "avd", "name"],
            capture_output=True,
            text=True,
            timeout=12,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
    return lines[-1] if lines else None


def seriales_emulador_en_adb(adb_bin: str) -> List[str]:
    r = subprocess.run(
        [adb_bin, "devices"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if r.returncode != 0:
        return []
    out: List[str] = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        if state == "device" and serial.startswith("emulator-"):
            out.append(serial)
    return sorted(out)


def esperar_serial_emulador_nuevo(
    adb_bin: str, antes: set[str], timeout_s: float = 180.0
) -> Optional[str]:
    """Tras arrancar un AVD, devuelve un serial emulator-* nuevo en estado device."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        time.sleep(2.0)
        ahora = set(seriales_emulador_en_adb(adb_bin))
        candidatos = ahora - antes
        for ser in sorted(candidatos):
            if _adb_serial_en_estado_device(adb_bin, ser):
                return ser
    return None


def _serial_parece_adb_por_red(serial: str) -> bool:
    """True si el serial tiene forma IPv4:puerto (dispositivo ya visto por ADB en red)."""
    if ":" not in serial:
        return False
    host, _, port_s = serial.rpartition(":")
    try:
        ipaddress.IPv4Address(host)
        int(port_s)
        return True
    except ValueError:
        return False


def _adb_serial_en_estado_device(adb_bin: str, serial: str) -> bool:
    r = subprocess.run(
        [adb_bin, "devices"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if r.returncode != 0:
        return False
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == serial and parts[1] == "device":
            return True
    return False


def obtener_nombre_dispositivo_adb(
    adb_bin: str, serial: str, intentar_connect: bool
) -> Optional[str]:
    """Fabricante + modelo (getprop). Si hace falta, adb connect para seriales IP:puerto."""
    try:
        if not _adb_serial_en_estado_device(adb_bin, serial):
            if not intentar_connect:
                return None
            cr = subprocess.run(
                [adb_bin, "connect", serial],
                capture_output=True,
                text=True,
                timeout=14,
            )
            if cr.returncode != 0:
                return None
        if not _adb_serial_en_estado_device(adb_bin, serial):
            return None
        man = subprocess.run(
            [adb_bin, "-s", serial, "shell", "getprop", "ro.product.manufacturer"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        mod = subprocess.run(
            [adb_bin, "-s", serial, "shell", "getprop", "ro.product.model"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        m = (man.stdout or "").strip()
        mo = (mod.stdout or "").strip()
        nombre = f"{m} {mo}".strip()
        return nombre if nombre else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def etiqueta_fila_dispositivo(
    serial: str,
    nombre: Optional[str],
    kind: str,
) -> str:
    """kind: usb | tcp | adb | emu | avd_off."""
    suf_nombre = f" — {nombre}" if nombre else ""
    if kind == "usb":
        return f"{serial}{suf_nombre}  [USB · cable]"
    if kind == "tcp":
        return f"{serial}{suf_nombre}  [Wi‑Fi · escaneo de red]"
    if kind == "adb":
        return f"{serial}{suf_nombre}  [Wi‑Fi · ya en adb, sin escaneo]"
    if kind == "emu":
        return f"{serial}{suf_nombre}  [Emulador · en ejecución]"
    if kind == "avd_off":
        avd = serial[4:] if serial.startswith("avd:") else serial
        return f"{avd}{suf_nombre}  [Emulador · apagado — Conectar solo arranca el AVD]"
    return f"{serial}{suf_nombre}  [Wi‑Fi · ya en adb, sin escaneo]"


def clasificar_dispositivos_adb(adb_bin: str) -> tuple[List[str], List[str], Optional[str]]:
    """Una sola llamada a «adb devices»: seriales USB/cable vs IP:puerto (estado «device»)."""
    r = subprocess.run(
        [adb_bin, "devices"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if r.returncode != 0:
        return [], [], (r.stderr or r.stdout or "adb devices falló").strip() or "adb devices falló"
    usbs: List[str] = []
    reds: List[str] = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        if state != "device":
            continue
        if _serial_parece_adb_por_red(serial):
            reds.append(serial)
        else:
            usbs.append(serial)
    return usbs, reds, None


def _orden_serial_red(serial: str) -> Tuple[int, ...]:
    if not _serial_parece_adb_por_red(serial):
        return (999,)
    host, _, ps = serial.rpartition(":")
    return tuple(int(x) for x in host.split(".")) + (int(ps),)


def resolve_scrcpy() -> Optional[str]:
    nested_bin = nested_mirror_scrcpy_binary_path()
    if nested_bin is not None:
        return str(nested_bin)
    nested = nested_mirror_launcher_path()
    if nested is not None:
        return str(nested)
    md = macos_executable_dir()
    if md:
        mirror = md / "DuplicacionAndroidMirror"
        if mirror.is_file():
            return str(mirror.resolve())
    p = bundled_dir() / "scrcpy"
    if p.is_file():
        return str(p.resolve())
    _ensure_path_for_bundled_app()
    return shutil.which("scrcpy")


def _hay_binario_scrcpy_embebido() -> bool:
    if nested_mirror_scrcpy_binary_path() is not None:
        return True
    if nested_mirror_app_bundle_path() is not None:
        return True
    if nested_mirror_launcher_path() is not None:
        return True
    md = macos_executable_dir()
    if md and (md / "DuplicacionAndroidMirror").is_file():
        return True
    return (bundled_dir() / "scrcpy").is_file()


def scrcpy_subprocess_env() -> dict:
    env = _scrcpy_env()
    bd = bundled_dir().resolve()
    path_bits: List[str] = []
    adb_path = bd / "adb"
    if adb_path.is_file():
        # scrcpy llama a «adb» por PATH; ADB es la forma explícita que documenta scrcpy.
        env["ADB"] = str(adb_path)
        path_bits.append(str(bd))
    if _frozen() and sys.platform == "darwin":
        path_bits.extend(["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"])
    if path_bits:
        env["PATH"] = os.pathsep.join(path_bits + [env.get("PATH", "")])

    if not _frozen() or sys.platform != "darwin":
        return env
    if not _hay_binario_scrcpy_embebido():
        return env
    roots = ["/opt/homebrew/lib", "/usr/local/lib"]
    prev = env.get("DYLD_LIBRARY_PATH", "").strip()
    env["DYLD_LIBRARY_PATH"] = ":".join(roots + ([prev] if prev else []))
    server = bd / "scrcpy-server"
    if server.is_file():
        env["SCRCPY_SERVER_PATH"] = str(server.resolve())
    return env


def obtener_ip_local() -> Optional[str]:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def candidatos_red_desde_ip(local_ip: str, prefijo_bits: int) -> List[str]:
    red = ipaddress.IPv4Network(f"{local_ip}/{prefijo_bits}", strict=False)
    hosts = [str(h) for h in red.hosts()]
    if local_ip in hosts:
        hosts.remove(local_ip)
    return hosts


def puerto_abierto(ip: str, puerto: int, timeout_s: float) -> bool:
    try:
        with socket.create_connection((ip, puerto), timeout=timeout_s):
            return True
    except OSError:
        return False


def escanear_puerto_5555(
    ips: List[str],
    workers: int,
    timeout_s: float,
) -> List[str]:
    encontrados: List[str] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futuros = {ex.submit(puerto_abierto, ip, 5555, timeout_s): ip for ip in ips}
        for fut in as_completed(futuros):
            ip = futuros[fut]
            try:
                if fut.result():
                    encontrados.append(ip)
            except Exception:
                pass
    encontrados.sort(key=lambda x: [int(p) for p in x.split(".")])
    return encontrados


def ejecutar(cmd: Sequence[str]) -> int:
    print(f"→ {' '.join(cmd)}")
    return subprocess.call(list(cmd))


def _scrcpy_env() -> dict:
    env = os.environ.copy()
    png = app_resources_dir() / "DuplicacionAndroidIcon.png"
    if png.is_file():
        env["SCRCPY_ICON_PATH"] = str(png.resolve())
    return env


def construir_argumentos_scrcpy(
    serial: str,
    apagar_pantalla_fisica: bool,
    compat_miu: bool,
) -> List[str]:
    """
    Flags para evitar ventana negra y falta de toque en Xiaomi/Redmi/MIUI:
    - -w mantiene el dispositivo despierto.
    - --render-driver=metal en macOS suele arreglar SDL en negro.
    - --mouse=uhid --keyboard=uhid evita el modo SDK que MIUI suele bloquear
      (además activa «Depuración USB (ajustes de seguridad)» en el móvil).
    Con compat_miu activo no se usa -S aunque el usuario lo pida: en muchos
    Xiaomi -S deja el vídeo en negro y sin control.
    """
    args: List[str] = []
    if apagar_pantalla_fisica and not compat_miu:
        args.append("-S")
    args.append("-w")
    if sys.platform == "darwin":
        args.extend(["--render-driver=metal"])
    if compat_miu:
        args.extend(["--mouse=uhid", "--keyboard=uhid"])
    args.extend(["-s", serial, f"--window-title={TITLE}"])
    return args


def _leer_y_borrar_log_scrcpy(path: str) -> str:
    try:
        with open(path, "rb") as f:
            text = f.read(8000).decode(errors="replace").strip()
    except OSError:
        text = ""
    try:
        os.unlink(path)
    except OSError:
        pass
    return text


def popen_scrcpy(
    serial: str,
    apagar_pantalla_fisica: bool = False,
    compat_miu: bool = False,
) -> tuple[subprocess.Popen, str]:
    """Devuelve (proceso, ruta de log stderr temporal para diagnóstico)."""
    # Importante: NO usar solo «open -W -a …»: el PID es «open», que a menudo termina con
    # código 0 al instante aunque scrcpy siga vivo → falso «scrcpy terminó antes de estabilizarse».
    # Ejecutar el Mach-O del bundle espejo + scrcpy_subprocess_env() (ADB y server embebidos).
    nested_scrcpy = nested_mirror_scrcpy_binary_path()
    if _frozen() and sys.platform == "darwin" and nested_scrcpy is not None:
        exe = str(nested_scrcpy)
    else:
        bin_scrcpy = resolve_scrcpy()
        if not bin_scrcpy:
            raise FileNotFoundError("scrcpy")
        exe = bin_scrcpy
    cmd = [exe] + construir_argumentos_scrcpy(
        serial, apagar_pantalla_fisica, compat_miu
    )
    fd, err_path = tempfile.mkstemp(prefix="dupandr-scrcpy-", suffix=".log", text=False)
    os.close(fd)
    err_f = open(err_path, "wb")
    try:
        proc = subprocess.Popen(
            cmd,
            env=scrcpy_subprocess_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=err_f,
            start_new_session=True,
        )
    finally:
        err_f.close()
    return proc, err_path


def _darwin_single_instance_or_exit() -> None:
    """Un solo proceso; si el puerto está ocupado, activar la otra instancia y salir."""
    if not (_frozen() and sys.platform == "darwin"):
        return
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", _SINGLETON_PORT))
    except OSError:
        sock.close()
        subprocess.run(
            [
                "osascript",
                "-e",
                'tell application id "com.munakdigitall.DuplicacionAndroid" to activate',
            ],
            capture_output=True,
            timeout=10,
        )
        sys.exit(0)
    try:
        sock.listen(8)
    except OSError:
        sock.close()
        sys.exit(1)
    setattr(sys, "_dupandr_singleton_sock", sock)


def _resolver_ips(cidr: Optional[str], prefijo: int) -> tuple[Optional[List[str]], Optional[str]]:
    if cidr:
        try:
            red = ipaddress.IPv4Network(cidr, strict=False)
            return [str(h) for h in red.hosts()], None
        except ValueError:
            return None, f"Red inválida: {cidr}"
    local = obtener_ip_local()
    if not local:
        return None, "No se pudo detectar la IP local. Indica una red, p. ej. 192.168.0.0/24"
    return candidatos_red_desde_ip(local, prefijo), None


def run_gui(
    cidr: Optional[str],
    timeout_s: float,
    workers: int,
    prefijo: int,
) -> int:
    import tkinter as tk
    from tkinter import messagebox, ttk

    if not resolve_adb():
        messagebox.showerror(
            TITLE,
            "No hay «adb» embebido ni en el PATH.\n"
            "Recompila el .app con platform-tools disponibles o instala Android SDK.",
        )
        return 1
    if not resolve_scrcpy():
        messagebox.showerror(
            TITLE,
            "No hay «scrcpy» embebido ni en el PATH.\n"
            "Recompila con Homebrew (brew install scrcpy) o instálalo en el sistema.",
        )
        return 1

    root = tk.Tk()
    root.title(TITLE)
    root.geometry("640x620")
    root.minsize(560, 540)
    root.configure(bg="#eceff4")

    icon_png = app_resources_dir() / "DuplicacionAndroidIcon.png"
    if icon_png.is_file():
        try:
            img = tk.PhotoImage(file=str(icon_png))
            root.iconphoto(True, img)
            root._duplicacion_icon = img
        except tk.TclError:
            pass

    style = ttk.Style()
    if sys.platform == "darwin":
        try:
            style.theme_use("aqua")
        except tk.TclError:
            pass
    _f_base = "Helvetica Neue" if sys.platform == "darwin" else "Segoe UI"
    style.configure("Dup.TFrame", background="#eceff4")
    style.configure("DupCard.TLabelframe", background="#ffffff")
    style.configure("DupCard.TLabelframe.Label", font=(_f_base, 13, "bold"), foreground="#1a1d24")
    style.configure("Dup.TLabel", font=(_f_base, 12), foreground="#2e3440", background="#eceff4")
    style.configure("DupMuted.TLabel", font=(_f_base, 11), foreground="#5e6778", background="#eceff4")
    style.configure("DupHdr.TLabel", font=(_f_base, 20, "bold"), foreground="#1a1d24", background="#eceff4")
    style.configure("Dup.TButton", font=(_f_base, 12), padding=(10, 6))
    style.configure("DupAccent.TButton", font=(_f_base, 12, "bold"), padding=(12, 8))
    style.map("DupAccent.TButton", foreground=[("disabled", "#888")])

    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    outer = ttk.Frame(root, padding=(20, 16, 20, 20), style="Dup.TFrame")
    outer.grid(row=0, column=0, sticky="nsew")
    outer.columnconfigure(0, weight=1)
    outer.rowconfigure(2, weight=1)

    ttk.Label(outer, text=TITLE, style="DupHdr.TLabel").grid(
        row=0, column=0, sticky="w", pady=(0, 4)
    )
    ttk.Label(
        outer,
        text="Dispositivos físicos, red ADB y emuladores del SDK.",
        style="DupMuted.TLabel",
    ).grid(row=1, column=0, sticky="w", pady=(0, 12))

    body = ttk.Frame(outer, style="Dup.TFrame")
    body.grid(row=2, column=0, sticky="nsew", pady=(0, 12))
    body.columnconfigure(0, weight=1)
    body.rowconfigure(1, weight=1)

    lf_opts = ttk.LabelFrame(body, text="Red y opciones de espejo", padding=(12, 10), style="DupCard.TLabelframe")
    lf_opts.grid(row=0, column=0, sticky="ew", pady=(0, 10))
    lf_opts.columnconfigure(1, weight=1)

    lf_dev = ttk.LabelFrame(body, text="Dispositivos", padding=(10, 8), style="DupCard.TLabelframe")
    lf_dev.grid(row=1, column=0, sticky="nsew")
    lf_dev.columnconfigure(0, weight=1)
    lf_dev.rowconfigure(1, weight=1)

    cidr_var = tk.StringVar(value=cidr or "")
    status = tk.StringVar(
        value="Pulsa «Buscar dispositivos» para listar USB, Wi‑Fi, emuladores y AVD del SDK."
    )
    ttk.Label(lf_opts, text="Red (opcional, vacío = /24 de tu IP):").grid(
        row=0, column=0, sticky="nw", padx=(0, 8), pady=(0, 4)
    )
    entry_cidr = ttk.Entry(lf_opts, textvariable=cidr_var, width=36)
    entry_cidr.grid(row=0, column=1, sticky="ew", pady=(0, 8))

    apagar_pantalla_var = tk.BooleanVar(value=False)
    compat_miu_var = tk.BooleanVar(value=True)
    ttk.Label(
        lf_opts,
        text=(
            "−S apaga la pantalla del móvil (en Xiaomi/Redmi suele romper control y vídeo). "
            "MIUI: activa compatibilidad y «Depuración USB (ajustes de seguridad)» en el teléfono."
        ),
        wraplength=520,
        justify=tk.LEFT,
    ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 6))
    chk_apagar = ttk.Checkbutton(
        lf_opts,
        text="Apagar pantalla del teléfono al duplicar (-S)",
        variable=apagar_pantalla_var,
    )
    chk_apagar.grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 4))
    chk_compat_miu = ttk.Checkbutton(
        lf_opts,
        text="Compatibilidad Xiaomi / MIUI (UHID + Metal; ignora -S)",
        variable=compat_miu_var,
    )
    chk_compat_miu.grid(row=3, column=0, columnspan=2, sticky="w")

    ttk.Label(lf_dev, textvariable=status, wraplength=520).grid(
        row=0, column=0, columnspan=2, sticky="w", pady=(0, 6)
    )
    lb_frame = ttk.Frame(lf_dev)
    lb_frame.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(0, 4))
    lb_frame.columnconfigure(0, weight=1)
    lb_frame.rowconfigure(0, weight=1)
    sb = ttk.Scrollbar(lb_frame, orient=tk.VERTICAL)
    listbox = tk.Listbox(
        lb_frame,
        height=8,
        exportselection=False,
        yscrollcommand=sb.set,
        font=(_f_base, 12),
        bg="#ffffff",
        fg="#1a1d24",
        selectbackground="#5e81ac",
        selectforeground="#ffffff",
        relief="flat",
        highlightthickness=1,
        highlightbackground="#d8dee9",
        highlightcolor="#5e81ac",
        activestyle="none",
    )
    sb.config(command=listbox.yview)
    listbox.grid(row=0, column=0, sticky="nsew")
    sb.grid(row=0, column=1, sticky="ns")

    btn_row = ttk.Frame(outer, style="Dup.TFrame")
    btn_row.grid(row=3, column=0, sticky="ew", pady=(4, 0))
    btn_row.columnconfigure(0, weight=1)

    # texto, serial adb, requiere adb connect, kind: usb|tcp|adb
    filas_actuales: List[Tuple[str, str, bool, str]] = []
    proceso_espejo: List[Optional[subprocess.Popen]] = [None]

    def ui_conexion_bloqueada(bloquear: bool) -> None:
        st = tk.DISABLED if bloquear else tk.NORMAL
        btn_scan.state(["disabled"] if bloquear else ["!disabled"])
        btn_connect.state(["disabled"] if bloquear else ["!disabled"])
        btn_tcpip_usb.state(["disabled"] if bloquear else ["!disabled"])
        chk_apagar.state(["disabled"] if bloquear else ["!disabled"])
        chk_compat_miu.state(["disabled"] if bloquear else ["!disabled"])
        try:
            listbox.config(state=st)
        except tk.TclError:
            pass
        entry_cidr.config(state=st)

    def set_status(msg: str) -> None:
        status.set(msg)
        root.update_idletasks()

    def do_scan() -> None:
        raw = cidr_var.get().strip()
        set_status("Buscando puerto 5555 en la red y leyendo dispositivos USB/ADB…")
        listbox.delete(0, tk.END)
        root.update_idletasks()
        adb_bin = resolve_adb()
        assert adb_bin

        def work() -> None:
            ips, err_red = _resolver_ips(raw or None, prefijo)
            usbs, reds_en_adb, err_adb = clasificar_dispositivos_adb(adb_bin)
            if err_red:
                root.after(
                    0,
                    lambda: on_scan_merge([], [], set(), err_red, None, []),
                )
                return
            assert ips is not None
            encontrados_tcp = escanear_puerto_5555(ips, workers, timeout_s)
            tcp_serials = {f"{ip}:5555" for ip in encontrados_tcp}
            wifi_serials = set(tcp_serials) | set(reds_en_adb)
            avd_nombres, _ = listar_avds_del_sdk()
            u_sorted = sorted(usbs)
            w_sorted = sorted(wifi_serials, key=_orden_serial_red)
            root.after(
                0,
                lambda: on_scan_merge(
                    u_sorted,
                    w_sorted,
                    tcp_serials,
                    None,
                    err_adb,
                    avd_nombres,
                ),
            )

        threading.Thread(target=work, daemon=True).start()

    def on_scan_merge(
        usbs: List[str],
        wifi_ordenados: List[str],
        tcp_serials: set[str],
        err_red: Optional[str],
        err_adb: Optional[str],
        avd_nombres: List[str],
    ) -> None:
        nonlocal filas_actuales
        filas_actuales = []
        listbox.delete(0, tk.END)
        if err_red:
            set_status(err_red)
            messagebox.showerror(TITLE, err_red)
            return
        physical = [s for s in usbs if not s.startswith("emulator-")]
        emus = [s for s in usbs if s.startswith("emulator-")]
        adb_b = resolve_adb()
        running_avd: set[str] = set()
        if adb_b:
            for ser in emus:
                an = avd_nombre_desde_serial_emulador(adb_b, ser)
                if an:
                    running_avd.add(an)
        for s in physical:
            filas_actuales.append(
                (etiqueta_fila_dispositivo(s, None, "usb"), s, False, "usb")
            )
        for s in sorted(emus):
            filas_actuales.append(
                (etiqueta_fila_dispositivo(s, None, "emu"), s, False, "emu")
            )
        for ser in wifi_ordenados:
            wkind = "tcp" if ser in tcp_serials else "adb"
            filas_actuales.append(
                (etiqueta_fila_dispositivo(ser, None, wkind), ser, True, wkind)
            )
        for name in sorted(avd_nombres):
            if name in running_avd:
                continue
            token = f"avd:{name}"
            filas_actuales.append(
                (etiqueta_fila_dispositivo(token, None, "avd_off"), token, False, "avd_off")
            )
        for label, _, _, _ in filas_actuales:
            listbox.insert(tk.END, label)
        if filas_actuales:
            listbox.selection_set(0)
        n_phy = len(physical)
        n_emu = len(emus)
        n_wifi = len(wifi_ordenados)
        n_avd_off = len([n for n in avd_nombres if n not in running_avd])
        set_status(
            f"{n_phy} USB, {n_wifi} Wi‑Fi, {n_emu} emulador(es) en marcha, {n_avd_off} AVD apagado(s). "
            "Obteniendo nombres…"
        )

        def enrich_worker() -> None:
            adb_bin = resolve_adb()
            if not adb_bin or not filas_actuales:
                return
            snapshot = list(filas_actuales)
            updates: List[Tuple[int, str, bool, str, Optional[str]]] = []
            for i, (_, ser, req, kind) in enumerate(snapshot):
                if kind == "avd_off":
                    updates.append((i, ser, req, kind, None))
                    continue
                nom = obtener_nombre_dispositivo_adb(
                    adb_bin,
                    ser,
                    intentar_connect=(kind not in ("usb", "emu")),
                )
                updates.append((i, ser, req, kind, nom))

            def apply_ui() -> None:
                if len(filas_actuales) != len(snapshot):
                    return
                for i, ser, req, kind, nom in updates:
                    if i >= len(filas_actuales) or filas_actuales[i][1] != ser:
                        continue
                    lab = etiqueta_fila_dispositivo(ser, nom, kind)
                    filas_actuales[i] = (lab, ser, req, kind)
                    listbox.delete(i)
                    listbox.insert(i, lab)
                set_status(
                    f"{n_phy} USB, {n_wifi} Wi‑Fi, {n_emu} emulador(es), {n_avd_off} AVD listo(s) para arrancar. "
                    "Emulador apagado: «Conectar y duplicar» solo arranca el AVD (sin scrcpy)."
                )

            root.after(0, apply_ui)

        threading.Thread(target=enrich_worker, daemon=True).start()
        if err_adb:
            messagebox.showwarning(
                TITLE,
                f"No se pudo leer «adb devices» bien:\n{err_adb}\n"
                "La lista puede faltar entradas USB o «ya en adb».",
            )
        elif not filas_actuales:
            messagebox.showinfo(
                TITLE,
                "No hay dispositivos.\n"
                "· USB: cable, depuración USB y autorizar este equipo.\n"
                "· Wi‑Fi: en el móvil depuración inalámbrica o «adb tcpip 5555» con cable; "
                "PC y teléfono en la misma red.\n"
                "· Emuladores: ANDROID_HOME y carpeta emulator/; crea AVD desde Android Studio.",
            )

    def aplicar_tcpip_usb() -> None:
        adb_bin = resolve_adb()
        if not adb_bin:
            return
        sel = listbox.curselection()
        if not sel:
            messagebox.showwarning(
                TITLE,
                "Selecciona en la lista una línea [USB · cable] y pulsa de nuevo.",
            )
            return
        idx = sel[0]
        if idx >= len(filas_actuales):
            return
        _, serial, requiere_connect, kind = filas_actuales[idx]
        if kind in ("emu", "avd_off"):
            messagebox.showinfo(
                TITLE,
                "«Activar 5555 por USB» no aplica a emuladores. Usa una línea [USB · cable].",
            )
            return
        if requiere_connect:
            messagebox.showwarning(
                TITLE,
                "«Activar 5555 por USB» solo aplica con el teléfono conectado por cable. "
                "Elige una línea que diga [USB · cable].",
            )
            return
        set_status("Ejecutando adb tcpip 5555…")
        root.update_idletasks()

        def work() -> None:
            r = subprocess.run(
                [adb_bin, "-s", serial, "tcpip", "5555"],
                capture_output=True,
                text=True,
                timeout=45,
            )
            out = (r.stdout or "").strip()
            err_txt = (r.stderr or "").strip()
            texto = "\n".join(x for x in (out, err_txt) if x)

            def done() -> None:
                if r.returncode != 0:
                    set_status("adb tcpip falló.")
                    messagebox.showerror(
                        TITLE,
                        texto or "adb tcpip 5555 devolvió error.",
                    )
                    return
                set_status(
                    "Puerto 5555 listo por Wi‑Fi. Pulsa «Buscar dispositivos» o desenchufa el USB."
                )
                msg = "Listo. Salida de adb:\n" + (texto or "(sin mensaje)")
                msg += (
                    "\n\nVuelve a pulsar «Buscar dispositivos» para ver la IP en la lista. "
                    "PC y móvil deben estar en la misma Wi‑Fi."
                )
                messagebox.showinfo(TITLE, msg)

            root.after(0, done)

        threading.Thread(target=work, daemon=True).start()

    def _root_viva() -> bool:
        try:
            return bool(root.winfo_exists())
        except tk.TclError:
            return False

    def _iniciar_espejo_tras_conexion(serial_espejo: str) -> None:
        if apagar_pantalla_var.get() and compat_miu_var.get():
            set_status(
                "Compat. MIUI activa: no se usará -S (evita negro/sin toque en Xiaomi). Iniciando espejo…"
            )
        else:
            set_status("Iniciando espejo…")
        root.update_idletasks()
        try:
            proc, err_log_path = popen_scrcpy(
                serial_espejo,
                apagar_pantalla_fisica=apagar_pantalla_var.get(),
                compat_miu=compat_miu_var.get(),
            )
        except OSError as e:
            ui_conexion_bloqueada(False)
            messagebox.showerror(TITLE, f"No se pudo ejecutar scrcpy:\n{e}")
            set_status("Error al ejecutar scrcpy.")
            return
        except FileNotFoundError:
            ui_conexion_bloqueada(False)
            messagebox.showerror(TITLE, "No se encontró el ejecutable scrcpy.")
            set_status("scrcpy no disponible.")
            return

        def esperar_arranque_scrcpy() -> None:
            err_path = err_log_path
            t0 = time.monotonic()
            max_espera = 5.0
            intervalo = 0.2
            code: Optional[int] = None
            while time.monotonic() - t0 < max_espera:
                code = proc.poll()
                if code is not None:
                    break
                time.sleep(intervalo)
            else:
                code = proc.poll()

            def en_interfaz() -> None:
                if not _root_viva():
                    try:
                        os.unlink(err_path)
                    except OSError:
                        pass
                    return
                if code is None:
                    try:
                        os.unlink(err_path)
                    except OSError:
                        pass
                    proceso_espejo[0] = proc
                    set_status(
                        "Espejo activo. Lista oculta; en el Dock solo «Duplicación Android»."
                    )
                    try:
                        root.update_idletasks()
                        root.withdraw()
                    except tk.TclError:
                        pass

                    def vigilar_cierre_espejo() -> None:
                        proc.wait()
                        proceso_espejo[0] = None

                        def al_terminar() -> None:
                            if not _root_viva():
                                return
                            ui_conexion_bloqueada(False)
                            try:
                                root.deiconify()
                            except tk.TclError:
                                pass
                            try:
                                root.lift()
                                root.focus_force()
                            except tk.TclError:
                                pass
                            set_status(
                                "El espejo terminó. Puedes buscar de nuevo o salir."
                            )

                        root.after(0, al_terminar)

                    threading.Thread(
                        target=vigilar_cierre_espejo, daemon=True
                    ).start()
                    return
                ui_conexion_bloqueada(False)
                detalle = _leer_y_borrar_log_scrcpy(err_path)
                msg = (
                    "scrcpy terminó antes de estabilizarse (código "
                    f"{code}). Suele ser autorización en el teléfono, adb o "
                    "librerías de scrcpy (Homebrew en /opt/homebrew/lib).\n\n"
                    "El .app ya usa adb y scrcpy-server embebidos en Resources/bundled "
                    "(variable ADB en el proceso de scrcpy)."
                )
                if detalle:
                    msg += "\n\nSalida de scrcpy:\n" + detalle
                messagebox.showerror(TITLE, msg)
                set_status("scrcpy no se mantuvo en ejecución. Revisa el mensaje anterior.")

            root.after(0, en_interfaz)

        threading.Thread(target=esperar_arranque_scrcpy, daemon=True).start()

    def do_connect() -> None:
        sel = listbox.curselection()
        if not sel:
            messagebox.showwarning(TITLE, "Selecciona un dispositivo de la lista.")
            return
        activo = proceso_espejo[0]
        if activo is not None and activo.poll() is None:
            messagebox.showinfo(
                TITLE,
                "Ya hay un espejo en curso. Cierra esa ventana primero o espera a que termine.",
            )
            return
        idx = sel[0]
        if idx >= len(filas_actuales):
            messagebox.showwarning(TITLE, "Vuelve a pulsar «Buscar dispositivos».")
            return
        _, serial, requiere_adb_connect, kind = filas_actuales[idx]

        if kind == "emu":
            messagebox.showinfo(
                TITLE,
                "Este emulador ya está en ejecución y tiene su ventana del Android Emulator.\n\n"
                "No se abrirá un duplicado con scrcpy.\n\n"
                "Para ver la pantalla duplicada aquí, elige un teléfono físico o por Wi‑Fi.",
            )
            set_status(
                f"Emulador {serial}: usa la ventana del Android Emulator (sin duplicación)."
            )
            return

        if kind == "avd_off":
            if not serial.startswith("avd:"):
                messagebox.showerror(TITLE, "Entrada de emulador inválida.")
                return
            avd = serial[4:]
            emu_bin = resolve_emulator_executable()
            if not emu_bin:
                messagebox.showerror(
                    TITLE,
                    "No se encontró «emulator».\n"
                    "Instala Android SDK, define ANDROID_HOME y comprueba que exista "
                    "$ANDROID_HOME/emulator/emulator.",
                )
                return
            ui_conexion_bloqueada(True)
            set_status(f"Iniciando emulador «{avd}» (puede tardar más de un minuto)…")
            root.update_idletasks()
            adb_bin = resolve_adb()
            assert adb_bin

            def boot_avd_work() -> None:
                antes = set(seriales_emulador_en_adb(adb_bin))
                try:
                    subprocess.Popen(
                        [emu_bin, "-avd", avd],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                except OSError as e:

                    def err_ui() -> None:
                        ui_conexion_bloqueada(False)
                        messagebox.showerror(TITLE, f"No se pudo lanzar el emulador:\n{e}")
                        set_status("Error al iniciar el emulador.")

                    root.after(0, err_ui)
                    return
                ser_nuevo = esperar_serial_emulador_nuevo(adb_bin, antes, 200.0)

                def done_ui() -> None:
                    if not _root_viva():
                        ui_conexion_bloqueada(False)
                        return
                    if not ser_nuevo:
                        ui_conexion_bloqueada(False)
                        messagebox.showerror(
                            TITLE,
                            "El emulador no apareció en «adb devices» a tiempo.\n"
                            "Comprueba el SDK, espacio en disco y que el AVD arranque manualmente.",
                        )
                        set_status("Timeout esperando al emulador.")
                        return
                    ui_conexion_bloqueada(False)
                    set_status(
                        f"Emulador listo ({ser_nuevo}). Usa su ventana «Android Emulator»; "
                        "no se abre duplicación con scrcpy."
                    )
                    messagebox.showinfo(
                        TITLE,
                        f"El AVD «{avd}» está arrancando o listo ({ser_nuevo}).\n\n"
                        "Usa la ventana del Android Emulator para interactuar.\n"
                        "Esta app no duplica emuladores con scrcpy para evitar dos pantallas iguales.\n\n"
                        "Pulsa «Buscar dispositivos» para refrescar la lista.",
                    )

                root.after(0, done_ui)

            threading.Thread(target=boot_avd_work, daemon=True).start()
            return

        ui_conexion_bloqueada(True)
        adb_bin = resolve_adb()
        assert adb_bin
        if requiere_adb_connect:
            set_status("Conectando por Wi‑Fi (adb connect)…")
            root.update_idletasks()
            r = subprocess.run(
                [adb_bin, "connect", serial],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if r.returncode != 0:
                ui_conexion_bloqueada(False)
                messagebox.showerror(TITLE, f"adb connect falló:\n{r.stderr or r.stdout}")
                set_status("Error en adb connect.")
                return
        else:
            if kind == "emu":
                set_status("Conectando al emulador en ejecución…")
            else:
                set_status("Conectando por USB (sin adb connect)…")
            root.update_idletasks()
        _iniciar_espejo_tras_conexion(serial)

    btn_inner = ttk.Frame(btn_row, style="Dup.TFrame")
    btn_inner.pack(side=tk.LEFT, fill=tk.X, expand=True)
    btn_scan = ttk.Button(
        btn_inner, text="Buscar dispositivos", command=do_scan, style="Dup.TButton"
    )
    btn_scan.pack(side=tk.LEFT, padx=(0, 8), pady=4)
    btn_connect = ttk.Button(
        btn_inner,
        text="Conectar y duplicar",
        command=do_connect,
        style="DupAccent.TButton",
    )
    btn_connect.pack(side=tk.LEFT, padx=(0, 8), pady=4)
    btn_tcpip_usb = ttk.Button(
        btn_inner,
        text="Activar 5555 por USB",
        command=aplicar_tcpip_usb,
        style="Dup.TButton",
    )
    btn_tcpip_usb.pack(side=tk.LEFT, padx=(0, 8), pady=4)

    def on_salir() -> None:
        p = proceso_espejo[0]
        if p is not None and p.poll() is None:
            if not messagebox.askokcancel(
                TITLE,
                "Hay un espejo activo. ¿Cerrar también el espejo y salir de la aplicación?",
            ):
                return
            try:
                p.terminate()
                p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                try:
                    p.kill()
                except Exception:
                    pass
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
            proceso_espejo[0] = None
        root.destroy()

    ttk.Button(btn_row, text="Salir", command=on_salir, style="Dup.TButton").pack(
        side=tk.RIGHT, padx=(12, 0), pady=4
    )
    root.protocol("WM_DELETE_WINDOW", on_salir)

    root.mainloop()
    return 0


def run_cli(args: argparse.Namespace) -> int:
    ips, err = _resolver_ips(args.cidr, 24)
    if err:
        print(err, file=sys.stderr)
        return 1
    assert ips is not None

    print("Buscando puerto 5555 abierto (puede tardar unos segundos)…")
    candidatos = escanear_puerto_5555(ips, args.workers, args.timeout)

    if not candidatos:
        print(
            "\nNo se encontró ningún host con 5555 abierto.\n"
            "En el móvil debe estar activo el ADB por red (USB: adb tcpip 5555, "
            "o depuración inalámbrica).",
            file=sys.stderr,
        )
        return 2

    print(f"\nEncontrados ({len(candidatos)}):")
    for i, ip in enumerate(candidatos, start=1):
        print(f"  {i}) {ip}:5555")

    while True:
        try:
            raw = input("\nElige número (Enter = 1, q = salir): ").strip()
        except EOFError:
            return 0
        if raw.lower() == "q":
            return 0
        if raw == "":
            idx = 1
            break
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(candidatos):
                break
        print("Opción no válida.")

    elegido = candidatos[idx - 1]
    serial = f"{elegido}:5555"

    adb_bin = resolve_adb()
    if not adb_bin:
        print("No se encontró adb.", file=sys.stderr)
        return 1
    r = ejecutar([adb_bin, "connect", serial])
    if r != 0:
        print("adb connect falló.", file=sys.stderr)
        return r

    if args.no_scrcpy:
        print("Listo (solo conexión). Ejecuta: scrcpy -s", serial, "(añade -S para apagar pantalla del móvil)")
        return 0

    sc = resolve_scrcpy()
    if not sc:
        print("No se encontró scrcpy.", file=sys.stderr)
        return 1
    cmd = [sc] + construir_argumentos_scrcpy(
        serial,
        getattr(args, "apagar_pantalla_movil", False),
        getattr(args, "xiaomi_compat", False),
    )
    print(f"→ {' '.join(cmd)}")
    return subprocess.call(cmd, env=scrcpy_subprocess_env())


def main() -> int:
    _ensure_path_for_bundled_app()

    if _frozen() and sys.platform == "darwin":
        _darwin_single_instance_or_exit()
        return run_gui(cidr=None, timeout_s=0.35, workers=64, prefijo=24)

    parser = argparse.ArgumentParser(
        description="Busca dispositivos con ADB TCP (5555), conecta y abre scrcpy."
    )
    parser.add_argument(
        "-n",
        "--cidr",
        help="Red a escanear, p. ej. 192.168.0.0/24 (por defecto: /24 de tu IP local)",
    )
    parser.add_argument(
        "-t",
        "--timeout",
        type=float,
        default=0.35,
        help="Timeout por conexión TCP (segundos, default: 0.35)",
    )
    parser.add_argument(
        "-j",
        "--workers",
        type=int,
        default=64,
        help="Hilos paralelos para el escaneo (default: 64)",
    )
    parser.add_argument(
        "--no-scrcpy",
        action="store_true",
        help="Solo adb connect, no lanzar scrcpy",
    )
    parser.add_argument(
        "--apagar-pantalla-movil",
        action="store_true",
        help="Pasar -S a scrcpy (apaga la pantalla física; con --xiaomi-compat se ignora)",
    )
    parser.add_argument(
        "--xiaomi-compat",
        action="store_true",
        help="UHID + Metal + sin -S: Xiaomi/Redmi/MIUI (pantalla negra / sin toque)",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Ventana gráfica (macOS)",
    )
    args = parser.parse_args()

    if sys.platform == "darwin" and args.gui:
        return run_gui(
            cidr=args.cidr,
            timeout_s=args.timeout,
            workers=args.workers,
            prefijo=24,
        )

    return run_cli(args)


if __name__ == "__main__":
    sys.exit(main())
