import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

import customtkinter


PROJECT_DIR = Path(__file__).resolve().parent
DIST_DIR = PROJECT_DIR / "dist"
BUILD_DIR = PROJECT_DIR / "build"
APP_NAME = "modvera"
SERVICE_NAME = "modvera_service"
MATPLOTLIB_BACKEND = "TkAgg"
RUNTIME_TMPDIR = PROJECT_DIR / "_pyi_runtime"
QT_EXCLUDED_MODULES = [
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "matplotlib.backends.backend_qt",
    "matplotlib.backends.backend_qt5",
    "matplotlib.backends.backend_qt5agg",
    "matplotlib.backends.backend_qt5cairo",
    "matplotlib.backends.backend_qtagg",
    "matplotlib.backends.backend_qtcairo",
    "matplotlib.backends.qt_compat",
]
GUI_BUILD_XREF = BUILD_DIR / APP_NAME / "xref-modvera.html"
FORBIDDEN_XREF_MARKERS = [
    "pyi_rth_pyqt5.py",
    "pyi_rth_pyqt6.py",
    "pyi_rth_pyside2.py",
    "pyi_rth_pyside6.py",
    "backend_qtagg",
    "backend_qt5agg",
    ">PyQt5<",
    ">PyQt6<",
    ">PySide2<",
    ">PySide6<",
]


def build_environment():
    env = os.environ.copy()
    env["MPLBACKEND"] = MATPLOTLIB_BACKEND
    return env


def run_command(command, extra_env=None):
    print("\n[BUILD]", " ".join(str(part) for part in command), flush=True)
    env = build_environment()
    if extra_env:
        env.update(extra_env)
    subprocess.run(command, cwd=PROJECT_DIR, check=True, env=env)


def clean_build_outputs(stage_dir=None):
    for path in [BUILD_DIR, DIST_DIR]:
        if path.exists():
            shutil.rmtree(path)
    if stage_dir and stage_dir.exists():
        shutil.rmtree(stage_dir)


def build_gui():
    ctk_path = Path(customtkinter.__file__).resolve().parent
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        f"--name={APP_NAME}",
        f"--runtime-tmpdir={RUNTIME_TMPDIR}",
        "--icon=icon.png",
        f"--add-data={ctk_path};customtkinter/",
        f"--add-data={PROJECT_DIR / 'icon.png'};.",
        "--hidden-import=pystray._win32",
	"--collect-all=database",
        "main.py",
    ]
    for module_name in QT_EXCLUDED_MODULES:
        command.insert(-1, f"--exclude-module={module_name}")
    run_command(command)
    verify_gui_build()


def build_service():
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--console",
        f"--name={SERVICE_NAME}",
        f"--runtime-tmpdir={RUNTIME_TMPDIR}",
        "--hidden-import=bcrypt",
        "--hidden-import=bcrypt._bcrypt",
        "logger_service.py",
    ]
    run_command(command)


def verify_gui_build():
    if not GUI_BUILD_XREF.exists():
        raise FileNotFoundError(f"GUI build dogrulama dosyasi bulunamadi: {GUI_BUILD_XREF}")

    content = GUI_BUILD_XREF.read_text(encoding="utf-8", errors="ignore")
    violations = []
    for marker in FORBIDDEN_XREF_MARKERS:
        if marker in content:
            violations.append(marker)
    if violations:
        details = ", ".join(violations)
        raise RuntimeError(f"GUI build beklenmeyen Qt bagimliliklari iceriyor: {details}")


def ensure_output_exists(path):
    if not path.exists():
        raise FileNotFoundError(f"Beklenen build ciktisi bulunamadi: {path}")


def copy_outputs_to_root():
    app_src = DIST_DIR / f"{APP_NAME}.exe"
    service_src = DIST_DIR / f"{SERVICE_NAME}.exe"
    ensure_output_exists(app_src)
    ensure_output_exists(service_src)

    shutil.copy2(app_src, PROJECT_DIR / app_src.name)
    shutil.copy2(service_src, PROJECT_DIR / service_src.name)

    return [PROJECT_DIR / app_src.name, PROJECT_DIR / service_src.name]


def stage_outputs(stage_dir):
    stage_dir.mkdir(parents=True, exist_ok=True)

    staged = []
    for filename in [
        f"{APP_NAME}.exe",
        f"{SERVICE_NAME}.exe",
        "icon.png",
        "RELEASE_CHECKLIST.md",
        "TEST_VE_OPERASYON_CHECKLIST.md",
    ]:
        src = PROJECT_DIR / filename
        if src.exists():
            dst = stage_dir / filename
            shutil.copy2(src, dst)
            staged.append(dst)

    return staged


def parse_args():
    parser = argparse.ArgumentParser(description="Build Modvera executables with PyInstaller.")
    parser.add_argument(
        "--stage-dir",
        default="release_bundle",
        help="Folder to collect release-ready artifacts. Use empty string to skip staging.",
    )
    parser.add_argument(
        "--clean-only",
        action="store_true",
        help="Only remove build output folders and exit.",
    )
    parser.add_argument(
        "--no-root-copy",
        action="store_true",
        help="Keep EXE outputs only under dist/stage without copying them to project root.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    stage_dir = PROJECT_DIR / args.stage_dir if args.stage_dir else None

    if args.clean_only:
        clean_build_outputs(stage_dir)
        print("[OK] Build output klasorleri temizlendi.", flush=True)
        return

    clean_build_outputs(stage_dir)
    build_gui()
    build_service()
    copied = []
    if not args.no_root_copy:
        copied = copy_outputs_to_root()

    staged = []
    if stage_dir is not None:
        if args.no_root_copy:
            stage_dir.mkdir(parents=True, exist_ok=True)
            for src in [DIST_DIR / f"{APP_NAME}.exe", DIST_DIR / f"{SERVICE_NAME}.exe", PROJECT_DIR / "icon.png"]:
                ensure_output_exists(src)
                dst = stage_dir / src.name
                shutil.copy2(src, dst)
                staged.append(dst)
            for doc_name in ["RELEASE_CHECKLIST.md", "TEST_VE_OPERASYON_CHECKLIST.md"]:
                src = PROJECT_DIR / doc_name
                if src.exists():
                    dst = stage_dir / doc_name
                    shutil.copy2(src, dst)
                    staged.append(dst)
        else:
            staged = stage_outputs(stage_dir)

    print("\n[OK] Build tamamlandi.", flush=True)
    for path in copied:
        print(f"- Root output: {path}", flush=True)
    for path in staged:
        print(f"- Staged output: {path}", flush=True)


if __name__ == "__main__":
    main()
