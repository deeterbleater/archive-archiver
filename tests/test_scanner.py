import tempfile
import unittest
from pathlib import Path

import scanner


class ScannerTests(unittest.TestCase):
    def test_eicar_signature_is_blocked_without_external_scanner(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "eicar.txt"
            path.write_bytes(scanner.EICAR)

            with self.assertRaises(scanner.MalwareDetected):
                scanner.scan_file(path, required=True)


if __name__ == "__main__":
    unittest.main()
