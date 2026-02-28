"""
py2app build script for SoloMiner.

Usage:
    pip install py2app
    python setup.py py2app

The .app bundle will be in dist/SoloMiner.app
"""

from setuptools import setup

APP = ["main.py"]
DATA_FILES = [("", ["logo.png"])]

OPTIONS = {
    "argv_emulation": False,
    "iconfile": "SoloMiner.icns",
    "plist": {
        "CFBundleName": "SoloMiner",
        "CFBundleDisplayName": "SoloMiner",
        "CFBundleIdentifier": "com.cooperwang.solominer",
        "CFBundleVersion": "1.3.0",
        "CFBundleShortVersionString": "1.3.0",
        "LSUIElement": True,  # Menu bar app - no dock icon
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
        "CFBundlePackageType": "APPL",
    },
    "packages": ["solominer"],
    "includes": [
        "objc",
        "Foundation",
        "AppKit",
        "Metal",
        "Quartz",
    ],
}

setup(
    name="SoloMiner",
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
