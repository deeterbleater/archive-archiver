from pathlib import Path
import tempfile
import unittest
from unittest import mock

import ocr


class OCRTests(unittest.TestCase):
    def test_extract_pdf_falls_back_between_engines(self):
        path = Path("fixture.pdf")
        useful = "Recognized page text " * 20

        with mock.patch("ocr._ocr_pdf_with_ocrmypdf", side_effect=ocr.OCRError("bad")):
            with mock.patch("ocr._ocr_pdf_with_tesseract", return_value=useful):
                text = ocr.extract_pdf(path, max_pages=3)

        self.assertIn("Recognized page text", text)

    def test_extract_pdf_reports_unavailable_when_no_engine_works(self):
        path = Path("fixture.pdf")

        with mock.patch("ocr._ocr_pdf_with_ocrmypdf", side_effect=ocr.OCRUnavailable("missing ocrmypdf")):
            with mock.patch("ocr._ocr_pdf_with_tesseract", side_effect=ocr.OCRUnavailable("missing tesseract")):
                with self.assertRaisesRegex(ocr.OCRUnavailable, "missing ocrmypdf"):
                    ocr.extract_pdf(path)

    def test_extract_image_uses_tesseract_stdout(self):
        class Result:
            stdout = "Recognized image text " * 20

        with tempfile.TemporaryDirectory() as tempdir:
            image = Path(tempdir) / "page.png"
            image.write_bytes(b"fake image")

            with mock.patch("ocr._tool", return_value="/usr/bin/tesseract"):
                with mock.patch("ocr._run", return_value=Result()) as run:
                    text = ocr.extract_image(image)

        self.assertIn("Recognized image text", text)
        self.assertEqual(run.call_args.args[0][0], "/usr/bin/tesseract")
        self.assertIn("stdout", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
