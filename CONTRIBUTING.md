# Contributing

Use Python 3.10+ and Node.js 20+ for development. The runtime has no pip or npm dependencies. Real-device access requires macOS; tests use in-memory transport and audio doubles.

```sh
python3 -m unittest discover -s tests -v
node --test tests/test_write_queue.cjs
node --check app.js
node --check panel_layout.js
node --check write_queue.js
python3 -m compileall -q .
```

Preview without hardware:

```sh
python3 tests/serve_preview.py
```

Open the localhost URL printed by the preview process. This preview uses simulated signals and temporary storage. Do not start `kb22_remote.py` to run unit tests.

For protocol changes, add a focused regression test using existing fixtures or sanitized observations. Label whether evidence is simulated, replayed, or measured on hardware. Keep “sent” separate from “confirmed by device” in both code and documentation. Do not remove mode, sample-rate, slot, or stale-operation guards.

UI contributions should remain usable at 360 px and desktop widths. Use project-owned or explicitly licensed assets with attribution. Do not add extracted vendor fonts, icons, application binaries, or disassembly.

Never commit `data/`, access URLs containing real tokens, personal presets, raw diagnostic logs, local user paths, or audio recordings. When reporting a problem, include OS/Python version, device mode and sample rate, reproduction steps, and sanitized error details. GitHub Actions only runs offline checks and does not prove real-device compatibility.
