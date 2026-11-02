"""Checks that do not need a desktop display."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest

from docs_utilz.ui_theme import PALETTES


def contrast(first, second):
    def luminance(color):
        channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        channels = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in channels]
        return sum(channel * weight for channel, weight in zip(channels, (.2126, .7152, .0722)))
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + .05) / (low + .05)


class ThemeChecks(unittest.TestCase):
    def test_readable_text_and_controls_in_both_themes(self):
        for name, colors in PALETTES.items():
            with self.subTest(theme=name):
                for text, background in (('text', 'background'), ('text', 'surface'),
                                         ('muted', 'background'), ('rail_text', 'rail_hover'),
                                         ('rail_muted', 'rail'), ('selected_text', 'selection')):
                    self.assertGreaterEqual(contrast(colors[text], colors[background]), 4.5)
                primary_text = '#FFFFFF' if name == 'Paper' else '#172B1D'
                self.assertGreaterEqual(contrast(primary_text, colors['accent']), 4.5)
                self.assertGreaterEqual(contrast(colors['border'], colors['surface']), 3)

    def test_keyboard_focus_scrolls_to_an_offscreen_control(self):
        source = (Path(__file__).resolve().parents[1] / 'docs_utilz' / 'app.py').read_text()
        klass = next(node for node in ast.parse(source).body if isinstance(node, ast.ClassDef) and node.name == 'App')
        method = next(node for node in klass.body if isinstance(node, ast.FunctionDef) and node.name == '_reveal_focus')
        namespace = {}
        exec(compile(ast.Module(body=[method], type_ignores=[]), 'app.py', 'exec'), namespace)
        moved = []
        inner = SimpleNamespace(winfo_rooty=lambda: 0, winfo_rootx=lambda: 0)
        canvas = SimpleNamespace(winfo_ismapped=lambda: True, update_idletasks=lambda: None,
            bbox=lambda _: (0, 0, 400, 800), canvasy=lambda _: 0, canvasx=lambda _: 0,
            winfo_height=lambda: 200, winfo_width=lambda: 400,
            yview_moveto=lambda position: moved.append(position))
        widget = SimpleNamespace(master=inner, winfo_rooty=lambda: 500, winfo_height=lambda: 30,
                                 winfo_rootx=lambda: 0, winfo_width=lambda: 100)
        namespace['_reveal_focus'](SimpleNamespace(_scroll_regions=[(canvas, inner)]), SimpleNamespace(widget=widget))
        self.assertEqual(moved, [.425])


if __name__ == '__main__':
    unittest.main()
