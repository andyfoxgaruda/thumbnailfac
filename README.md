# PhotoCraft Thumbnail Studio

A local, editable anime thumbnail workflow controlled through PhotoCraft's native MCP bridge. It includes an OAuth-protected MCP server, a private upload/review portal, Art DNA style context, anime asset sourcing, local segmentation, and a Windows desktop manager with scheduling.

## What belongs in this repository

This repository contains source code, tests, and setup instructions. Artwork, `ReferenceLibrary`, Art DNA data, projects, exports, models, databases, credentials, local network identity, and schedules stay on the owner's computer. `.gitignore` excludes them. No artwork or PhotoCraft binaries are supplied here.

## Requirements

- Windows with Python 3.15 and PhotoCraft installed (`photocraft-cli.exe` on `PATH`).
- Internet access only when sourcing artwork or installing dependencies/models. The editing system runs locally.
- No paid image API is required. Provider availability and artwork reuse rights are not guaranteed.

## Setup

From PowerShell in the checkout:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
```

The script creates `.venv` if absent, installs the pinned runtime packages, and creates local `secrets.json` and `studio_network.json` **only if missing**. It never prints or replaces existing credentials. The default network config accepts only local loopback access; configure your own private address in `studio_network.json` before remote access. To install the optional anime and general cutout models:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -InstallSegmentation
```

Start the MCP server and portal in separate terminals for a first check:

```powershell
.\.venv\Scripts\python.exe server.py
.\.venv\Scripts\python.exe portal.py
```

For regular use without flashing terminals, run `install_studio_startup.ps1` and `install_studio_manager.ps1` with the same PowerShell execution-policy command pattern, then open the **Thumbnail Studio** desktop shortcut. The manager starts/stops both services and controls the daily schedule. It stores schedule settings locally.

## Main flow

1. Upload analysis references or working assets in the portal. The protected `ReferenceLibrary` is separate from working assets.
2. Ask an MCP client for `get_art_dna_brief`, then `source_poster_assets` (see [anime sourcing](ANIME_SOURCING.md)) or use uploaded assets.
3. Import a pack with `load_asset_pack_into_photocraft`, or edit with `native_compose` / `native_edit_batch`. The gateway only forwards reviewed native commands; it does not expose unrestricted shell access.
4. Create a review job and submit a result. The **Ongoing** portal tab holds pending results; owner approval moves a finished PNG to **Takeout**.

## Quick checks

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -q
```

Installed-app integration checks are opt-in: set `PHOTOCRAFT_RUN_LIVE=1` and run `tests.test_photocraft_gateway`. The ordinary tests use isolated temporary workspaces. Tests must not write to `ReferenceLibrary`.

## Security and local data

`secrets.json` and `studio_network.json` are local-only. Keep OAuth behind your trusted private network and retain the gateway's reviewed command allowlist. Downloaded anime art is attributed where metadata is available and is marked with unknown reuse rights unless verified separately. Do not add user artwork or model files to Git with `git add -f`.
