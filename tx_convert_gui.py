#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Arnold TX Converter - PySide6 GUI
- Strictly Arnold-focused maketx wrapper
- OCIO: choose .ocio file in GUI, else fallback to $OCIO
- maketx.exe: choose once, path is remembered in ~/.arnold_tx_converter.json
- Concurrency: uses up to (CPU cores - 1) concurrent maketx processes
- Output: .tx written next to source textures
- Skips: skip if .tx exists and is newer than source
- Logging: in-UI log + optional log file
"""

import os
import sys
import json
import shutil
import argparse
import datetime
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

CONFIG_FILE = Path.home() / ".arnold_tx_converter.json"

VALID_EXTS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".exr", ".dds", ".tga", ".bmp", ".psd")
COLOR_TAGS = ("srgb", "basecolor", "albedo", "color", "diffuse")
DSP_TAGS   = ("dsp", "disp", "displacement", "zdisp", "height")


# ---------------------------
# Config helpers
# ---------------------------

def load_config():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_config(cfg: dict):
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception as e:
        print("Failed to save config:", e)


# ---------------------------
# maketx helpers
# ---------------------------

def is_displacement(fname: Path) -> bool:
    low = fname.name.lower()
    return any(tag in low for tag in DSP_TAGS)

def is_color(fname: Path) -> bool:
    low = fname.name.lower()
    if is_displacement(fname):
        return False
    return any(tag in low for tag in COLOR_TAGS)

def needs_conversion(src: Path, dst: Path) -> bool:
    if not dst.exists():
        return True
    return src.stat().st_mtime > dst.stat().st_mtime

def build_maketx_cmd(src: Path, ocio_path: str | None, verbose: bool,
                     is_col: bool, is_dsp: bool, maketx_path: str) -> list[str]:
    cmd = [maketx_path, str(src)]

    if ocio_path:
        cmd += ["--colorconfig", ocio_path]

    cmd += [
        "--opaque-detect", "--constant-color-detect", "--monochrome-detect",
        "--fixnan", "box3",
        "-u",
        "--filter", "lanczos3",
        "--attrib", "tiff:half", "1",
        "--unpremult",
        "--oiio",
    ]

    cmd += ["--colorconvert"]
    if is_col:
        cmd += ["Utility - sRGB - Texture", "ACES - ACEScg"]
    else:
        cmd += ["Utility - Raw", "ACES - ACEScg"]

    if is_dsp:
        cmd += ["-d", "float"]
    else:
        cmd += ["-d", "half"]

    if verbose:
        cmd += ["-v"]

    cmd += ["--threads", "1"]
    return cmd

def convert_one(src: Path, ocio_path: str | None, verbose: bool, maketx_path: str) -> tuple[Path, bool, str]:
    try:
        dst = src.with_suffix(src.suffix + ".tx") if src.suffix.lower() != ".tx" else src
        if src.suffix.lower() == ".tx":
            return (src, False, "Already a .tx; skipping.")

        if not needs_conversion(src, dst):
            return (src, False, f"Up-to-date .tx exists: {dst.name}; skipping.")

        col = is_color(src)
        dsp = is_displacement(src)
        cmd = build_maketx_cmd(src, ocio_path, verbose, col, dsp, maketx_path)

        proc = subprocess.run(
            cmd,
            shell=False,
            stdout=subprocess.PIPE if verbose else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False
        )

        if proc.returncode == 0:
            out_msg = ""
            if verbose and proc.stdout:
                out_msg += proc.stdout.strip()
            return (src, True, out_msg or "OK")
        else:
            err = (proc.stderr or "").strip()
            return (src, False, f"maketx failed: {err if err else 'Unknown error'}")

    except Exception as e:
        return (src, False, f"Exception: {e}")


# ---------------------------
# Worker
# ---------------------------

class ConvertWorker(QtCore.QObject):
    progress = QtCore.Signal(int, int)
    item_done = QtCore.Signal(str)
    finished = QtCore.Signal(int, int)
    fatal = QtCore.Signal(str)

    def __init__(self, root_dir: Path, filter_str: str, recursive: bool,
                 ocio_file: str | None, verbose: bool, maketx_path: str, parent=None):
        super().__init__(parent)
        self.root_dir = root_dir
        self.filter_str = filter_str.strip() if filter_str else ""
        self.recursive = recursive
        self.ocio_file = ocio_file.strip() if ocio_file else None
        self.verbose = verbose
        self.maketx_path = maketx_path.strip()
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        if not self.maketx_path or not Path(self.maketx_path).exists():
            self.fatal.emit("maketx.exe not set or not found. Please select the path in the GUI.")
            return

        ocio_to_use = None
        if self.ocio_file:
            if not Path(self.ocio_file).is_file():
                self.fatal.emit(f"OCIO file not found: {self.ocio_file}")
                return
            ocio_to_use = self.ocio_file
        else:
            env_ocio = os.environ.get("OCIO", "").strip()
            if not env_ocio:
                self.fatal.emit("No OCIO specified and $OCIO is not set.")
                return
            if not Path(env_ocio).exists():
                self.fatal.emit(f"$OCIO points to non-existent file: {env_ocio}")
                return
            ocio_to_use = env_ocio

        files = []
        try:
            if self.recursive:
                for p in self.root_dir.rglob("*"):
                    if p.is_file() and p.suffix.lower() in VALID_EXTS:
                        if self.filter_str and self.filter_str not in p.name:
                            continue
                        files.append(p)
            else:
                for p in self.root_dir.glob("*"):
                    if p.is_file() and p.suffix.lower() in VALID_EXTS:
                        if self.filter_str and self.filter_str not in p.name:
                            continue
                        files.append(p)
        except Exception as e:
            self.fatal.emit(f"Failed to list files: {e}")
            return

        total = len(files)
        if total == 0:
            self.fatal.emit("No valid textures in folder (check extensions or filter).")
            return

        cpu = max(1, (os.cpu_count() or 1) - 1)
        max_workers = max(1, cpu)

        self.item_done.emit(f"Found {total} texture(s). Using {max_workers} worker(s).")
        done = 0
        ok = 0
        fail = 0

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(convert_one, p, ocio_to_use, self.verbose, self.maketx_path): p for p in files}
            for fut in as_completed(futures):
                if self._cancelled:
                    self.item_done.emit("Cancellation requested. Stopping...")
                    break
                src, success, message = fut.result()
                done += 1
                if success:
                    ok += 1
                    self.item_done.emit(f"✓ {src.name}: {message}")
                else:
                    if "skip" in message.lower():
                        self.item_done.emit(f"• {src.name}: {message}")
                    else:
                        fail += 1
                        self.item_done.emit(f"✗ {src.name}: {message}")
                self.progress.emit(done, total)

        self.finished.emit(ok, fail)


# ---------------------------
# Main GUI
# ---------------------------

class TxConverterUI(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.worker_thread = None
        self.worker = None
        self._init_ui()
        self._apply_style()
        self._load_saved_config()

    def _init_ui(self):
        self.setWindowTitle("Arnold TX Converter")
        self.setMinimumSize(820, 600)

        self.path_edit = QtWidgets.QLineEdit()
        self.path_btn  = QtWidgets.QPushButton("Browse…")

        self.filter_edit = QtWidgets.QLineEdit()
        self.recursive_chk = QtWidgets.QCheckBox("Recursive")
        self.verbose_chk = QtWidgets.QCheckBox("Verbose")

        self.ocio_edit = QtWidgets.QLineEdit()
        self.ocio_btn  = QtWidgets.QPushButton("Browse…")

        self.maketx_edit = QtWidgets.QLineEdit()
        self.maketx_btn  = QtWidgets.QPushButton("Browse…")

        self.start_btn = QtWidgets.QPushButton("Start")
        self.cancel_btn = QtWidgets.QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 100)

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.save_log_btn = QtWidgets.QPushButton("Save Log…")

        form = QtWidgets.QFormLayout()
        row1 = QtWidgets.QHBoxLayout(); row1.addWidget(self.path_edit, 1); row1.addWidget(self.path_btn)
        form.addRow("Folder:", row1)
        row2 = QtWidgets.QHBoxLayout(); row2.addWidget(self.filter_edit, 1)
        form.addRow("Filter:", row2)
        row3 = QtWidgets.QHBoxLayout(); row3.addWidget(self.recursive_chk); row3.addWidget(self.verbose_chk); row3.addStretch(1)
        form.addRow("", row3)
        row4 = QtWidgets.QHBoxLayout(); row4.addWidget(self.ocio_edit, 1); row4.addWidget(self.ocio_btn)
        form.addRow("OCIO:", row4)
        row5 = QtWidgets.QHBoxLayout(); row5.addWidget(self.maketx_edit, 1); row5.addWidget(self.maketx_btn)
        form.addRow("maketx.exe:", row5)

        actions = QtWidgets.QHBoxLayout()
        actions.addWidget(self.start_btn)
        actions.addWidget(self.cancel_btn)
        actions.addStretch(1)
        actions.addWidget(self.save_log_btn)

        v = QtWidgets.QVBoxLayout(self)
        v.addLayout(form)
        v.addWidget(self.progress)
        v.addWidget(self.log, 1)
        v.addLayout(actions)

        self.path_btn.clicked.connect(self.on_browse_folder)
        self.ocio_btn.clicked.connect(self.on_browse_ocio)
        self.maketx_btn.clicked.connect(self.on_browse_maketx)
        self.start_btn.clicked.connect(self.on_start)
        self.cancel_btn.clicked.connect(self.on_cancel)
        self.save_log_btn.clicked.connect(self.on_save_log)

        self.recursive_chk.setChecked(True)

    def _apply_style(self):
        self.setStyleSheet("""
        QLineEdit, QPlainTextEdit {
            border: 1px solid rgba(120,120,120,0.4);
            border-radius: 8px;
            padding: 6px;
        }
        QProgressBar {
            height: 16px;
            border: 1px solid rgba(120,120,120,0.4);
            border-radius: 8px;
            text-align: center;
        }
        QPushButton {
            border: 1px solid rgba(120,120,120,0.4);
            border-radius: 10px;
            padding: 6px 12px;
        }
        QPushButton:hover { background: rgba(100,100,100,0.06); }
        """)

    def _load_saved_config(self):
        cfg = load_config()
        if "maketx" in cfg:
            self.maketx_edit.setText(cfg["maketx"])

    def append_log(self, text): self.log.appendPlainText(text)

    def set_busy(self, busy: bool):
        self.start_btn.setEnabled(not busy)
        self.cancel_btn.setEnabled(busy)

    def on_browse_folder(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose folder")
        if d: self.path_edit.setText(d)

    def on_browse_ocio(self):
        f, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Choose .ocio", str(Path.home()), "OCIO (*.ocio)")
        if f: self.ocio_edit.setText(f)

    def on_browse_maketx(self):
        f, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Choose maketx.exe", str(Path.home()), "maketx (maketx.exe)")
        if f:
            self.maketx_edit.setText(f)
            cfg = load_config(); cfg["maketx"] = f; save_config(cfg)

    def on_start(self):
        folder = self.path_edit.text().strip()
        if not folder: return
        root_dir = Path(folder)
        if not root_dir.exists(): return
        ocio_file = self.ocio_edit.text().strip() or None
        verbose = self.verbose_chk.isChecked()
        recursive = self.recursive_chk.isChecked()
        filter_str = self.filter_edit.text().strip()
        maketx_path = self.maketx_edit.text().strip()
        if not maketx_path:
            QtWidgets.QMessageBox.warning(self,"Missing maketx","Select maketx.exe")
            return
        cfg = load_config(); cfg["maketx"] = maketx_path; save_config(cfg)

        self.progress.setValue(0)
        self.append_log(f"=== Starting on {root_dir} ===")

        self.worker_thread = QtCore.QThread(self)
        self.worker = ConvertWorker(root_dir, filter_str, recursive, ocio_file, verbose, maketx_path)
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.item_done.connect(self.append_log)
        self.worker.progress.connect(self.on_progress)
        self.worker.finished.connect(self.on_finished)
        self.worker.fatal.connect(self.on_fatal)
        self.worker.finished.connect(self._stop_worker_thread)
        self.worker.fatal.connect(self._stop_worker_thread)
        self.set_busy(True)
        self.worker_thread.start()

    def _stop_worker_thread(self,*args):
        if self.worker: self.worker.cancel()
        if self.worker_thread:
            self.worker_thread.quit(); self.worker_thread.wait()
        self.worker=None; self.worker_thread=None
        self.set_busy(False)

    def on_cancel(self):
        if self.worker: self.worker.cancel()
        self.append_log("Cancellation requested…")

    def on_progress(self, done,total): self.progress.setValue(int(done/max(1,total)*100))

    def on_finished(self, ok, fail): self.append_log(f"Done. Success:{ok} Fail:{fail}")

    def on_fatal(self,msg):
        self.append_log("FATAL: "+msg)
        QtWidgets.QMessageBox.critical(self,"Fatal",msg)

    def on_save_log(self):
        f, _ = QtWidgets.QFileDialog.getSaveFileName(self,"Save Log",str(Path.home()/ "txconvert_log.txt"),"Text (*.txt)")
        if f:
            with open(f,"w",encoding="utf-8") as fh: fh.write(self.log.toPlainText())
            QtWidgets.QMessageBox.information(self,"Saved",f"Log saved to {f}")


# ---------------------------
# Entry
# ---------------------------

def main():
    args = argparse.ArgumentParser()
    app = QtWidgets.QApplication(sys.argv)
    ui = TxConverterUI(); ui.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
