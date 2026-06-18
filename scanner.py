import os
import shutil
import subprocess


class ScanError(RuntimeError):
    pass


class MalwareDetected(ScanError):
    def __init__(self, signature, message=None):
        super().__init__(message or f"malware detected: {signature}")
        self.signature = signature


class ScannerUnavailable(ScanError):
    pass


EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


def scan_file(path, required=False):
    path = str(path)
    with open(path, "rb") as handle:
        sample = handle.read(4096)
    if EICAR in sample:
        raise MalwareDetected("EICAR-Test-File")

    scanner = os.getenv("ARCHIVE_CLAMSCAN_BIN") or shutil.which("clamscan")
    if not scanner:
        if required:
            raise ScannerUnavailable("clamscan is not available")
        return {
            "status": "unavailable",
            "engine": "none",
            "signature": None,
        }

    proc = subprocess.run(
        [scanner, "--no-summary", path],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    output = f"{proc.stdout}\n{proc.stderr}".strip()
    if proc.returncode == 0:
        return {
            "status": "clean",
            "engine": "clamscan",
            "signature": None,
        }
    if proc.returncode == 1:
        signature = "unknown"
        for line in output.splitlines():
            if " FOUND" in line:
                signature = line.rsplit(":", 1)[-1].replace("FOUND", "").strip() or signature
                break
        raise MalwareDetected(signature, output)
    raise ScannerUnavailable(output or f"clamscan exited with {proc.returncode}")
