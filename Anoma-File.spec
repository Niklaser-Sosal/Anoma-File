# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

project = Path(SPECPATH)
datas = [
    (str(project / 'web'), 'web'),
    (str(project / 'assets'), 'assets'),
    (str(project / 'engine'), 'engine'),
]

block_cipher = None

a = Analysis(
    ['app.py'],
    pathex=[str(project)],
    binaries=[],
    datas=datas,
    hiddenimports=['webview.platforms.winforms', 'webview.platforms.edgechromium'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PyQt5', 'PyQt5.QtCore', 'PyQt5.QtGui', 'PyQt5.QtWidgets', 'PyQt5.QtNetwork', 'PyQt5.QtWebEngine', 'PyQt5.QtWebEngineCore', 'PyQt5.QtWebEngineWidgets', 'PyQt6', 'PySide2', 'PySide6'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='Anoma-File',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=str(project / 'assets' / 'anoma-file.ico'),
)
