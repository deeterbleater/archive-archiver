import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


DEFAULT_MAX_PAGES = int(os.getenv("ALGE_OCR_MAX_PAGES", "50"))
MIN_USEFUL_CHARS = int(os.getenv("ALGE_OCR_MIN_CHARS", "200"))
OCR_ENABLED = os.getenv("ALGE_OCR_ENABLED", "1").lower() in ("1", "true", "yes")
OCR_LANGUAGE = os.getenv("ALGE_OCR_LANGUAGE", "eng")


class OCRUnavailable(ValueError):
    pass


class OCRError(ValueError):
    pass


def _normalize_text(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _has_useful_text(text, min_chars=MIN_USEFUL_CHARS):
    alnum = re.findall(r"[A-Za-z0-9]", text or "")
    return len(alnum) >= min_chars


def available_tools():
    return {
        "ocrmypdf": shutil.which(os.getenv("ALGE_OCRMYPDF_BIN", "ocrmypdf")),
        "tesseract": shutil.which(os.getenv("ALGE_TESSERACT_BIN", "tesseract")),
        "pdftoppm": shutil.which(os.getenv("ALGE_PDFTOPPM_BIN", "pdftoppm")),
    }


def _tool(name):
    path = available_tools().get(name)
    if not path:
        raise OCRUnavailable(f"{name} is not installed")
    return path


def _run(command, timeout):
    try:
        return subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise OCRError(f"OCR command timed out: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise OCRError(f"OCR command failed: {command[0]}: {detail[:500]}") from exc


def _ocr_pdf_with_ocrmypdf(path, max_pages=None, timeout=None):
    binary = _tool("ocrmypdf")
    timeout = timeout or int(os.getenv("ALGE_OCRMYPDF_TIMEOUT", "1800"))
    with tempfile.TemporaryDirectory(prefix="alge-ocrmypdf-") as tempdir:
        tempdir = Path(tempdir)
        output_pdf = tempdir / "ocr.pdf"
        sidecar = tempdir / "sidecar.txt"
        command = [
            binary,
            "--skip-text",
            "--optimize", "0",
            "--output-type", "pdf",
            "--sidecar", str(sidecar),
        ]
        if max_pages:
            command.extend(["--pages", f"1-{max_pages}"])
        command.extend([str(path), str(output_pdf)])
        _run(command, timeout)
        if not sidecar.exists():
            raise OCRError("ocrmypdf completed without a sidecar text file")
        return _normalize_text(sidecar.read_text(encoding="utf-8", errors="replace"))


def _ocr_pdf_with_tesseract(path, max_pages=None, timeout=None):
    pdftoppm = _tool("pdftoppm")
    tesseract = _tool("tesseract")
    timeout = timeout or int(os.getenv("ALGE_TESSERACT_TIMEOUT", "1800"))
    with tempfile.TemporaryDirectory(prefix="alge-tesseract-pdf-") as tempdir:
        tempdir = Path(tempdir)
        prefix = tempdir / "page"
        command = [pdftoppm, "-r", "200", "-png"]
        if max_pages:
            command.extend(["-f", "1", "-l", str(max_pages)])
        command.extend([str(path), str(prefix)])
        _run(command, timeout)

        pages = sorted(tempdir.glob("page-*.png"))
        if not pages:
            raise OCRError("pdftoppm produced no page images")

        texts = []
        per_page_timeout = max(30, timeout // max(len(pages), 1))
        for page in pages:
            result = _run(
                [tesseract, str(page), "stdout", "-l", OCR_LANGUAGE, "--psm", "1"],
                per_page_timeout,
            )
            texts.append(result.stdout)
        return _normalize_text("\n\n".join(texts))


def extract_pdf(path, max_pages=None, min_chars=MIN_USEFUL_CHARS):
    if not OCR_ENABLED:
        raise OCRUnavailable("OCR is disabled by ALGE_OCR_ENABLED")
    max_pages = DEFAULT_MAX_PAGES if max_pages is None else max_pages
    errors = []
    for engine in (_ocr_pdf_with_ocrmypdf, _ocr_pdf_with_tesseract):
        try:
            text = engine(Path(path), max_pages=max_pages)
        except OCRUnavailable as exc:
            errors.append(str(exc))
            continue
        except OCRError as exc:
            errors.append(str(exc))
            continue
        if _has_useful_text(text, min_chars=min_chars):
            return text
        if text:
            errors.append(f"{engine.__name__} returned too little text")
    raise OCRUnavailable("; ".join(errors) or "no OCR engine is available")


def extract_image(path, min_chars=MIN_USEFUL_CHARS):
    if not OCR_ENABLED:
        raise OCRUnavailable("OCR is disabled by ALGE_OCR_ENABLED")
    tesseract = _tool("tesseract")
    result = _run(
        [tesseract, str(path), "stdout", "-l", OCR_LANGUAGE, "--psm", "1"],
        int(os.getenv("ALGE_TESSERACT_TIMEOUT", "1800")),
    )
    text = _normalize_text(result.stdout)
    if not _has_useful_text(text, min_chars=min_chars):
        raise OCRUnavailable("image OCR returned too little text")
    return text
