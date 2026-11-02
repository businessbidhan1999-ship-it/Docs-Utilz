"""Windows device contracts and UI failure paths, without physical jobs."""
import ast
import math
from pathlib import Path
import queue
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

from PIL import Image
from docs_utilz import devices
from docs_utilz import tools


@patch.object(devices.sys, 'platform', 'win32')
class WindowsDeviceTests(unittest.TestCase):
    def test_discovery_uses_installed_windows_devices(self):
        result = subprocess.CompletedProcess([], 0, 'Canon scanner\nOffice scanner\n', '')
        with patch.object(devices, '_powershell', return_value=result) as run:
            self.assertEqual(devices.scanners(), ['Canon scanner', 'Office scanner'])
            self.assertIn('WIA.DeviceManager', run.call_args.args[0])
            self.assertEqual(run.call_args.kwargs['timeout'], 30)
            self.assertEqual(devices.printers(), ['Canon scanner', 'Office scanner'])
            self.assertIn('InstalledPrinters', run.call_args.args[0])

    def test_scan_selected_device_and_actual_driver_dpi(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'scan.pdf'
            def acquire(script, **values):
                self.assertEqual(values['TOOLKIT_DEVICE'], 'Canon $device')
                self.assertEqual(values['TOOLKIT_COLOR'], 'Grayscale')
                self.assertEqual(values['TOOLKIT_DPI'], '300')
                self.assertIn('ShowSelectItems', script)
                self.assertIn('ShowTransfer', script)
                Image.new('RGB', (400, 200)).save(values['TOOLKIT_SCAN'], 'BMP')
                return subprocess.CompletedProcess([], 0, 'DPI:200', '')
            with patch.object(devices, '_powershell', side_effect=acquire):
                self.assertTrue(devices.scan(str(out), 'Canon $device', color='Grayscale'))
            with tools.fitz.open(out) as doc:
                self.assertAlmostEqual(doc[0].rect.width, 144, places=2)
                self.assertAlmostEqual(doc[0].rect.height, 72, places=2)

    def test_no_image_or_failed_driver_preserves_existing_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'existing.png'
            out.write_bytes(b'original')
            with patch.object(devices, '_powershell', return_value=subprocess.CompletedProcess([], 0, '', '')):
                with self.assertRaisesRegex(RuntimeError, 'returned no image'):
                    devices.scan(str(out), 'Canon')
            self.assertEqual(out.read_bytes(), b'original')
            with patch.object(devices, '_powershell', side_effect=RuntimeError('Driver offline')):
                with self.assertRaisesRegex(RuntimeError, 'offline'):
                    devices.scan(str(out), 'Canon')
            self.assertEqual(out.read_bytes(), b'original')

    def test_connection_failure_is_not_reported_ready(self):
        with patch.object(devices, '_powershell', side_effect=RuntimeError('offline')):
            with self.assertRaisesRegex(RuntimeError, 'offline'):
                devices.scanner_status('Canon')
        with self.assertRaises(ValueError):
            devices.scanner_status('')

    def test_print_preparation_carries_layout_and_real_page_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / 'two pages.pdf'
            doc = tools.fitz.open()
            doc.new_page(width=144, height=72)
            doc.new_page(width=72, height=144)
            doc.save(src); doc.close()
            def print_dialog(script, **values):
                self.assertEqual(values['TOOLKIT_MARGIN'], '0')
                self.assertEqual(values['TOOLKIT_LANDSCAPE'], '1')
                self.assertEqual(values['TOOLKIT_SCALING'], 'Fill')
                self.assertEqual(values['TOOLKIT_DUPLEX'], 'Long edge')
                self.assertEqual(values['TOOLKIT_PRINTER'], 'Canon $printer')
                pages = sorted(Path(values['TOOLKIT_PAGES']).glob('*.png'))
                self.assertEqual(len(pages), 2)
                with Image.open(pages[0]) as image:
                    dpi = image.info['dpi'][0]
                    self.assertAlmostEqual(image.width / dpi, 2, places=2)
                    self.assertAlmostEqual(image.height / dpi, 1, places=2)
                return subprocess.CompletedProcess([], 0, 'Printing cancelled.', '')
            before = src.read_bytes()
            with patch.object(devices, '_powershell', side_effect=print_dialog):
                self.assertEqual(devices.print_file(str(src), 'Canon $printer', paper='A4',
                    orientation='Landscape', scaling='Fill', duplex='Long edge'), 'Printing cancelled.')
            self.assertEqual(src.read_bytes(), before)

    def test_invalid_settings_never_start_a_device_job(self):
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / 'image.png'
            Image.new('RGB', (10, 10)).save(src)
            with patch.object(devices, '_powershell') as run:
                for settings in ({'margin_mm': math.nan}, {'margin_mm': 51},
                                 {'paper': 'bad'}, {'scaling': 'Stretch'}, {'copies': 0}):
                    with self.assertRaises(ValueError):
                        devices.print_file(str(src), **settings)
                for settings in ({'dpi': 0}, {'color': 'bad'}, {'source': 'bad'}):
                    with self.assertRaises(ValueError):
                        devices.scan(str(src), 'Canon', **settings)
                run.assert_not_called()


class DeviceUiTests(unittest.TestCase):
    def test_scan_worker_posts_visible_failure_and_cancellation_states(self):
        # Load only the real action; the test does not require a Tk display.
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'docs_utilz' / 'app.py').read_text())
        method = next(node for cls in tree.body if isinstance(cls, ast.ClassDef)
                      for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == '_on_scan')
        namespace = {'devices': devices, 'Path': Path, 'tk': SimpleNamespace(TclError=ValueError),
                     'messagebox': Mock(), 'filedialog': Mock(), 'APP_TITLE': 'Toolkit'}
        exec(compile(ast.Module(body=[method], type_ignores=[]), 'app.py', 'exec'), namespace)
        namespace['filedialog'].asksaveasfilename.return_value = 'scan.pdf'
        var = lambda value: SimpleNamespace(get=lambda: value)
        ui = SimpleNamespace(_busy=False, scanner_name=var('Canon'), scan_dpi=var(300),
                             scan_color=var('Color'), scan_source=var('Driver default'),
                             scan_driver_settings=var(True), _device_status=Mock(),
                             _msgq=queue.Queue(), progress=Mock())
        callbacks = []
        ui._start_job = lambda label, work: callbacks.append(work)
        namespace['_on_scan'](ui)
        with patch.object(devices, 'scan', side_effect=RuntimeError('driver failure')):
            with self.assertRaisesRegex(RuntimeError, 'driver failure'):
                callbacks[0](None)
        kind, status = ui._msgq.get_nowait()
        self.assertEqual(kind, 'device_status')
        self.assertEqual(status[1], 'error')
        with patch.object(devices, 'scan', return_value=False):
            result = callbacks[0](None)
        self.assertEqual(result[1], 'Scanning cancelled.')
        self.assertIn('cancelled', ui._msgq.get_nowait()[1][2])
        namespace['filedialog'].asksaveasfilename.reset_mock()
        ui.scan_dpi = var('invalid')
        namespace['_on_scan'](ui)
        namespace['messagebox'].showwarning.assert_called_once()
        namespace['filedialog'].asksaveasfilename.assert_not_called()


if __name__ == '__main__':
    unittest.main()
