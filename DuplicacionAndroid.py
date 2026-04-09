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
from typing import List, Optional, Sequence

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
    """Ruta al .app hijo (LSUIElement); preferir lanzar con /usr/bin/open -a para respetar el bundle."""
    contents = main_bundle_contents_dir()
    if contents is None:
        return None
    p = contents / "Frameworks" / "DuplicacionAndroidEspejo.app"
    if p.is_dir():
        return p.resolve()
    return None


def resolve_adb() -> Optional[str]:
    p = bundled_dir() / "adb"
    if p.is_file():
        return str(p.resolve())
    _ensure_path_for_bundled_app()
    return shutil.which("adb")


def resolve_scrcpy() -> Optional[str]:
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
    nested_app = nested_mirror_app_bundle_path()
    if _frozen() and sys.platform == "darwin" and nested_app is not None:
        # open -a aplica Info.plist del bundle (LSUIElement); ejecutar el Mach-O directo suele
        # hacer que SDL/scrcpy aparezca como otra app en el Dock.
        cmd = [
            "/usr/bin/open",
            "-n",
            "-W",
            "-a",
            str(nested_app),
            "--args",
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
            start_new_session=not (_frozen() and sys.platform == "darwin"),
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
    root.geometry("460x360")
    root.minsize(400, 300)

    icon_png = app_resources_dir() / "DuplicacionAndroidIcon.png"
    if icon_png.is_file():
        try:
            img = tk.PhotoImage(file=str(icon_png))
            root.iconphoto(True, img)
            root._duplicacion_icon = img
        except tk.TclError:
            pass

    cidr_var = tk.StringVar(value=cidr or "")
    status = tk.StringVar(value="Pulsa «Buscar dispositivos».")
    list_frame = ttk.Frame(root, padding=8)
    list_frame.pack(fill=tk.BOTH, expand=True)
    ttk.Label(list_frame, text="Red (opcional, vacío = /24 de tu IP):").pack(anchor=tk.W)
    entry_cidr = ttk.Entry(list_frame, textvariable=cidr_var)
    entry_cidr.pack(fill=tk.X, pady=(0, 6))
    ttk.Label(list_frame, textvariable=status).pack(anchor=tk.W)
    lb_frame = ttk.Frame(list_frame)
    lb_frame.pack(fill=tk.BOTH, expand=True, pady=6)
    sb = ttk.Scrollbar(lb_frame, orient=tk.VERTICAL)
    listbox = tk.Listbox(lb_frame, height=10, exportselection=False, yscrollcommand=sb.set)
    sb.config(command=listbox.yview)
    listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    sb.pack(side=tk.RIGHT, fill=tk.Y)
    btn_row = ttk.Frame(list_frame)
    btn_row.pack(fill=tk.X)

    candidatos_actuales: List[str] = []
    proceso_espejo: List[Optional[subprocess.Popen]] = [None]

    def ui_conexion_bloqueada(bloquear: bool) -> None:
        st = tk.DISABLED if bloquear else tk.NORMAL
        btn_scan.state(["disabled"] if bloquear else ["!disabled"])
        btn_connect.state(["disabled"] if bloquear else ["!disabled"])
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
        set_status("Escaneando…")
        listbox.delete(0, tk.END)
        root.update_idletasks()

        def work() -> None:
            ips, err = _resolver_ips(raw or None, prefijo)
            if err:
                root.after(0, lambda: on_scan_done([], err))
                return
            assert ips is not None
            found = escanear_puerto_5555(ips, workers, timeout_s)
            root.after(0, lambda: on_scan_done(found, None))

        threading.Thread(target=work, daemon=True).start()

    def on_scan_done(found: List[str], err: Optional[str]) -> None:
        nonlocal candidatos_actuales
        candidatos_actuales = found
        listbox.delete(0, tk.END)
        if err:
            set_status(err)
            messagebox.showerror(TITLE, err)
            return
        for ip in found:
            listbox.insert(tk.END, f"{ip}:5555")
        if found:
            listbox.selection_set(0)
            set_status(f"{len(found)} dispositivo(s) con puerto 5555.")
        else:
            set_status(
                "Ningún host con 5555 abierto. Activa ADB por red en el móvil (p. ej. adb tcpip 5555)."
            )

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
        line = listbox.get(sel[0])
        serial = line.strip()
        if ":5555" not in serial:
            serial = f"{serial}:5555"
        ui_conexion_bloqueada(True)
        set_status("Conectando (ADB)…")
        root.update_idletasks()
        adb_bin = resolve_adb()
        assert adb_bin
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
                    f"{code}). El equipo sí está en la red; el fallo suele ser "
                    "ADB interno, autorización del teléfono o librerías de "
                    "scrcpy (Homebrew).\n\n"
                    "Ya se fuerza el adb embebido del .app (variable ADB)."
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
