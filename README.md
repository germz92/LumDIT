# LumDIT

Cross-platform (Windows / macOS) DIT and media-management tool for **verified SD card offloads**.

- Create a production and get a ready-made folder hierarchy.
- Browse cards and folders with thumbnails; double-click opens the file in the system viewer/player.
- Drag a card folder onto the production panel to start a checksum-verified backup.
- Every offload writes an **MHL 1.1 manifest**, a `.xxh64` sidecar and a plain-text report into the card folder.

## Folder hierarchy

```
<Client Name>/
  <Production Name>/
    production.json
    Photo/
      10.05.2026/
        FX3 - Germaine (#12)/   <- created at offload time; contains the card's full tree + MHL
      Project Files/
      Deliverables/
    Video/            (same)
    Headshot Booth/   (same)
```

Date folders are `MM.DD.YYYY`, one per day of the shoot. The number in `(#12)` is the label
written on the physical card, so it is entered by the operator rather than auto-incremented; the
dialog remembers the last card used per camera/operator and warns when a card has already been
offloaded. The card folder name template (`{camera} - {operator} ({card})` by default) can be
changed in Settings. `{card}` renders as `#12` for numbered cards or as the label itself (e.g.
`Internal`) when the card log uses free text.

## Event Backup (card log)

LumDIT can read the crew app's card log from MongoDB and walk the DIT card-by-card.

1. Put the connection string in `lumdit/.env` as `MONGO_URI=...` (or enter it under
   Settings > Card Log; `.env` is git-ignored and never bundled into installers). Use a database
   user limited to read/write on the card-log database. Click **Test Connection** to confirm.
2. Click **Start Event Backup...** (welcome page, toolbar, or `Ctrl+E`) and pick an event. The
   production is created as `<root>/<Company>/<Event title>` (the event's company name is the
   client folder; both are editable before you start) with the event's dates and the
   only the categories found in its log (Photo / Video / Headshot Booth / Other) - a photo-only
   event gets just a `Photo` folder; others are added when a card for them is offloaded. Re-selecting the same
   event reuses the existing production.
3. The right-hand **Card Log** panel lists every card grouped by shoot date and highlights the next
   one to insert ("Insert card #32 - A7IV - Jennifer R."). Only card 1 of each dual-recording pair is
   requested; `Internal` recordings are listed but skipped.
4. Insert the card (or drop its folder on the drop zone). LumDIT asks *Offload / Different card... /
   Manual offload... / Ignore* - it never starts copying on its own. Camera, operator, card number,
   category and date all come from the log; the destination preview is shown before you confirm.
5. As soon as the transfer starts the panel moves to the next card ("Offload next card?") so the
   next card can be inserted while the previous one copies and verifies.
6. After a **verified** offload the entry's `card1BackedUp` box is ticked in MongoDB. If the database
   is unreachable the write-back is queued in `production.json` and retried on the next refresh
   (the panel refreshes every 60 s and on demand).

Reopening a production created from an event re-links it to the card log automatically.

### What gets copied from a card

When the source is the top of a camera card, LumDIT copies only the media folders that match
the category and keeps the card-relative structure under them (so XML sidecars, `MEDIAPRO.XML`
and thumbnails stay next to the clips for Catalyst / Resolve / Media Composer):

| Category | Folders copied | Notes |
|---|---|---|
| Photo, Headshot Booth | `DCIM` | Sony `100MSDCF`, Canon `100CANON`, Nikon, Fuji, Lumix, GoPro, DJI... |
| Video | `PRIVATE/M4ROOT` (Sony XAVC S/HS), `PRIVATE/XDROOT` (Sony MXF), `PRIVATE/AVCHD`, `MP_ROOT`, `CONTENTS` (Canon XF-AVC), `XFVC` / `CRM` / `XMLTAG` (Canon EOS R) | If the card has none of these but `DCIM` holds clips (Canon EOS R, Nikon, Fuji, Lumix, GoPro), `DCIM` is copied instead. |

Camera housekeeping (`MISC`, `CAMSET`, `AVF_INFO`, `PRIVATE/SONY`, OS metadata) is never copied.
The confirmation prompt lists what will be copied and what the category rule leaves on the card,
with an **Offload entire card** button when other media is present. Cards with no recognisable
layout (Blackmagic, RED `*.RDC`, ARRI, Atomos write clips at the root) and dropped sub-folders are
copied in full. The manual offload dialog has the same rule as a checkbox, and the
`OFFLOAD_REPORT.txt` records which roots were copied.

## Running from source

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS
source .venv/bin/activate

pip install -e .[dev]
python -m lumdit
```

Tests: `pytest`

## Log file

Every run writes a rotating log (2 MB x 5) with offload start/finish lines, per-file problems,
card-log activity and full tracebacks for anything unexpected. Open it from **Help > Open Log File**.

- Windows: `%LOCALAPPDATA%\LumDIT\lumdit.log`
- macOS: `~/Library/Logs/LumDIT/lumdit.log`

MongoDB credentials are redacted before they reach the file. Set `LUMDIT_DEBUG=1` for verbose output.

## Building installers

```bash
pyinstaller packaging/lumdit-windows.spec   # -> dist/LumDIT/LumDIT.exe
pyinstaller packaging/lumdit-macos.spec     # -> dist/LumDIT.app  (run on a Mac)
```

Windows installer (needs [Inno Setup 6](https://jrsoftware.org/isinfo.php)):

```bash
iscc /DAppVersion=0.1.0 packaging\lumdit.iss      # -> dist/LumDIT-0.1.0-Setup.exe
```

Drop a `lumdit.ico` / `lumdit.icns` into `packaging/` to get a custom app icon.

### Releases

Pushing a tag like `v0.1.0` runs `.github/workflows/release.yml`, which builds and tests on
Windows and macOS (Apple Silicon), produces `LumDIT-<ver>-Setup.exe`, a portable zip and
`LumDIT-<ver>-macOS-arm64.dmg`, and publishes them as a GitHub release with SHA-256 checksums.
The builds are not code-signed/notarised: Windows shows a SmartScreen prompt and macOS requires
right-click > Open on first launch.

## How a backup works

1. The source is scanned; OS junk (`.DS_Store`, `._*`, `Thumbs.db`, `System Volume Information`, ...) is skipped.
2. Pre-flight: destination must not be inside the source, and there must be enough free space.
3. Each file is streamed to `<name>.part` while an xxHash64 is computed on the bytes read from the card.
4. The written file is `fsync`'d, **read back** from the destination and hashed again. Mismatch -> retry once, then flagged.
5. `.part` is atomically renamed to the final name and the original modification time is restored.
6. Existing files are **never overwritten**: identical files are skipped (resumable), different ones are flagged as conflicts.
7. An MHL manifest, `.xxh64` sidecar and `OFFLOAD_REPORT.txt` are written next to the media.

Jobs run concurrently (default 2) but never two readers on the same card. The computer is kept awake while jobs run.
Use **Verify...** on any folder to re-check its files against the manifests before wiping cards.
