"""Local OS printer/scanner bridges; no Python driver packages required."""
from __future__ import annotations

import base64
import json
import xml.etree.ElementTree as ET
import os
import re
import math
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile

from PIL import Image
from . import tools


def _device_error(error) -> str:
    """Decode Windows PowerShell CLIXML error records, excluding progress noise."""
    if isinstance(error, bytes):
        error = error.decode('utf-8', errors='replace')
    error = (error or '').strip()
    if error.startswith('#< CLIXML'):
        try:
            root = ET.fromstring(error[error.index('<Objs'):])
            records = []
            for node in root.iter():
                if node.get('S', '').lower() != 'error':
                    continue
                if node.tag.rsplit('}', 1)[-1] == 'S':
                    records.append(node.text or '')
                else:
                    description = next((child.text for child in node.iter()
                                        if child.tag.rsplit('}', 1)[-1] == 'ToString'), None)
                    if description:
                        records.append(description)
            if records:
                # Decode once: _x005F_x000D_ represents a literal _x000D_, not a newline.
                return re.sub(r'_x([0-9a-fA-F]{4})_', lambda match: chr(int(match[1], 16)),
                              ''.join(records)).replace('\r\n', '\n').strip()
            return 'PowerShell reported a device failure without an error message. Check the driver and retry.'
        except (ET.ParseError, ValueError):
            return 'PowerShell returned an unreadable device error. Check the driver and retry.'
    return error or 'The device operation failed. Check the connection and driver.'


def _run(args, **kwargs):
    try:
        result = subprocess.run(args, check=False, capture_output=True, **kwargs)
    except subprocess.TimeoutExpired:
        raise RuntimeError('The device did not respond in time. Check its power, connection and driver, then retry.') from None
    except FileNotFoundError:
        raise RuntimeError(f"{args[0]} is unavailable. Install the system printer/scanner tools and drivers.") from None
    if result.returncode:
        raise RuntimeError(_device_error(result.stderr))
    return result


def _powershell(script: str, *, timeout=None, **values):
    env = os.environ.copy()
    env.update(values)
    script = ("$ErrorActionPreference = 'Stop'\n$ProgressPreference = 'SilentlyContinue'\n"
              "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\ntry {\n" + script +
              "\n} catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }\n")
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    return _run(['powershell.exe', '-NoProfile', '-STA', '-OutputFormat', 'Text', '-EncodedCommand', encoded],
                env=env, text=True, encoding='utf-8', timeout=timeout, creationflags=0x08000000)


# Device identifiers/names travel through environment variables, never script interpolation.
_WIA_CONNECT = r"""
$manager = New-Object -ComObject WIA.DeviceManager
$matches = @($manager.DeviceInfos | Where-Object {
  $_.Type -eq 1 -and ($_.DeviceID -eq $env:TOOLKIT_DEVICE -or
    $_.Properties.Item('Name').Value -eq $env:TOOLKIT_DEVICE)
})
if ($matches.Count -eq 0) {
  throw 'Scanner not found. Switch it on, install its WIA driver, then refresh devices. For a TWAIN-only driver, install the manufacturer WIA driver.'
}
if ($matches.Count -gt 1) { throw 'More than one scanner has this name. Enter its unique WIA DeviceID.' }
$device = $matches[0].Connect()
$dialog = New-Object -ComObject WIA.CommonDialog
"""


# WIA 2 drivers identify sources by scan-item category, not legacy device property 3088.
_WIA_FLATBED = 'fb607b1f-43f3-488b-855b-fb703ec342a6'
_WIA_FEEDER = 'fe131934-f84c-42ad-8da4-6129cddd7288'

_WIA_CHOOSE_ITEMS = r"""
try { $items = $dialog.ShowSelectItems($device, $intent, 0, $true, $false, $false) }
catch { $items = $dialog.ShowSelectItems($device, $intent, 0, $true, $true, $false) }
if ($null -eq $items -or $items.Count -eq 0) { Write-Output 'CANCELLED'; exit 0 }
$item = $items.Item(1)
"""

_WIA_CAPABILITIES = r"""
function Get-ScanItems($parent, $path) {
  for ($i = 1; $i -le $parent.Items.Count; $i++) {
    $item = $parent.Items.Item($i)
    [int[]]$itemPath = @($path | Where-Object { $null -ne $_ }) + @($i)
    $category = @($item.Properties | Where-Object { $_.PropertyID -eq 4125 })
    $categoryValue = if ($category.Count -gt 0) { [string]$category[0].Value } else { '' }
    if (($item.ItemType -band 8192) -ne 0) {
      [PSCustomObject]@{ path = $itemPath; category = $categoryValue }
    }
    if ($item.Items.Count -gt 0) { Get-ScanItems $item $itemPath }
  }
}
$items = @(Get-ScanItems $device @())
$legacySource = @($device.Properties | Where-Object { $_.PropertyID -eq 3088 }).Count -gt 0
[PSCustomObject]@{ items = $items; legacySource = $legacySource } | ConvertTo-Json -Depth 8 -Compress
"""


def _wia_source_plan(capabilities: dict, source: str) -> tuple[list[int], str]:
    """Choose a WIA 2 source item, or an exposed WIA 1 source property."""
    items = capabilities.get('items', [])
    if not items:
        raise RuntimeError('The driver exposes no transferable scan items. Enable driver acquisition settings.')
    if source == 'Driver default':
        return items[0]['path'], ''
    category = _WIA_FEEDER if source == 'Feeder' else _WIA_FLATBED
    selected = next((item for item in items
                     if item.get('category', '').strip('{}').lower() == category), None)
    if selected:
        return selected['path'], ''
    # Only legacy items without WIA 2 categories use the deprecated source flags.
    if capabilities.get('legacySource') and not any(item.get('category') for item in items):
        return items[0]['path'], '1' if source == 'Feeder' else '2'
    raise RuntimeError(f'{source} is not exposed by this scanner. Choose Driver default or enable driver acquisition settings to select a supported source.')


def scanners() -> list[str]:
    if sys.platform.startswith('win'):
        result = _powershell(r"""
$manager = New-Object -ComObject WIA.DeviceManager
foreach ($info in $manager.DeviceInfos) {
  if ($info.Type -eq 1) { Write-Output $info.Properties.Item('Name').Value }
}
""", timeout=30)
    else:
        result = _run(['scanimage', '-f', '%d%n'], text=True, timeout=30)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def printers() -> list[str]:
    if sys.platform.startswith('win'):
        result = _powershell("Add-Type -AssemblyName System.Drawing\n[System.Drawing.Printing.PrinterSettings]::InstalledPrinters", timeout=30)
    else:
        result = _run(['lpstat', '-e'], text=True, timeout=30)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def scanner_status(device: str) -> str:
    if not device.strip():
        raise ValueError('Select or enter a scanner first.')
    if sys.platform.startswith('win'):
        _powershell(_WIA_CONNECT + "\nWrite-Output 'CONNECTED'", TOOLKIT_DEVICE=device, timeout=45)
        return 'Driver connected — ready to request a scan'
    # Opening the device for its options checks the backend without acquiring a page.
    _run(['scanimage', '-d', device, '--help'], text=True, timeout=30)
    return 'Backend connected — ready to request a scan'


def scanner_settings(device: str) -> str:
    if not device.strip():
        raise ValueError('Select or enter a scanner first.')
    if sys.platform.startswith('win'):
        result = _powershell(_WIA_CONNECT + "$intent = 0\n" + _WIA_CHOOSE_ITEMS,
                             TOOLKIT_DEVICE=device)
        return ('Scanner settings cancelled.' if 'CANCELLED' in result.stdout else
                'Acquisition settings closed. Review settings again when scanning.')
    return _run(['scanimage', '-d', device, '--help'], text=True, timeout=30).stdout


def open_device_setup() -> None:
    """Let the operating system install drivers and add USB/network devices."""
    if sys.platform.startswith('win'):
        os.startfile('ms-settings:printers')
    elif shutil.which('system-config-printer'):
        subprocess.Popen(['system-config-printer'])
    else:
        subprocess.Popen(['xdg-open', 'http://localhost:631'])


def scan(out_path: str, device: str = '', dpi: int = 300, *,
         color: str = 'Color', source: str = 'Driver default', driver_settings: bool = True) -> bool:
    """Acquire one page using the selected driver; never replace a failed/cancelled scan."""
    if not 75 <= dpi <= 1200 or color not in {'Color', 'Grayscale', 'Black & white'}:
        raise ValueError('Choose 75–1200 DPI and a supported color mode.')
    if source not in {'Driver default', 'Flatbed', 'Feeder'}:
        raise ValueError('Choose Driver default, Flatbed or Feeder.')
    dest = Path(out_path)
    if dest.suffix.lower() not in {'.pdf', '.png', '.jpg', '.jpeg'}:
        raise ValueError('Save a scan as PDF, PNG or JPEG.')
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent) as scratch:
        raw = Path(scratch) / 'scan.bmp'
        if sys.platform.startswith('win'):
            if not device.strip():
                raise ValueError('Select or enter a scanner first, then check its connection.')
            item_path, legacy_source = [], ''
            if not driver_settings:
                capabilities = _powershell(_WIA_CONNECT + _WIA_CAPABILITIES,
                                           TOOLKIT_DEVICE=device, timeout=45)
                item_path, legacy_source = _wia_source_plan(json.loads(capabilities.stdout), source)
            # Driver UI owns source/DPI selection. Never apply legacy source properties before it.
            result = _powershell(_WIA_CONNECT + _WINDOWS_SCAN,
                 TOOLKIT_SCAN=str(raw.resolve()), TOOLKIT_DEVICE=device, TOOLKIT_DPI=str(dpi),
                 TOOLKIT_COLOR=color, TOOLKIT_ITEM='/'.join(map(str, item_path)),
                 TOOLKIT_LEGACY_SOURCE=legacy_source, TOOLKIT_SETTINGS='1' if driver_settings else '0')
            if 'CANCELLED' in result.stdout:
                return False
            actual = re.search(r'DPI:(\d+)', result.stdout)
            if actual:
                dpi = int(actual.group(1))
            if not raw.is_file():
                raise RuntimeError('The scanner returned no image. Check its driver and paper source.')
        else:
            args = ['scanimage', '--format=png', '--resolution', str(dpi),
                    '--mode', {'Color': 'Color', 'Grayscale': 'Gray', 'Black & white': 'Lineart'}[color]]
            if source != 'Driver default':
                args += ['--source', 'ADF' if source == 'Feeder' else 'Flatbed']
            if device:
                args += ['-d', device]
            result = _run(args, timeout=300)
            raw.write_bytes(result.stdout)
        converted = Path(scratch) / dest.name
        if dest.suffix.lower() == '.pdf':
            tools.images_to_pdf([str(raw)], str(converted), dpi=dpi)
        else:
            with Image.open(raw) as image:
                image.convert('RGB').save(converted, dpi=(dpi, dpi))
        os.replace(converted, dest)
    return True


_WINDOWS_SCAN = r"""
function Set-ScanProperty($item, $id, $value) {
  $property = @($item.Properties | Where-Object { $_.PropertyID -eq $id })
  if ($property.Count -eq 0) { throw "Scanner driver does not support setting $id. Enable driver acquisition settings." }
  $property[0].Value = $value
}
$intent = switch ($env:TOOLKIT_COLOR) { 'Color' { 1 }; 'Grayscale' { 2 }; 'Black & white' { 4 } }
if ($env:TOOLKIT_SETTINGS -eq '1') {
""" + _WIA_CHOOSE_ITEMS + r"""
} else {
  if ($env:TOOLKIT_LEGACY_SOURCE) {
    Set-ScanProperty $device 3088 ([int]$env:TOOLKIT_LEGACY_SOURCE)
  }
  $item = $device
  foreach ($index in $env:TOOLKIT_ITEM.Split('/')) {
    $item = $item.Items.Item([int]$index)
  }
  Set-ScanProperty $item 6146 $intent
  Set-ScanProperty $item 6147 ([int]$env:TOOLKIT_DPI)
  Set-ScanProperty $item 6148 ([int]$env:TOOLKIT_DPI)
}
$image = $dialog.ShowTransfer($item, '{00000000-0000-0000-0000-000000000000}', $false)
if ($null -eq $image) { Write-Output 'CANCELLED'; exit 0 }
$image.SaveFile($env:TOOLKIT_SCAN)
$actualDpi = [int]$image.HorizontalResolution
if ($actualDpi -le 0) { $actualDpi = [int]$env:TOOLKIT_DPI }
Write-Output "DPI:$actualDpi"
"""


def print_file(path: str, printer: str = '', copies: int = 1, *,
               paper: str = 'A4', orientation: str = 'Portrait', scaling: str = 'Fit',
               margin_mm: float = 0, duplex: str = 'Off', review: bool = True) -> str:
    """Review layout before submitting; respect physical printable bounds."""
    src = Path(path).resolve()
    if not src.is_file() or not tools.kind_of(str(src)):
        raise ValueError('Choose an existing PDF, JPEG or PNG file to print.')
    if not 1 <= copies <= 999:
        raise ValueError('Choose between 1 and 999 copies.')
    if paper not in {'A4', 'Letter', 'Legal', 'Printer default'} or orientation not in {'Portrait', 'Landscape'}:
        raise ValueError('Choose a supported paper size and orientation.')
    if scaling not in {'Fit', 'Fill', 'Actual size'} or duplex not in {'Off', 'Long edge', 'Short edge'}:
        raise ValueError('Choose a supported scaling and duplex setting.')
    if not math.isfinite(margin_mm) or not 0 <= margin_mm <= 50:
        raise ValueError('Margins must be between 0 and 50 mm.')
    if not sys.platform.startswith('win'):
        args = ['lp', '-n', str(copies), '-o', 'print-scaling=' + {'Fit': 'fit', 'Fill': 'fill', 'Actual size': 'none'}[scaling],
                '-o', 'orientation-requested=' + ('4' if orientation == 'Landscape' else '3'),
                '-o', 'sides=' + {'Off': 'one-sided', 'Long edge': 'two-sided-long-edge', 'Short edge': 'two-sided-short-edge'}[duplex]]
        if paper != 'Printer default':
            args += ['-o', 'media=' + paper]
        for side in ('left', 'right', 'top', 'bottom'):
            args += ['-o', f'page-{side}={margin_mm * 72 / 25.4:.2f}']
        if printer.strip():
            args += ['-d', printer.strip()]
        result = _run(args + ['--', str(src)], text=True, timeout=60)
        return result.stdout.strip() or 'Print job submitted.'
    with tempfile.TemporaryDirectory() as scratch:
        if tools.kind_of(str(src)) == 'pdf':
            pages = tools.pdf_to_images(str(src), scratch, dpi=300, fmt='png')
            # Converter PNGs have no resolution metadata; actual-size printing needs it.
            for page in pages:
                with Image.open(page) as image:
                    pixels = image.copy()
                pixels.save(page, dpi=(300, 300))
                pixels.close()
        else:
            with Image.open(src) as image:
                image.convert('RGB').save(Path(scratch) / 'page.png', dpi=image.info.get('dpi', (300, 300)))
        result = _powershell(_WINDOWS_PRINT, TOOLKIT_PAGES=scratch, TOOLKIT_TITLE=src.name,
                             TOOLKIT_COPIES=str(copies), TOOLKIT_PRINTER=printer,
                             TOOLKIT_PAPER=paper, TOOLKIT_LANDSCAPE='1' if orientation == 'Landscape' else '0',
                             TOOLKIT_SCALING=scaling, TOOLKIT_MARGIN=str(margin_mm), TOOLKIT_DUPLEX=duplex,
                             TOOLKIT_REVIEW='1' if review else '0')
        return result.stdout.strip()


_WINDOWS_PRINT = r"""
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
$script:pages = @(Get-ChildItem -LiteralPath $env:TOOLKIT_PAGES -Filter '*.png' | Sort-Object Name)
$script:index = 0
$doc = New-Object System.Drawing.Printing.PrintDocument
try {
  $doc.DocumentName = $env:TOOLKIT_TITLE
  if ($env:TOOLKIT_PRINTER) { $doc.PrinterSettings.PrinterName = $env:TOOLKIT_PRINTER }
  if (-not $doc.PrinterSettings.IsValid) { throw 'Printer unavailable. Add it in Device setup or choose an installed printer.' }
  $doc.PrinterSettings.Copies = [int16]$env:TOOLKIT_COPIES
  $doc.DefaultPageSettings.Landscape = $env:TOOLKIT_LANDSCAPE -eq '1'
  if ($env:TOOLKIT_PAPER -ne 'Printer default') {
    $size = @($doc.PrinterSettings.PaperSizes | Where-Object { $_.Kind.ToString() -eq $env:TOOLKIT_PAPER })
    if ($size.Count -eq 0) { throw 'Selected paper is not supported by this printer. Choose Printer default or use its driver settings.' }
    $doc.DefaultPageSettings.PaperSize = $size[0]
  }
  $margin = [int]([double]::Parse($env:TOOLKIT_MARGIN, [Globalization.CultureInfo]::InvariantCulture) * 100 / 25.4)
  $doc.DefaultPageSettings.Margins = New-Object System.Drawing.Printing.Margins($margin, $margin, $margin, $margin)
  if ($env:TOOLKIT_DUPLEX -ne 'Off' -and -not $doc.PrinterSettings.CanDuplex) { throw 'This printer does not support two-sided printing.' }
  $doc.PrinterSettings.Duplex = switch ($env:TOOLKIT_DUPLEX) {
    'Long edge' {
      if ($doc.DefaultPageSettings.Landscape) { [System.Drawing.Printing.Duplex]::Horizontal }
      else { [System.Drawing.Printing.Duplex]::Vertical }
    }
    'Short edge' {
      if ($doc.DefaultPageSettings.Landscape) { [System.Drawing.Printing.Duplex]::Vertical }
      else { [System.Drawing.Printing.Duplex]::Horizontal }
    }
    default { [System.Drawing.Printing.Duplex]::Simplex }
  }
  $doc.OriginAtMargins = $false
  # Preview and the real job each start at page one.
  $doc.add_BeginPrint({
    param($sender, $e)
    $script:index = 0
    $script:previewing = $e.PrintAction -eq [System.Drawing.Printing.PrintAction]::PrintToPreview
  })
  $doc.add_PrintPage({
    param($sender, $e)
    $image = [System.Drawing.Image]::FromFile($script:pages[$script:index].FullName)
    try {
      # Intersect requested margins with the driver printable area; do not add one-inch defaults.
      $bounds = [System.Drawing.RectangleF]::new($e.MarginBounds.X, $e.MarginBounds.Y, $e.MarginBounds.Width, $e.MarginBounds.Height)
      $box = [System.Drawing.RectangleF]::Intersect($bounds, $e.PageSettings.PrintableArea)
      if ($box.Width -le 0 -or $box.Height -le 0) { throw 'Margins leave no printable area.' }
      $w = $image.Width * 100 / $image.HorizontalResolution
      $h = $image.Height * 100 / $image.VerticalResolution
      $scale = switch ($env:TOOLKIT_SCALING) {
        'Fill' { [Math]::Max($box.Width / $w, $box.Height / $h) }
        'Actual size' { 1.0 }
        default { [Math]::Min($box.Width / $w, $box.Height / $h) }
      }
      $w *= $scale; $h *= $scale
      $x = $box.X + ($box.Width - $w) / 2
      $y = $box.Y + ($box.Height - $h) / 2
      $state = $e.Graphics.Save()
      try {
        # Graphics origin is the hardware printable corner, whereas box is physical-page based.
        if (-not $script:previewing) {
          $e.Graphics.TranslateTransform(-$e.PageSettings.HardMarginX, -$e.PageSettings.HardMarginY)
        }
        $e.Graphics.SetClip($box)
        $e.Graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $e.Graphics.DrawImage($image, [System.Drawing.RectangleF]::new($x, $y, $w, $h))
      } finally { $e.Graphics.Restore($state) }
    } finally { $image.Dispose() }
    $script:index++
    $e.HasMorePages = $script:index -lt $script:pages.Count
  })
  $dialog = New-Object System.Windows.Forms.PrintDialog
  try {
    $dialog.Document = $doc
    $dialog.UseEXDialog = $true
    if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { Write-Output 'Printing cancelled.'; exit 0 }
  } finally { $dialog.Dispose() }
  if ($env:TOOLKIT_REVIEW -eq '1') {
    $setup = New-Object System.Windows.Forms.PageSetupDialog
    try {
      $setup.Document = $doc; $setup.EnableMetric = $true
      if ($setup.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { Write-Output 'Printing cancelled.'; exit 0 }
    } finally { $setup.Dispose() }
    # The native preview toolbar prints on demand. Never print again on closing it.
    $preview = New-Object System.Windows.Forms.PrintPreviewDialog
    try {
      $preview.Document = $doc
      $preview.Width = 1000; $preview.Height = 750
      $preview.ShowDialog() | Out-Null
      Write-Output 'Preview closed. Jobs are submitted only when you click the preview toolbar Print button.'
    } finally { $preview.Dispose() }
  } else {
    $doc.Print(); Write-Output 'Print job submitted.'
  }
} finally { $doc.Dispose() }
"""
