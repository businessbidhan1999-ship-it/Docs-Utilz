# Docs-Utilz

A local desktop application and command-line toolkit for removing repeated watermarks, adjusting document sizes, converting PDFs and images, and working with printers and scanners.

The application uses a cropped sample of a watermark to find similar marks across a document. Processing runs on your computer, without uploading documents to a service. It uses OpenCV template matching and PDF processing rather than a trained generative model.

## Contents

- [Features](#features)
- [Installation](#installation)
- [Desktop workflow](#desktop-workflow)
- [Command-line usage](#command-line-usage)
- [How watermark removal works](#how-watermark-removal-works)
- [Compression and exact file sizes](#compression-and-exact-file-sizes)
- [System Architecture](#system-architecture)
- [Directory Architecture](#directory-architecture)
- [Settings and local files](#settings-and-local-files)
- [Printing and scanning](#printing-and-scanning)
- [Building executables](#building-executables)
- [Development](#development)
- [Troubleshooting](#troubleshooting)

## Features

| Area | Capabilities |
| --- | --- |
| Watermark removal | Detect repeated marks using a cropped sample; preview results; process selected pages; choose raster or PDF object removal |
| Raster cleanup | Reconstruct paper, reverse a translucent tint, inpaint, fill with paper color, or blank a detected rectangle |
| File sizes | Compress PDF, JPEG, and PNG files; set an exact file size through the desktop interface |
| Conversion | Export PDF pages as PNG or JPEG; assemble images into a PDF with image-sized, A4, or Letter pages |
| Devices | Print documents and acquire a scanned page through supported system tools and drivers |
| Desktop interface | Page previews, zoom, progress reporting, activity logs, and Paper/Ink themes |
| Command line | Watermark removal, compression, PDF-to-image conversion, and image-to-PDF conversion |

Supported document formats are PDF, PNG, JPG, and JPEG. Device integration targets Windows and Linux. The repository includes executable build scripts for those two platforms; macOS device integration and executable builds are not provided.

## Installation

### Requirements

- Python **3.10 or newer**.
- Tkinter for the desktop interface.
- A graphical desktop session to open the application.
- System printer/scanner tools and drivers if using device features.

Python dependencies are declared in `requirements.txt`:

| Dependency | Purpose |
| --- | --- |
| PyMuPDF | PDF rendering, object removal, document writing, and optimization |
| OpenCV | Template matching, image masks, and inpainting |
| NumPy | Pixel arrays and numerical operations |
| Pillow | Image loading, encoding, resizing, and Tkinter previews |

### Linux

Clone or download this repository and open a terminal in its root directory. On Debian or Ubuntu, install the desktop prerequisites:

```bash
sudo apt update
sudo apt install python3-venv python3-tk libgl1 libglib2.0-0
```

Create an environment and install the application:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install .
```

Other distributions use different package names for Tkinter and the OpenCV system libraries.

### Windows

Install Python 3.10 or newer with Tcl/Tk support, then open Command Prompt in the repository directory:

```bat
py -m venv .venv
.venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install .
```

The activation command above is for Command Prompt. In PowerShell, use `.venv\Scripts\Activate.ps1` if your local execution policy permits it.

### Launch

With the environment activated:

```bash
docs-utilz-gui
```

You can also launch using Python directly:

```bash
python -m docs_utilz
```

The command-line entry point is:

```bash
docs-utilz --help
```

Its module equivalent is `python -m docs_utilz.cli`. The examples below use the installed command; either form calls the same CLI.

To check Tkinter separately, run `python -m tkinter`. It should open a small test window.

## Desktop workflow

1. Open a PDF or supported picture in the watermark workspace.
2. Load a cropped picture of the watermark. Use a tight crop that contains the mark with as little unrelated content as possible.
3. Choose a preset:
   - **Remove it completely** uses raster cleanup and protects dark content where possible.
   - **Wipe the whole area blank** uses raster cleanup without dark-content protection.
   - **Keep the text selectable** applies PDF object removal.
   - **Custom** uses the detailed settings in Advanced options.
4. Preview a representative page. Check both the detected regions and the surrounding content.
5. Adjust confidence, fill strategy, resolution, or rotation search if needed.
6. Save the processed result to a separate output file.

The size workspace accepts multiple files and lets you choose which ones to process. Conversion tools export PDF pages or assemble pictures in the order shown in the list. The device workspace provides printer and scanner settings.

## Command-line usage

### Remove a watermark

Watermark removal is the default operation, so it has no subcommand:

```bash
docs-utilz input.pdf sample.png -o cleaned.pdf
```

For an image:

```bash
docs-utilz photo.jpg sample.png -o photo_clean.jpg
```

Without `-o`, the command adds `_clean` to the input filename. It refuses to use the input path as the output path.

Check detections without writing a processed document:

```bash
docs-utilz input.pdf sample.png --dry-run
```

Use PDF object removal and include text objects among the removal targets:

```bash
docs-utilz input.pdf sample.png -o cleaned.pdf --mode smart --targets images,graphics,text
```

Process selected pages and search additional rotations:

```bash
docs-utilz input.pdf sample.png -o cleaned.pdf --pages "1-3,7" --dpi 300 --rotations
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--mode` | `raster` | Raster cleanup or `smart` PDF object removal |
| `--dpi` | `200` | PDF rendering resolution for detection and raster output |
| `--threshold` | `0.62` | Template correlation threshold; lower values admit more candidates |
| `--verify` | `0.42` | Required shape agreement; lower values accept less similar shapes |
| `--fill` | `clean` | Raster fill strategy: `clean`, `unblend`, `inpaint`, `background`, or `box` |
| `--targets` | `images,graphics` | PDF object types eligible for smart removal; optionally include `text` |
| `--rotations` | Disabled | Search −45°, −30°, 0°, 30°, and 45° |
| `--pages` | All pages | One-based page numbers and ranges, such as `1-3,7` |
| `--dry-run` | Disabled | Print match counts and scores without writing output |

Page selection limits which pages receive watermark removal. **Raster mode still renders every page in the output PDF, including unselected pages**, so selectable text is lost throughout that output. Smart mode keeps the existing document structure outside its redactions.

### Compress documents

```bash
docs-utilz compress report.pdf --target 1000
docs-utilz compress photo1.jpg photo2.jpg --target 500 --outdir small
```

Targets use 1,024 bytes per KB. The default output suffix is `_small`.

PDF compression preserves selectable text by default. Allow page flattening if you prefer a smaller file over selectable text:

```bash
docs-utilz compress report.pdf --target 500 --flatten
```

Prevent PNG-to-JPEG fallback:

```bash
docs-utilz compress scan.png --target 300 --no-format-change
```

A target is not guaranteed to be achievable. The command reports whether it reached the requested size and may save a best-effort result. Compression returns exit status `1` if any file fails or misses its target; success returns `0`.

### Export PDF pages

```bash
docs-utilz pdf2img report.pdf --outdir pages --format png --dpi 300
docs-utilz pdf2img report.pdf --outdir previews --format jpg --quality 85 --pages "1-3"
```

Without `--outdir`, output goes into `<pdf name>_pages` beside the input. Page filenames follow the pattern `<stem>_page_001.png` or the selected JPEG extension.

### Assemble pictures into a PDF

```bash
docs-utilz img2pdf page1.jpg page2.jpg -o combined.pdf --size a4
docs-utilz img2pdf front.png back.png -o scan.pdf --size letter --margin 18
```

Pictures appear in the order supplied. Margins are measured in PDF points, with 72 points per inch. The default `--size image` makes each PDF page fit its picture at the specified DPI.

Use explicit filenames when order matters. Shell wildcard expansion varies between operating systems and shells.

## How watermark removal works

### Detection

The detector uses a supplied example rather than trying to recognize arbitrary watermarks:

1. Load the sample as grayscale and trim flat borders.
2. Render the PDF page to an RGB image, or load the source picture.
3. Search a coarse set of scales and optional angles to find the strongest correlation.
4. Search more closely around the winning scale and angle to collect repeated instances.
5. Suppress overlapping detections and compare the candidate shapes with the sample.

Shape verification helps reject regions that have similar brightness but different strokes. Lower-confidence candidates require stronger shape agreement because watermarks crossing body text can correlate less strongly than marks on blank paper.

The default scale search spans roughly 0.55–1.85 times the sample size. The detector chooses one best angle and scale neighborhood per page, then searches for instances around that choice. It works best when marks on a page have a consistent appearance and size; it is not an unrestricted search for every possible watermark variation.

### Raster mode

Raster mode edits rendered pixels and creates a new image-based PDF, or saves a cleaned picture. It can work on scanned documents where the watermark is embedded in the image.

| Fill | Approach | Tradeoff |
| --- | --- | --- |
| `clean` | Estimate the local paper surface and reconstruct the marked area | Useful on document backgrounds; can alter underlying detail |
| `unblend` | Estimate and reverse a translucent watermark's tint | Depends on the transparency estimate; strong marks may leave residue |
| `inpaint` | Use OpenCV to infer pixels from the surrounding area | May smear text or fine structure |
| `background` | Replace masked strokes with a sampled paper color | Fast, but uneven lighting can reveal the patch |
| `box` | Clear the entire detected rectangle | Removes other content inside that rectangle too |

Dark-content protection attempts to retain body text. It is a heuristic: content that was fully covered by an opaque mark cannot be reliably recovered. Preview the result when a mark crosses text, signatures, photographs, or diagrams.

Raster PDF output embeds JPEG page images at the chosen rendering resolution. It does not perform OCR, so the resulting pages do not retain selectable text or the original vector detail.

### Smart mode

Smart mode detects marks on a rendered preview, converts the detected pixel coordinates into PDF points, and applies PyMuPDF redactions to selected object types.

Image and graphics targets remove eligible objects intersecting a detected region. Optional text removal checks text spans against the region's overlap and containment. This can preserve selectable text elsewhere, but intersecting images, graphics, or text can also be affected.

Smart mode is most useful when a watermark is separate PDF content. It cannot clean a watermark baked into a scanned page without affecting the image that contains it. Detection counts describe candidate watermark regions; they are not proof that every mark was removed successfully.

## Compression and exact file sizes

Image compression searches a ladder of quality, scale, and encoding settings for a result under the target. PNG compression can use palette reduction and may switch to JPEG when allowed.

PDF compression proceeds through increasingly destructive options:

1. Rebuild and optimize the PDF structure losslessly.
2. Re-encode embedded images while retaining text objects.
3. If explicitly allowed, render pages as images at reduced settings.

The desktop interface also supports **exact file sizes**. It compresses files that are too large, then adds nonvisual, format-specific padding to reach the requested byte count. Files that are already smaller can be padded without changing their pixels or PDF text. Increasing byte size does not increase resolution or restore detail.

Exact sizing is available through `tools.resize_file()` and the desktop interface. The CLI `compress` command targets an upper size limit rather than an exact byte count.

## System Architecture

Docs-Utilz runs as a local Python application. Its desktop and command-line interfaces call shared processing functions in the same process. Document processing needs no application server, database, or remote processing API. Printing and scanning cross the process boundary to use operating system tools and device drivers.

### Components and data flow

```mermaid
flowchart TB
    User[User]
    Files[(Local PDF and image files)]
    Prefs[(Local settings and saved sample)]

    subgraph App["Docs-Utilz application process"]
        GUI["Desktop interface<br/>Tkinter main thread"]
        CLI["Command-line interface<br/>Argument parsing and synchronous execution"]
        Workers["Background worker threads<br/>Desktop jobs"]
        Queue["Message queue<br/>Progress, results, and errors"]
        Engine["Watermark engine<br/>Detection and raster or smart removal"]
        Tools["Document tools<br/>Compression, exact sizing, and conversion"]
        Devices["Device bridge<br/>Print and scan requests"]
    end

    subgraph OS["Operating system and external devices"]
        Native["Windows: PowerShell, printing APIs, WIA<br/>Linux: CUPS and SANE commands"]
        Hardware[Printer or scanner]
    end

    User --> GUI
    User --> CLI
    GUI -->|Capture settings and start job| Workers
    CLI --> Engine
    CLI --> Tools
    Workers --> Engine
    Workers --> Tools
    Workers --> Devices
    Workers -->|Publish job messages| Queue
    Queue -->|Polled by Tkinter event loop| GUI
    GUI <-->|Load and save preferences| Prefs
    Files -->|Read documents and watermark samples| Engine
    Files -->|Read source documents| Tools
    Engine -->|Write cleaned files| Files
    Tools -->|Write resized or converted files| Files
    Devices <-->|Read print input or save scan output| Files
    Devices --> Native
    Native <--> Hardware
```

The desktop interface captures widget values on the main thread before starting a worker. That worker calls the selected processing function and reports progress through callbacks. Progress, completion, and error messages return through a queue that the Tkinter event loop polls roughly every 100 milliseconds. The main thread then updates previews, status labels, and activity logs.

The CLI calls the same watermark and document functions synchronously and prints progress to the terminal. Printing, scanning, and exact file sizing are exposed through the desktop interface or Python functions; the CLI currently offers watermark removal, compression, and the two conversion commands.

| Component | Responsibility | Main dependencies |
| --- | --- | --- |
| Watermark engine | Render or load pages, find sample matches, verify shapes, apply cleanup, and write results | PyMuPDF, OpenCV, NumPy, Pillow |
| Document tools | Search compression settings, add exact-size padding, render PDF pages, and assemble images into PDFs | PyMuPDF, Pillow, Python standard library |
| Device bridge | Discover devices, invoke native print/scan operations, and convert acquired images when needed | System commands, PowerShell/WIA on Windows, CUPS/SANE on Linux, document tools |
| Desktop coordinator | Read user choices, launch workers, consume messages, show previews, and persist preferences | Tkinter, threading, queue, shared processing modules |
| CLI coordinator | Parse arguments, invoke processing functions, print progress, and return exit statuses | argparse, shared processing modules |

### Watermark processing pipeline

```mermaid
flowchart TD
    Input["Input document + cropped watermark sample + Settings"]
    Load["Load sample and trim flat borders"]
    Render["Render a PDF page or load an image"]
    Detect["Search scales and optional angles<br/>Collect template matches"]
    Verify["Suppress duplicate regions<br/>Verify stroke shape agreement"]
    Preview{"Preview or process?"}
    Show["Return annotated and cleaned previews<br/>with detected regions"]
    Mode{"Input and removal mode"}
    Raster["Raster cleanup<br/>Apply fill strategy to pixels"]
    Smart["Smart PDF cleanup<br/>Map pixels to PDF points and redact selected objects"]
    Encode["Encode cleaned picture<br/>or image-based PDF pages"]
    Save["Save processed output"]
    Report["Return match counts and processing report"]

    Input --> Load --> Render --> Detect --> Verify --> Preview
    Preview -->|Preview| Show
    Preview -->|Process| Mode
    Mode -->|Image input or raster PDF mode| Raster
    Mode -->|PDF input with smart mode| Smart
    Raster --> Encode --> Save
    Smart --> Save
    Save --> Report
```

For a PDF, page processing repeats according to the selected mode and page range. Raster output reconstructs all PDF pages as images, including pages excluded from watermark removal. Smart output edits the original PDF's selected regions and preserves the remaining document structure.

The preview function uses raster cleanup to illustrate the detected regions, even when smart mode is selected. Smart-mode output must therefore be checked separately after saving.

`engine.Settings` carries the operation's parameters; `engine.Match` represents a detected region. A match is evidence of visual similarity, so previews and reported counts should be assessed alongside the saved result. The distinction between raster and smart output is explained in [How watermark removal works](#how-watermark-removal-works).

Compression follows a separate path: source file → candidate encodings → size checks → chosen output. Exact sizing adds format-specific padding after compression when necessary. Conversion either renders PDF pages into images or inserts ordered images into a new PDF. These operations share the document tools rather than passing through watermark detection.

### Using the processing API

```python
from docs_utilz.engine import Settings, process

settings = Settings(mode="raster", dpi=200, fill="clean")
report = process(
    "input.pdf",
    "sample.png",
    "cleaned.pdf",
    settings,
    progress=lambda fraction, message: print(f"{fraction:.0%}: {message}"),
)
print(report)
```

The report includes the total page count, processed page count, match count, and per-page counts. Python API page selections use zero-based indexes, for example `Settings(pages=(0, 1, 2, 6))`; the CLI accepts one-based page numbers.

Choose a separate output path when calling processing functions directly. Output naming and overwrite checks vary by operation; do not assume every low-level function has the same protections as the GUI.

## Directory Architecture

The source package, tests, and build tooling are organized as follows:

```text
docs-utilz/
├── docs_utilz/
│   ├── __init__.py
│   ├── __main__.py       # Module launch entry point
│   ├── app.py            # Tkinter interface, previews, preferences, job coordination
│   ├── cli.py            # Argument parsing and batch commands
│   ├── engine.py         # Watermark detection and raster/PDF removal
│   ├── tools.py          # Compression, exact sizing, PDF/image conversion
│   ├── devices.py        # Windows and Linux printer/scanner bridges
│   └── ui_theme.py       # Tkinter palettes and widget styling
├── tests/                # unittest coverage and mocked device integration
├── packaging/            # PyInstaller spec, build scripts, installers, release tools
├── pyproject.toml        # Package metadata and executable entry points
├── requirements.txt      # Runtime dependencies
├── .gitignore
└── README.md
```

## Settings and local files

The desktop application saves preferences in `settings.json`. A watermark sample can be remembered as `watermark.png` when that option is enabled.

The storage location is chosen in this order:

1. Beside a frozen executable, if that directory is writable.
2. At the package's parent directory when running from Python, if writable. In a source checkout, this is the repository root.
3. A `Docs-Utilz` directory beneath `%APPDATA%` on Windows, or `~/.config` when `APPDATA` is absent.

Document processing stays local. Printing and scanning use your system's configured devices, which may themselves be network-connected.

Git ignores generated builds, Python caches, virtual environments, local settings, and agent state. Markdown files are ignored except this root `README.md`.

## Printing and scanning

### Windows

The device bridge uses PowerShell, Windows printing APIs, and WIA scanner drivers. Install and configure the manufacturer's drivers before using the device workspace. A scanner must expose a usable WIA interface; installing a TWAIN-only driver is insufficient for this integration.

Driver acquisition dialogs can override the application's scan resolution and paper-source settings. Device support, paper trays, duplex printing, and available scan sources depend on the installed driver.

### Linux

Printing uses the CUPS command-line tools; scanning uses SANE's `scanimage`. On Debian or Ubuntu, the relevant packages include:

```bash
sudo apt install cups-client sane-utils
```

Configure the devices and verify that the system tools can see them:

```bash
lpstat -a
scanimage -L
```

The scan operation acquires one page and can save it as PDF, PNG, or JPEG. It uses temporary files and preserves an existing destination if acquisition fails or is cancelled.

## Building executables

PyInstaller bundles Python and the runtime dependencies into a standalone GUI executable. People running a completed bundle do not need a separate Python installation. Printer/scanner drivers and platform system libraries are still external requirements.

### Linux build

The supplied script uses a Debian 12 Python container and defaults to Podman:

```bash
bash packaging/build_linux.sh
```

To use Docker:

```bash
ENGINE=docker bash packaging/build_linux.sh
```

Output: `dist/linux/Docs-Utilz`.

### Windows build

For a native Windows build, run from Command Prompt:

```bat
packaging\build_windows.bat
```

Output: `dist\windows\Docs-Utilz.exe`.

The repository also includes a Wine/container build path from Linux:

```bash
bash packaging/build_windows.sh
```

Both container build scripts require a working container engine and network access to download their images and dependencies. Build dependencies are installed separately from the application's runtime requirements.

### Release tools

- `packaging/package_release.py` packages binaries staged under `dist/release/linux` and `dist/release/windows`, together with installers and checksums.
- `packaging/refresh_bundles.py` replaces application code in existing unsigned PyInstaller templates. It requires PyInstaller 6.x and the exact Python minor version and bytecode format used by the template. Prefer a clean build when changing dependencies or native libraries.
- The Windows installer adds shortcuts and a per-user uninstall entry. The Linux installer creates a per-user application directory and desktop entry.

Build outputs are ignored by Git. Executables must be built or supplied separately; their presence in a local `dist/` directory does not mean a GitHub release has been published.

## Development

Use an editable installation when changing the source:

```bash
python -m pip install -e .
```

Run the existing test suite from the repository root:

```bash
python -m unittest discover -s tests
```

The suite covers file-size adjustment, preservation of existing output on relevant failure paths, checked-file selection, theme behavior, print arguments, Windows device handling, and scanner compatibility. Device tests use mocks rather than sending physical print or scan jobs.

The automated suite does not replace manual GUI checks or hardware testing. When changing the desktop interface, also check previews, resizing, job progress, and error messages. When changing packaging or device code, verify on the affected operating system.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `No module named tkinter` | Install your distribution's Tkinter package or a Python build with Tcl/Tk support |
| `no display name` or a display connection error | Launch the GUI in a graphical session; use the CLI for terminal-only document processing |
| `docs-utilz` is not found | Activate the virtual environment and install the package, or use `python -m docs_utilz.cli` from the checkout |
| OpenCV cannot load a shared library | Install the OpenCV system libraries required by your distribution, including the GL/GLib libraries listed in the Linux setup |
| No watermark matches | Try a tighter sample crop, check scale differences, lower the confidence threshold gradually, or enable rotation search |
| Unrelated content is detected | Raise confidence or shape-verification thresholds and inspect a preview before processing all pages |
| Text is no longer selectable | Raster cleanup and PDF compression with flattening create image-based pages; use smart removal or text-preserving compression when suitable |
| A cleanup patch or shadow remains | Try another fill strategy; results depend on paper texture, transparency, and the content underneath |
| Compression misses its target | Increase the target or allow more destructive options; some files cannot reach a requested size with the available settings |
| Printer or scanner is missing | Check operating system device configuration, installed drivers, and CUPS/SANE or WIA availability |
| Windows scan appears to stall | Check the taskbar for a driver dialog waiting for input |
