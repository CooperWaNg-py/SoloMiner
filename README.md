# SoloMiner

A lightweight macOS menu bar application for solo Bitcoin mining. Uses Apple Metal for GPU-accelerated SHA-256d hashing and connects to pools via the Stratum v1 protocol.

By **Cooper Wang**

---

## Features

- **Menu bar app** -- lives in your macOS menu bar, no dock icon
- **GPU mining** -- Apple Metal compute shader for SHA-256d (~69 MH/s on M2 Pro)
- **CPU fallback** -- works on Macs without Metal (much slower)
- **Stratum v1** -- connects to any Bitcoin solo mining pool
- **Auto-reconnect** -- recovers from dropped connections with jitter
- **Difficulty auto-tune** -- measures hashrate and suggests optimal pool difficulty
- **Address validation** -- checks P2PKH, P2SH, bech32, and taproot formats before mining
- **Start at login** -- optional launchd agent for automatic startup
- **Live dashboard** -- hashrate, shares, uptime, GPU info, animated status
- **Built-in benchmark** -- test your GPU hash rate without connecting to a pool
- **Terminal UI** -- full curses-based TUI with the same features as the GUI
- **CLI mode** -- headless mining from the command line
- **Crash logging** -- all crashes written to `~/Library/Application Support/SoloMiner/crash.log`

## Requirements

- **macOS 12.0+** (Monterey or later)
- **Python 3.10+**
- Apple Silicon (M1/M2/M3/M4) or Intel Mac with Metal support

## Quick Start

### 1. Install dependencies

```bash
pip3 install -r requirements.txt
```

This installs:
- `pyobjc-core` -- Python-ObjC bridge
- `pyobjc-framework-Cocoa` -- Foundation + AppKit
- `pyobjc-framework-Metal` -- GPU compute
- `pyobjc-framework-Quartz` -- Core Animation (ambient UI effects)

### 2. Run the GUI

```bash
python3 main.py
```

A menu bar item labeled **SoloMiner** appears in your macOS menu bar. Click it to open the popover dashboard.

### 3. Configure

1. Click **Settings** > **Mining** tab
2. Enter your Bitcoin address (bc1q...)
3. Select a network (Mainnet for real mining)
4. Go to **Pools** tab to pick or add a pool
5. Click **Save Configuration**

### 4. Mine

Click **Start** on the dashboard. The status dot pulses green when actively mining.

## Running Modes

### Menu Bar GUI (default)

```bash
python3 main.py
```

Full native macOS popover UI with dark theme, glass-card styling, and ambient animations.

### Terminal UI

```bash
python3 main.py --tui
```

Curses-based interface with full feature parity. Works over SSH.

### CLI (headless)

```bash
python3 cli.py --address bc1qYOUR_ADDRESS --pool public-pool.io --port 3333
```

Minimal command-line miner with colored log output. Good for running in tmux/screen.

```bash
python3 cli.py --benchmark
```

Run a GPU benchmark without connecting to a pool.

## Building a .app Bundle

You can package SoloMiner as a standalone macOS .app that runs without a Python installation.

### Option A: PyInstaller (recommended)

```bash
pip3 install pyinstaller
pyinstaller SoloMiner.spec
```

The app bundle is created at `dist/SoloMiner.app`. You can drag it to `/Applications`.

> **Note:** PyInstaller may have issues bundling PyObjC on some Python versions. If the .app crashes on launch with `ModuleNotFoundError: No module named 'Foundation'`, use Option B or run directly with `python3 main.py`.

### Option B: py2app

```bash
pip3 install py2app
python3 setup.py py2app
```

The app bundle is created at `dist/SoloMiner.app`.

> **Note:** py2app requires Python <= 3.13 as of py2app 0.28. If you're on Python 3.14+, use PyInstaller or run directly.

### Option C: Nuitka (advanced)

```bash
pip3 install nuitka
python3 -m nuitka --macos-app-name=SoloMiner --macos-app-mode=ui-element \
    --include-package=solominer --include-module=objc \
    --include-module=Foundation --include-module=AppKit \
    --include-module=Metal --include-module=Quartz \
    --standalone --onefile main.py
```

### After building

The .app bundle is configured with `LSUIElement = true`, so it runs as a menu bar app with no dock icon. Double-click it or:

```bash
open dist/SoloMiner.app
```

## Project Structure

```
SoloMiner/
    main.py              # Entry point, crash handlers, --tui routing
    cli.py               # Standalone CLI miner
    setup.py             # py2app build config
    SoloMiner.spec       # PyInstaller build config
    requirements.txt     # Python dependencies
    solominer/
        __init__.py      # Package init, version
        config.py        # MinerConfig, JSON persistence, address validation, launchd
        engine.py        # MiningEngine: stratum + GPU mining loop
        metal_miner.py   # Metal SHA-256d shader, MetalMiner class
        stratum.py       # Stratum v1 protocol client
        ui.py            # PyObjC/AppKit menu bar GUI
        tui.py           # Curses terminal UI
```

## Default Pools

| Pool | Host | Port |
|------|------|------|
| public-pool.io | public-pool.io | 3333 |
| VKBIT SOLO | eu.vkbit.com | 3555 |
| nerdminer.io | pool.nerdminer.io | 3333 |
| CKPool Solo (EU) | eusolo.ckpool.org | 3333 |
| CKPool Solo (US) | solo.ckpool.org | 3333 |

You can add custom pools in Settings > Pools.

## How It Works

1. **Stratum v1** -- SoloMiner connects to a mining pool, subscribes, and receives block templates (jobs)
2. **SHA-256d** -- each job is an 80-byte block header. The miner varies the 4-byte nonce field and computes `SHA256(SHA256(header))` for each candidate
3. **Metal GPU** -- the SHA-256d computation runs on the GPU via an Apple Metal compute shader, dispatching ~4 million nonces per batch
4. **Share submission** -- when a hash meets the pool's share difficulty target, the nonce is submitted back to the pool
5. **Difficulty auto-tune** -- after ~15 seconds of mining, the engine measures hashrate and requests an optimal difficulty from the pool targeting ~1 share every 20 seconds

Solo mining means you're trying to find an actual Bitcoin block. The odds are astronomically low with consumer hardware (~1 in 10^15 per block at current difficulty), but if you do find one, the entire block reward (~3.125 BTC) is yours.

## Configuration

Settings are stored at:
```
~/Library/Application Support/SoloMiner/config.json
```

Other files in the same directory:
- `activity.log` -- mining activity log
- `stats.json` -- cumulative statistics (hashes, runtime, shares)
- `crash.log` -- crash reports

Login item plist (when "Start at Login" is enabled):
```
~/Library/LaunchAgents/com.cooperwang.solominer.plist
```

## License

MIT
