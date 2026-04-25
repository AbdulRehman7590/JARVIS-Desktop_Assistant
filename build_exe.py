"""Build a one-file ``JARVIS.exe`` using PyInstaller.

Usage:

    python build_exe.py            # build (windowed)
    python build_exe.py --console  # build with a console window (debug)
    python build_exe.py --clean    # wipe build/ and dist/ first

What gets bundled:
    * **PySide6 (QtCore + QtGui + QtWidgets only)** — the modern
      dashboard. Every other Qt module (Qt3D, QtBluetooth, QtWebEngine,
      QtMultimedia, QtCharts, QtQml/Quick, QtNetwork, QtSql, …) is
      explicitly excluded to keep the binary in the ~100-200 MB range
      instead of 500+ MB.
    * **google-genai** — Google Gemini SDK. Optional at runtime, but we
      bundle it so users can paste a Gemini key and have it Just Work.
    * **pyttsx3 + SAPI5** — TTS. Hidden imports needed for the COM driver.
    * **speech_recognition** + **sounddevice** + (optionally) **PyAudio**
      — STT. We collect both so the picker can fall back at runtime.

Whisper is *huge* and pulls in PyTorch, so the build can be slow and the
resulting exe can be hundreds of MB. If you don't need offline STT,
comment out ``openai-whisper`` and ``torch`` in ``requirements.txt``
before building to get a much smaller binary.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


# Qt modules JARVIS actually imports — see `rg "from PySide6"`.
_QT_MODULES_USED = ("QtCore", "QtGui", "QtWidgets")

# Qt modules that PyInstaller's PySide6 hooks would otherwise pull in
# transitively. None of these are imported by JARVIS, and skipping them
# saves several hundred MB in the final exe.
_QT_MODULES_EXCLUDED = (
    "Qt3DAnimation", "Qt3DCore", "Qt3DExtras", "Qt3DInput",
    "Qt3DLogic", "Qt3DRender",
    "QtAxContainer", "QtBluetooth", "QtCanvasPainter", "QtCharts",
    "QtConcurrent", "QtDataVisualization", "QtDBus", "QtDesigner",
    "QtHelp", "QtHttpServer", "QtMultimedia", "QtMultimediaWidgets",
    "QtNetwork", "QtNetworkAuth", "QtNfc", "QtOpenGL",
    "QtOpenGLWidgets", "QtPdf", "QtPdfWidgets", "QtPositioning",
    "QtPrintSupport", "QtQml", "QtQmlCore", "QtQuick", "QtQuick3D",
    "QtQuickControls2", "QtQuickTest", "QtQuickWidgets",
    "QtRemoteObjects", "QtScxml", "QtSensors", "QtSerialBus",
    "QtSerialPort", "QtSpatialAudio", "QtSql", "QtStateMachine",
    "QtSvg", "QtSvgWidgets", "QtTest", "QtTextToSpeech", "QtUiTools",
    "QtWebChannel", "QtWebEngine", "QtWebEngineCore",
    "QtWebEngineQuick", "QtWebEngineWidgets", "QtWebSockets",
    "QtWebView", "QtXml",
)


def _existing(*paths: Path) -> list[Path]:
    return [p for p in paths if p.exists()]


def _hidden_imports() -> list[str]:
    """Modules PyInstaller can't always discover automatically."""
    base = [
        # TTS — pyttsx3 imports its drivers via importlib at runtime.
        "pyttsx3.drivers",
        "pyttsx3.drivers.sapi5",
        # STT — speech_recognition probes for backends at import time.
        "speech_recognition",
        # System info / scheduler.
        "psutil",
        # Optional STT capture backends. Whichever has a wheel installed
        # will be picked up at runtime; missing ones produce a benign
        # PyInstaller warning.
        "sounddevice",
        "pyaudio",
        # Optional Gemini SDK. Safe to keep even when the user only
        # configures an OpenAI key — the dispatcher only imports it if
        # LLM_PROVIDER=gemini.
        "google.genai",
        "google.genai.types",
    ]
    base.extend(f"PySide6.{m}" for m in _QT_MODULES_USED)
    return base


def _excluded_modules() -> list[str]:
    """Modules to keep OUT of the final exe (drastically smaller binary)."""
    return [f"PySide6.{m}" for m in _QT_MODULES_EXCLUDED] + [
        # PyInstaller often picks these up transitively via PyTorch / Whisper
        # but we don't need them in the assistant.
        "matplotlib", "tkinter.test", "test", "unittest", "pydoc_data",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build JARVIS.exe with PyInstaller."
    )
    parser.add_argument("--console", action="store_true",
                        help="Show a console window (useful for debugging).")
    parser.add_argument("--clean", action="store_true",
                        help="Delete build/ and dist/ before building.")
    parser.add_argument("--name", default="JARVIS",
                        help="Output executable name (default: JARVIS).")
    args = parser.parse_args()

    if args.clean:
        for d in (ROOT / "build", ROOT / "dist"):
            if d.exists():
                print(f"[clean] removing {d}")
                shutil.rmtree(d, ignore_errors=True)
        for spec in ROOT.glob("*.spec"):
            spec.unlink(missing_ok=True)

    cmd: list[str] = [
        sys.executable, "-m", "PyInstaller",
        "--name", args.name,
        "--onefile",
        "--noconfirm",
        "--clean",
        # Targeted submodule collection — DON'T sweep every PySide6
        # module, just the speech / google packages. Qt modules are
        # opted-in by name via --hidden-import below.
        "--collect-submodules", "speech_recognition",
        "--collect-submodules", "pyttsx3",
        "--collect-submodules", "google.genai",
    ]

    if not args.console:
        cmd.append("--noconsole")

    for hi in _hidden_imports():
        cmd.extend(["--hidden-import", hi])
    for ex in _excluded_modules():
        cmd.extend(["--exclude-module", ex])

    # Bundle docs alongside the exe (optional).
    sep = ";" if sys.platform.startswith("win") else ":"
    for extra in _existing(ROOT / "README.md", ROOT / ".env.example"):
        cmd.extend(["--add-data", f"{extra}{sep}."])

    cmd.append(str(ROOT / "main.py"))

    print("[build]", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=str(ROOT))
    if proc.returncode != 0:
        print("[build] FAILED")
        return proc.returncode

    out = ROOT / "dist" / f"{args.name}.exe"
    if out.exists():
        size_mb = out.stat().st_size / (1024 * 1024)
        print(f"[build] DONE -> {out} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
