"""Regressions for the reported CLIXML/unsupported-3088 scan failure."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from docs_utilz import devices


CLIXML = '''#< CLIXML
<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">
<Obj S="progress"><MS><AV>Preparing modules for first use.</AV></MS></Obj>
<S S="Error">Scanner driver does not support setting 3088. Use driver defaults/settings._x000D__x000A_</S>
<S S="Error">At line:18 char:32_x000D__x000A_</S></Objs>'''


class PowerShellErrorTests(unittest.TestCase):
    def test_reported_xml_error_is_readable_and_excludes_progress(self):
        with patch.object(devices.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', CLIXML)):
            with self.assertRaises(RuntimeError) as raised:
                devices._run(['powershell.exe'])
        error = str(raised.exception)
        self.assertEqual(error, 'Scanner driver does not support setting 3088. Use driver defaults/settings.\nAt line:18 char:32')
        self.assertNotIn('CLIXML', error)
        self.assertNotIn('Preparing modules', error)

    def test_xml_escaping_plain_errors_and_malformed_xml(self):
        xml = '<Objs><S S="Error">Canon &amp; office _x005F_x000D_</S></Objs>'
        self.assertEqual(devices._device_error('#< CLIXML\n' + xml), 'Canon & office _x000D_')
        self.assertEqual(devices._device_error(b'Paper jam'), 'Paper jam')
        self.assertIn('unreadable device error', devices._device_error('#< CLIXML\n<Objs>'))
        self.assertIn('without an error message', devices._device_error('#< CLIXML\n<Objs/>'))


class ScanSourceTests(unittest.TestCase):
    def test_wia2_flatbed_and_nested_feeder_without_legacy_property(self):
        capabilities = dict(legacySource=False, items=[
            dict(path=[1], category='{' + devices._WIA_FLATBED.upper() + '}'),
            dict(path=[2, 1], category=devices._WIA_FEEDER)])
        self.assertEqual(devices._wia_source_plan(capabilities, 'Flatbed'), ([1], ''))
        self.assertEqual(devices._wia_source_plan(capabilities, 'Feeder'), ([2, 1], ''))
        self.assertEqual(devices._wia_source_plan(capabilities, 'Driver default'), ([1], ''))

    def test_legacy_source_is_used_only_when_exposed(self):
        capabilities = dict(legacySource=True, items=[dict(path=[1], category='')])
        self.assertEqual(devices._wia_source_plan(capabilities, 'Flatbed'), ([1], '2'))
        self.assertEqual(devices._wia_source_plan(capabilities, 'Feeder'), ([1], '1'))
        capabilities['legacySource'] = False
        with self.assertRaisesRegex(RuntimeError, 'Choose Driver default'):
            devices._wia_source_plan(capabilities, 'Flatbed')
        self.assertEqual(devices._wia_source_plan(capabilities, 'Driver default'), ([1], ''))
        with self.assertRaisesRegex(RuntimeError, 'no transferable'):
            devices._wia_source_plan({'items': []}, 'Driver default')

    @patch.object(devices.sys, 'platform', 'win32')
    def test_driver_dialog_does_not_apply_form_source_3088(self):
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / 'scan.png'
            def acquire(script, **values):
                # This driver's 3088 setter would fail; the real scan path must never request it.
                self.assertEqual(values['TOOLKIT_LEGACY_SOURCE'], '')
                self.assertEqual(values['TOOLKIT_ITEM'], '')
                self.assertEqual(values['TOOLKIT_SETTINGS'], '1')
                Image.new('RGB', (20, 20)).save(values['TOOLKIT_SCAN'], 'BMP')
                return subprocess.CompletedProcess([], 0, 'DPI:300', '')
            with patch.object(devices, '_powershell', side_effect=acquire) as run:
                self.assertTrue(devices.scan(str(dest), 'Canon', source='Flatbed', driver_settings=True))
                self.assertEqual(run.call_count, 1)
            with Image.open(dest) as image:
                self.assertEqual(image.size, (20, 20))

    @patch.object(devices.sys, 'platform', 'win32')
    def test_manual_wia2_source_uses_scan_item_without_3088(self):
        capabilities = dict(legacySource=False, items=[dict(path=[2, 1], category=devices._WIA_FLATBED)])
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / 'scan.png'
            def acquire(script, **values):
                if 'TOOLKIT_SCAN' not in values:
                    return subprocess.CompletedProcess([], 0, json.dumps(capabilities), '')
                self.assertEqual(values['TOOLKIT_ITEM'], '2/1')
                self.assertEqual(values['TOOLKIT_LEGACY_SOURCE'], '')
                self.assertEqual(values['TOOLKIT_SETTINGS'], '0')
                Image.new('RGB', (20, 20)).save(values['TOOLKIT_SCAN'], 'BMP')
                return subprocess.CompletedProcess([], 0, 'DPI:300', '')
            with patch.object(devices, '_powershell', side_effect=acquire) as run:
                self.assertTrue(devices.scan(str(dest), 'Canon', source='Flatbed', driver_settings=False))
                self.assertEqual(run.call_count, 2)

    @patch.object(devices.sys, 'platform', 'win32')
    def test_settings_uses_acquisition_ui_and_handles_cancel(self):
        with patch.object(devices, '_powershell', return_value=subprocess.CompletedProcess([], 0, 'CANCELLED', '')) as run:
            self.assertEqual(devices.scanner_settings('Canon'), 'Scanner settings cancelled.')
            self.assertIn('ShowSelectItems', run.call_args.args[0])
            self.assertNotIn('ShowDeviceProperties', run.call_args.args[0])
            self.assertNotIn('ShowTransfer', run.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
