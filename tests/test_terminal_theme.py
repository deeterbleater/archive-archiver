import unittest

import terminal_theme


class TerminalThemeTests(unittest.TestCase):
    def test_highlight_markup_uses_pond_scum_green(self):
        rendered = terminal_theme.render_markup("[highlight]alge[/highlight]")

        self.assertIn("alge", rendered)
        self.assertEqual(terminal_theme.THEME.styles["highlight"].color.triplet, (111, 143, 31))

    def test_invalid_markup_is_escaped(self):
        rendered = terminal_theme.render_markup("[not-a-real-tag]alge")

        self.assertIn("alge", rendered)

    def test_logo_gradient_runs_from_light_to_dark_green(self):
        self.assertEqual(terminal_theme.LOGO_GRADIENT[0], "#c8ff7a")
        self.assertEqual(terminal_theme.LOGO_GRADIENT[-1], "#263f0a")
        self.assertEqual(len(terminal_theme.LOGO_GRADIENT), 5)


if __name__ == "__main__":
    unittest.main()
