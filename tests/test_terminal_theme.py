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

    def test_logo_gradient_finishes_with_light_gray_line(self):
        self.assertEqual(terminal_theme.LOGO_GRADIENT[0], "#c8ff7a")
        self.assertEqual(terminal_theme.LOGO_GRADIENT[-1], "#c9d1c8")
        self.assertEqual(len(terminal_theme.LOGO_GRADIENT), 5)

    def test_prompt_marks_ansi_as_nonprinting_for_readline(self):
        prompt = terminal_theme.prompt()

        self.assertIn(terminal_theme.READLINE_START_IGNORE, prompt)
        self.assertIn(terminal_theme.READLINE_END_IGNORE, prompt)
        self.assertEqual(terminal_theme.visible_prompt(prompt), "\033[38;2;111;143;31malge>\033[0m ")

    def test_status_pips_use_expected_styles(self):
        self.assertEqual(terminal_theme.pip("pending"), "[warning]•[/warning]")
        self.assertEqual(terminal_theme.pip("success"), "[success]•[/success]")
        self.assertEqual(terminal_theme.pip("failed"), "[danger]•[/danger]")


if __name__ == "__main__":
    unittest.main()
