#!/usr/bin/env python3
"""
Duplicación Android: busca hosts con ADB TCP (5555), conecta y lanza scrcpy (-S apaga la pantalla).

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


def popen_scrcpy(serial: str) -> tuple[subprocess.Popen, str]:
    """Devuelve (proceso, ruta de log stderr temporal para diagnóstico)."""
    # Importante: NO usar solo «open -W -a …»: el PID es «open», que a menudo termina con
    # código 0 al instante aunque scrcpy siga vivo → falso «scrcpy terminó antes de estabilizarse».
    # Ejecutar el Mach-O del bundle espejo + scrcpy_subprocess_env() (ADB y server embebidos).
    nested_scrcpy = nested_mirror_scrcpy_binary_path()
    if _frozen() and sys.platform == "darwin" and nested_scrcpy is not None:
        cmd = [
            str(nested_scrcpy),
            "-S",
            "-s",
            serial,
            f"--window-title={TITLE}",
        ]
    else:
        bin_scrcpy = resolve_scrcpy()
        if not bin_scrcpy:
            raise FileNotFoundError("scrcpy")
        cmd = [
            bin_scrcpy,
            "-S",
            "-s",
            serial,
            f"--window-title={TITLE}",
        ]
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
    root.geometry("520x420")
    root.minsize(420, 340)

    icon_png = app_resources_dir() / "DuplicacionAndroidIcon.png"
    if icon_png.is_file():
        try:
            img = tk.PhotoImage(file=str(icon_png))
            root.iconphoto(True, img)
            root._duplicacion_icon = img
        except tk.TclError:
            pass

    main = ttk.Frame(root, padding=8)
    main.pack(fill=tk.BOTH, expand=True)

    cidr_var = tk.StringVar(value=cidr or "")
    status = tk.StringVar(
        value="Pulsa «Buscar dispositivos» para ver USB, red y dispositivos ya conectados por ADB."
    )
    ttk.Label(main, text="Red (opcional, vacío = /24 de tu IP):").pack(anchor=tk.W)
    entry_cidr = ttk.Entry(main, textvariable=cidr_var)
    entry_cidr.pack(fill=tk.X, pady=(0, 4))
    ttk.Label(
        main,
        text=(
            "Lista unificada: [USB] cable; [Wi‑Fi] red (escaneo o ya visto por adb). "
            "Elige uno y «Conectar y duplicar». «Activar 5555 por USB» solo con cable."
        ),
        wraplength=500,
        justify=tk.LEFT,
    ).pack(anchor=tk.W, pady=(0, 4))
    ttk.Label(main, textvariable=status).pack(anchor=tk.W)
    lb_frame = ttk.Frame(main)
    lb_frame.pack(fill=tk.BOTH, expand=True, pady=6)
    sb = ttk.Scrollbar(lb_frame, orient=tk.VERTICAL)
    listbox = tk.Listbox(lb_frame, height=11, exportselection=False, yscrollcommand=sb.set)
    sb.config(command=listbox.yview)
    listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    sb.pack(side=tk.RIGHT, fill=tk.Y)
    btn_row = ttk.Frame(main)
    btn_row.pack(fill=tk.X)

    # (texto en lista, serial adb, requiere «adb connect» antes de scrcpy)
    filas_actuales: List[Tuple[str, str, bool]] = []
    proceso_espejo: List[Optional[subprocess.Popen]] = [None]

    def ui_conexion_bloqueada(bloquear: bool) -> None:
        st = tk.DISABLED if bloquear else tk.NORMAL
        btn_scan.state(["disabled"] if bloquear else ["!disabled"])
        btn_connect.state(["disabled"] if bloquear else ["!disabled"])
        btn_tcpip_usb.state(["disabled"] if bloquear else ["!disabled"])
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
                root.after(0, lambda: on_scan_merge([], [], set(), err_red, None))
                return
            assert ips is not None
            encontrados_tcp = escanear_puerto_5555(ips, workers, timeout_s)
            tcp_serials = {f"{ip}:5555" for ip in encontrados_tcp}
            wifi_serials = set(tcp_serials) | set(reds_en_adb)
            root.after(
                0,
                lambda: on_scan_merge(
                    sorted(usbs),
                    sorted(wifi_serials, key=_orden_serial_red),
                    tcp_serials,
                    None,
                    err_adb,
                ),
            )

        threading.Thread(target=work, daemon=True).start()

    def on_scan_merge(
        usbs: List[str],
        wifi_ordenados: List[str],
        tcp_serials: set[str],
        err_red: Optional[str],
        err_adb: Optional[str],
    ) -> None:
        nonlocal filas_actuales
        filas_actuales = []
        listbox.delete(0, tk.END)
        if err_red:
            set_status(err_red)
            messagebox.showerror(TITLE, err_red)
            return
        for s in usbs:
            filas_actuales.append((f"{s}  [USB · cable]", s, False))
        for ser in wifi_ordenados:
            if ser in tcp_serials:
                suf = "[Wi‑Fi · escaneo de red]"
            else:
                suf = "[Wi‑Fi · ya en adb, sin escaneo]"
            filas_actuales.append((f"{ser}  {suf}", ser, True))
        for label, _, _ in filas_actuales:
            listbox.insert(tk.END, label)
        if filas_actuales:
            listbox.selection_set(0)
        n_usb = len(usbs)
        n_wifi = len(wifi_ordenados)
        set_status(
            f"{n_usb} por USB, {n_wifi} por Wi‑Fi en lista. "
            "Si el móvil no sale: misma Wi‑Fi, sin aislamiento AP, o prueba «Activar 5555 por USB»."
        )
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
                "PC y teléfono en la misma red.",
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
        _, serial, requiere_connect = filas_actuales[idx]
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
        _, serial, requiere_adb_connect = filas_actuales[idx]
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
            set_status("Conectando por USB (sin adb connect)…")
            root.update_idletasks()
        set_status("Iniciando espejo…")
        root.update_idletasks()
        try:
            proc, err_log_path = popen_scrcpy(serial)
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

    btn_scan = ttk.Button(btn_row, text="Buscar dispositivos", command=do_scan)
    btn_scan.pack(side=tk.LEFT, padx=(0, 6))
    btn_connect = ttk.Button(btn_row, text="Conectar y duplicar", command=do_connect)
    btn_connect.pack(side=tk.LEFT, padx=(0, 6))
    btn_tcpip_usb = ttk.Button(
        btn_row,
        text="Activar 5555 por USB",
        command=aplicar_tcpip_usb,
    )
    btn_tcpip_usb.pack(side=tk.LEFT, padx=(0, 6))

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

    ttk.Button(btn_row, text="Salir", command=on_salir).pack(side=tk.RIGHT)
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
        print("Listo (solo conexión). Ejecuta: scrcpy -S -s", serial)
        return 0

    sc = resolve_scrcpy()
    if not sc:
        print("No se encontró scrcpy.", file=sys.stderr)
        return 1
    cmd = [sc, "-S", "-s", serial, f"--window-title={TITLE}"]
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
