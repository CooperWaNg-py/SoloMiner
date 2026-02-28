# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for SoloMiner.

Usage:
    pyinstaller SoloMiner.spec
"""

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('logo.png', '.')],
    hiddenimports=[
        # PyObjC core
        'objc', 'objc._bridges', 'objc._bridgesupport', 'objc._callable_docstr',
        'objc._category', 'objc._compat', 'objc._context', 'objc._convenience',
        'objc._convenience_mapping', 'objc._convenience_nsarray',
        'objc._convenience_nsdata', 'objc._convenience_nsdecimal',
        'objc._convenience_nsdictionary', 'objc._convenience_nsobject',
        'objc._convenience_nsset', 'objc._convenience_nsstring',
        'objc._convenience_sequence', 'objc._descriptors', 'objc._dyld',
        'objc._framework', 'objc._informal_protocol', 'objc._lazyimport',
        'objc._locking', 'objc._machsignals', 'objc._new', 'objc._objc',
        'objc._properties', 'objc._protocols', 'objc._pycoder',
        'objc._pythonify', 'objc._structtype', 'objc._transform',
        'objc._types', 'objc.simd',
        # Foundation
        'Foundation', 'Foundation._Foundation', 'Foundation._context',
        'Foundation._functiondefines', 'Foundation._inlines',
        'Foundation._metadata', 'Foundation._nsindexset',
        'Foundation._nsobject', 'Foundation._nsurl',
        # AppKit
        'AppKit', 'AppKit._AppKit', 'AppKit._inlines',
        'AppKit._metadata', 'AppKit._nsapp',
        # Metal
        'Metal', 'Metal._Metal', 'Metal._inlines', 'Metal._metadata',
        # Quartz (for Core Animation)
        'Quartz', 'Quartz.QuartzCore', 'Quartz.QuartzCore._metadata',
        'Quartz.QuartzCore._quartzcore', 'Quartz.CoreGraphics',
        'Quartz.CoreGraphics._coregraphics', 'Quartz.CoreGraphics._metadata',
        'Quartz.CoreGraphics._inlines',
        # App modules
        'solominer', 'solominer.config', 'solominer.engine',
        'solominer.metal_miner', 'solominer.stratum',
        'solominer.ui', 'solominer.tui',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SoloMiner',
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
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='SoloMiner',
)
app = BUNDLE(
    coll,
    name='SoloMiner.app',
    icon='SoloMiner.icns',
    bundle_identifier='com.cooperwang.solominer',
    info_plist={
        'CFBundleName': 'SoloMiner',
        'CFBundleDisplayName': 'SoloMiner',
        'CFBundleIconFile': 'SoloMiner',
        'CFBundleVersion': '1.3.0',
        'CFBundleShortVersionString': '1.3.0',
        'LSUIElement': True,
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '12.0',
    },
)
