"""Generated documents and mocked OS bridges; no physical device jobs."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import subprocess
from types import SimpleNamespace
import ast

from PIL import Image
from docs_utilz import devices
from docs_utilz import tools


class SizeTests(unittest.TestCase):
    def test_increase_preserves_pixels_and_selectable_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for extension in ('pdf', 'png', 'jpg'):
                with self.subTest(extension=extension):
                    src, dest = root / ('in.' + extension), root / ('out.' + extension)
                    if extension == 'pdf':
                        with tools.fitz.open() as doc:
                            doc.new_page().insert_text((72, 72), 'Selectable text')
                            doc.save(src)
                    else:
                        Image.new('RGBA' if extension == 'png' else 'RGB', (100, 80),
                                  (40, 90, 130, 100) if extension == 'png' else (40, 90, 130)).save(src)
                    before = src.read_bytes()
                    result = tools.resize_file(str(src), str(dest), 100)
                    self.assertTrue(result.reached)
                    self.assertEqual(dest.stat().st_size, 100 * 1024)
                    self.assertEqual(src.read_bytes(), before)
                    if extension == 'pdf':
                        with tools.fitz.open(dest) as doc:
                            self.assertIn('Selectable text', doc[0].get_text())
                            self.assertFalse(doc.is_repaired)
                    else:
                        with Image.open(src) as original, Image.open(dest) as resized:
                            self.assertEqual(original.tobytes(), resized.tobytes())

    def test_decrease_all_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = Image.effect_noise((900, 900), 80).convert('RGB')
            for extension in ('jpg', 'png', 'pdf'):
                with self.subTest(extension=extension):
                    src, dest = root / ('large.' + extension), root / ('small.' + extension)
                    if extension == 'pdf':
                        image.save(root / 'page.png')
                        tools.images_to_pdf([str(root / 'page.png')], str(src))
                    else:
                        image.save(src)
                    before = src.read_bytes()
                    result = tools.resize_file(str(src), str(dest), 100, False)
                    self.assertTrue(result.reached)
                    self.assertEqual(Path(result.path).stat().st_size, 102400)
                    self.assertEqual(src.read_bytes(), before)
                    with tools.fitz.open(result.path) as doc:
                        self.assertEqual(doc.page_count, 1)
                        if extension == 'pdf':
                            self.assertFalse(doc.is_repaired)

    def test_reject_same_path_and_preserve_output_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            src, dest = Path(directory) / 'in.png', Path(directory) / 'out.png'
            Image.new('RGB', (10, 10)).save(src)
            with self.assertRaises(ValueError):
                tools.resize_file(str(src), str(src), 100)
            dest.write_bytes(b'existing output')
            with patch.object(tools, '_pad_media', side_effect=ValueError('failed')):
                with self.assertRaises(ValueError):
                    tools.resize_file(str(src), str(dest), 100)
            self.assertEqual(dest.read_bytes(), b'existing output')

    def test_unreachable_target_reports_best_effort(self):
        with tempfile.TemporaryDirectory() as directory:
            src, dest = Path(directory) / 'in.pdf', Path(directory) / 'out.pdf'
            with tools.fitz.open() as doc:
                page = doc.new_page()
                for line in range(100):
                    page.insert_text((10, 10 + line * 7), f'Unique selectable content {line}')
                doc.save(src)
            result = tools.resize_file(str(src), str(dest), 1, True)
            self.assertFalse(result.reached)
            self.assertGreater(dest.stat().st_size, 1024)
            with tools.fitz.open(dest) as doc:
                self.assertIn('Unique selectable', doc[0].get_text())


class FileSelectionTests(unittest.TestCase):
    def test_only_checked_files_processed_without_overwriting_previous_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chosen, excluded = root / 'chosen.png', root / 'excluded.png'
            Image.new('RGB', (20, 20)).save(chosen)
            Image.new('RGB', (20, 20)).save(excluded)
            existing = root / 'chosen_sized.png'
            existing.write_bytes(b'previous result')
            variable = lambda value: SimpleNamespace(get=lambda: value)
            jobs = []
            harness = SimpleNamespace(
                _busy=False, cmp_files={'one': (str(chosen), True), 'two': (str(excluded), False)},
                target_kb=variable(100), cmp_outdir=variable(''),
                size_mode=variable('Set exact file size'), cmp_keep_text=variable(True),
                cmp_allow_jpeg=variable(True),
                _start_job=lambda label, work: jobs.append(work))
            # Load just the job starter so selection can be checked without
            # a Tk display or Pillow's optional ImageTk system package.
            parsed = ast.parse((Path(__file__).resolve().parents[1] / 'docs_utilz' / 'app.py').read_text())
            app_class = next(node for node in parsed.body if isinstance(node, ast.ClassDef) and node.name == 'App')
            method = next(node for node in app_class.body if isinstance(node, ast.FunctionDef) and node.name == '_on_compress')
            namespace = {'Path': Path, 'tools': tools, 'tk': SimpleNamespace(TclError=RuntimeError)}
            exec(compile(ast.Module(body=[method], type_ignores=[]), 'app.py', 'exec'), namespace)
            namespace['_on_compress'](harness)
            self.assertEqual(len(jobs), 1)
            result = jobs[0](lambda fraction, message: None)
            self.assertEqual(len(result[2]), 1)
            self.assertEqual(existing.read_bytes(), b'previous result')
            self.assertEqual((root / 'chosen_sized_1.png').stat().st_size, 102400)
            self.assertFalse((root / 'excluded_sized.png').exists())


class DeviceTests(unittest.TestCase):
    @patch.object(devices.sys, 'platform', 'linux')
    def test_print_paths_are_arguments_and_copies_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / 'name with $special.png'
            Image.new('RGB', (10, 10)).save(src)
            with patch.object(devices, '_run', return_value=subprocess.CompletedProcess([], 0, 'request id 1', '')) as run:
                devices.print_file(str(src), 'Office', 2)
                args = run.call_args.args[0]
                self.assertEqual(args[:3], ['lp', '-n', '2'])
                self.assertIn('print-scaling=fit', args)
                self.assertIn('media=A4', args)
                self.assertIn('page-left=0.00', args)
                self.assertEqual(args[-4:], ['-d', 'Office', '--', str(src.resolve())])
                with self.assertRaises(ValueError):
                    devices.print_file(str(src), copies=0)
                self.assertEqual(run.call_count, 1)

    @patch.object(devices.sys, 'platform', 'linux')
    def test_scan_conversion_and_failure_preserve_destination(self):
        buf = io.BytesIO()
        Image.new('RGB', (40, 30), 'white').save(buf, 'PNG')
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / 'scan.pdf'
            with patch.object(devices, '_run', return_value=subprocess.CompletedProcess([], 0, buf.getvalue(), b'')):
                self.assertTrue(devices.scan(str(dest), 'scanner-id', 300))
            with tools.fitz.open(dest) as doc:
                self.assertEqual(doc.page_count, 1)
            before = dest.read_bytes()
            with patch.object(devices, '_run', side_effect=RuntimeError('offline')):
                with self.assertRaises(RuntimeError):
                    devices.scan(str(dest))
            self.assertEqual(dest.read_bytes(), before)

    @patch.object(devices.sys, 'platform', 'linux')
    def test_device_discovery(self):
        with patch.object(devices, '_run', return_value=subprocess.CompletedProcess([], 0, 'Office\nFrontDesk\n', '')):
            self.assertEqual(devices.printers(), ['Office', 'FrontDesk'])
        with patch.object(devices, '_run', return_value=subprocess.CompletedProcess([], 0, 'scanner:usb001\n', '')):
            self.assertEqual(devices.scanners(), ['scanner:usb001'])

    def test_missing_driver_tools_explain_recovery(self):
        with patch.object(devices.subprocess, 'run', side_effect=FileNotFoundError):
            with self.assertRaisesRegex(RuntimeError, 'Install the system printer/scanner tools'):
                devices._run(['scanimage'])

    @patch.object(devices.sys, 'platform', 'win32')
    def test_cancelled_scan_does_not_replace_file(self):
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / 'scan.png'
            dest.write_bytes(b'keep this')
            with patch.object(devices, '_powershell', return_value=subprocess.CompletedProcess([], 0, 'CANCELLED', '')):
                self.assertFalse(devices.scan(str(dest), "Canon scanner"))
            self.assertEqual(dest.read_bytes(), b'keep this')


if __name__ == '__main__':
    unittest.main()
