"""Local OCR only. PDFium renders; Tesseract recognizes Korean and English."""

import csv
import io
import math
import subprocess

import pypdfium2 as pdfium


class OCRUnavailable(RuntimeError):
    pass


class TesseractOCR:
    def __call__(self, pdf: bytes, page_index_zero_based: int, config: dict) -> dict:
        languages = config.get("languages", "kor+eng")
        data_args = ["--tessdata-dir", config["tessdata_dir"]] if config.get("tessdata_dir") else []
        try:
            available = (
                subprocess.run(
                    ["tesseract", *data_args, "--list-langs"],
                    capture_output=True,
                    check=True,
                    timeout=10,
                )
                .stdout.decode("utf-8", errors="replace")
                .splitlines()
            )
            if not set(languages.split("+")) <= {line.strip() for line in available}:
                raise OCRUnavailable("Install both kor and eng Tesseract language models")
            with pdfium.PdfDocument(pdf) as document:
                page = document[page_index_zero_based]
                try:
                    width, height = page.get_size()
                    scale = min(
                        config.get("dpi", 300) / 72,
                        math.sqrt(config.get("max_pixels", 16000000) / (width * height)),
                    )
                    bitmap = page.render(scale=scale)
                    try:
                        image = bitmap.to_pil()
                        try:
                            output = io.BytesIO()
                            image.save(output, format="PNG")
                            image_width, image_height = image.size
                        finally:
                            image.close()
                    finally:
                        bitmap.close()
                finally:
                    page.close()
            result = subprocess.run(
                [
                    "tesseract",
                    "stdin",
                    "stdout",
                    *data_args,
                    "-l",
                    languages,
                    "--psm",
                    "3",
                    "-c",
                    "load_system_dawg=0",
                    "-c",
                    "load_freq_dawg=0",
                    "-c",
                    "tessedit_create_tsv=1",
                ],
                input=output.getvalue(),
                capture_output=True,
                check=True,
                timeout=config.get("timeout", 120),
            )
        except OCRUnavailable:
            raise
        except FileNotFoundError as exc:
            raise OCRUnavailable("Tesseract executable is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise OCRUnavailable("OCR timed out") from exc
        except subprocess.CalledProcessError as exc:
            raise OCRUnavailable("Tesseract could not recognize the page") from exc
        grouped, confidences = {}, []
        rows = csv.DictReader(
            io.StringIO(result.stdout.decode("utf-8", errors="replace")),
            delimiter="\t",
            quoting=csv.QUOTE_NONE,
        )
        if not rows.fieldnames or not {"level", "text", "conf", "left", "top"} <= set(
            rows.fieldnames
        ):
            raise OCRUnavailable("Tesseract returned an invalid TSV response")
        for row in rows:
            if row.get("level") != "5" or not row.get("text", "").strip():
                continue
            key = (row["block_num"], row["par_num"], row["line_num"])
            confidence = float(row["conf"])
            word = {
                "text": row["text"],
                "confidence": confidence,
                "bbox": {
                    "x": int(row["left"]) / image_width,
                    "y": int(row["top"]) / image_height,
                    "width": int(row["width"]) / image_width,
                    "height": int(row["height"]) / image_height,
                },
            }
            grouped.setdefault(key, []).append(word)
            confidences.append(confidence)
        lines = [
            {
                "text": " ".join(word["text"] for word in words),
                "words": words,
                "ocr_confidence": min(word["confidence"] for word in words),
            }
            for words in grouped.values()
        ]
        return {
            "text": "\n".join(line["text"] for line in lines),
            "lines": lines,
            "ocr_confidence": sum(confidences) / len(confidences) if confidences else 0.0,
            "ocr_min_word_confidence": min(confidences) if confidences else 0.0,
        }
