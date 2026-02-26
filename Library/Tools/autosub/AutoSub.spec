# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['autosub_gui.py'],
    pathex=[],
    binaries=[('D:\\Program Files\\CapCut\\7.7.0.3143\\ffmpeg.exe', '.')],
    datas=[('..\\vdown', 'Library\\Tools\\vdown'), ('..\\transcriber', 'Library\\Tools\\transcriber'), ('..\\hardsubber', 'Library\\Tools\\hardsubber'), ('..\\subtranslator', 'Library\\Tools\\subtranslator'), ('..\\common', 'Library\\Tools\\common'), ('autosub.py', 'Library\\Tools\\autosub'), ('autosub_gui.py', 'Library\\Tools\\autosub'), ('agent_task_runner.py', 'Library\\Tools\\autosub'), ('apply_style.py', 'Library\\Tools\\autosub'), ('defaults.json', 'Library\\Tools\\autosub'), ('smart_translate.py', 'Library\\Tools\\autosub'), ('autosub.ico', 'Library\\Tools\\autosub'), ('C:\\Program Files\\Python\\Python312\\DLLs\\sqlite3.dll', '.')],
    hiddenimports=['yt_dlp', 'faster_whisper', 'torch', 'torchaudio', 'google.generativeai', 'pysubs2', 'tkinter'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'notebook', 'test', 'nltk', 'llama_index'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AutoSub',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['autosub.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='AutoSub',
)
