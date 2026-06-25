#!/usr/bin/env python3
"""Minimal MinerU install verification demo.

Default mode verifies the editable install, key runtime imports, PDF reading,
and PDF preprocessing without starting the model pipeline.

Use --run-pipeline after dependencies and models are ready to run a one-page
pipeline parse and verify Markdown/JSON outputs.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def import_check() -> None:
    import cv2
    import mineru
    import numpy
    import pypdf
    import pypdfium2
    import torch
    import transformers
    from mineru.cli.common import convert_pdf_bytes_to_bytes, read_fn

    del mineru, pypdf, pypdfium2, read_fn, convert_pdf_bytes_to_bytes
    print("[OK] imports")
    print(f"     cv2={cv2.__version__} numpy={numpy.__version__}")
    print(f"     torch={torch.__version__} transformers={transformers.__version__}")


def pdf_preprocess_check(input_pdf: Path) -> bytes:
    from mineru.cli.common import convert_pdf_bytes_to_bytes, read_fn
    from mineru.utils.pdfium_guard import (
        close_pdfium_document,
        get_pdfium_document_page_count,
        open_pdfium_document,
    )
    import pypdfium2 as pdfium

    pdf_bytes = read_fn(input_pdf)
    rewritten = convert_pdf_bytes_to_bytes(pdf_bytes, start_page_id=0, end_page_id=0)
    pdf_doc = open_pdfium_document(pdfium.PdfDocument, rewritten)
    try:
        page_count = get_pdfium_document_page_count(pdf_doc)
    finally:
        close_pdfium_document(pdf_doc)

    if page_count != 1:
        raise RuntimeError(f"expected one preprocessed page, got {page_count}")
    print(f"[OK] PDF read/preprocess: {input_pdf} -> 1 page, {len(rewritten)} bytes")
    return rewritten


def run_pipeline_check(input_pdf: Path, output_dir: Path) -> None:
    from mineru.cli.common import do_parse, read_fn
    from mineru.utils.enum_class import MakeMode

    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Keep the smoke test small: one page, pipeline backend, no formula/table
    # models. Layout and OCR models are still required for a real parse.
    do_parse(
        output_dir=str(output_dir),
        pdf_file_names=[input_pdf.stem],
        pdf_bytes_list=[read_fn(input_pdf)],
        p_lang_list=["ch"],
        backend="pipeline",
        parse_method="auto",
        formula_enable=False,
        table_enable=False,
        f_draw_layout_bbox=False,
        f_draw_span_bbox=False,
        f_dump_md=True,
        f_dump_middle_json=True,
        f_dump_model_output=False,
        f_dump_orig_pdf=False,
        f_dump_content_list=True,
        f_make_md_mode=MakeMode.MM_MD,
        start_page_id=0,
        end_page_id=0,
    )

    parse_dir = output_dir / input_pdf.stem / "auto"
    md_path = parse_dir / f"{input_pdf.stem}.md"
    content_list_path = parse_dir / f"{input_pdf.stem}_content_list.json"
    middle_path = parse_dir / f"{input_pdf.stem}_middle.json"

    missing = [p for p in (md_path, content_list_path, middle_path) if not p.exists()]
    if missing:
        raise RuntimeError("missing expected output files: " + ", ".join(map(str, missing)))

    content_items = json.loads(content_list_path.read_text(encoding="utf-8"))
    middle_json = json.loads(middle_path.read_text(encoding="utf-8"))
    if not isinstance(content_items, list):
        raise RuntimeError("content_list output is not a JSON list")
    if "pdf_info" not in middle_json:
        raise RuntimeError("middle_json output does not contain pdf_info")

    print(f"[OK] pipeline parse output: {parse_dir}")
    print(f"     markdown bytes={md_path.stat().st_size}")
    print(f"     content_list items={len(content_items)}")
    print(f"     pages={len(middle_json['pdf_info'])}")


def parse_args() -> argparse.Namespace:
    root = project_root()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-pdf",
        type=Path,
        default=root / "demo" / "pdfs" / "small_ocr.pdf",
        help="PDF to verify. Defaults to demo/pdfs/small_ocr.pdf.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=root / "demo" / "smoke_output",
        help="Output directory used when --run-pipeline is enabled.",
    )
    parser.add_argument(
        "--run-pipeline",
        action="store_true",
        help="Run a real one-page pipeline parse. This may download/load models.",
    )
    parser.add_argument(
        "--model-source",
        choices=["huggingface", "modelscope", "local"],
        help="Optional MINERU_MODEL_SOURCE override for model lookup/download.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_pdf = args.input_pdf.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()

    if args.model_source:
        os.environ["MINERU_MODEL_SOURCE"] = args.model_source

    if not input_pdf.exists():
        print(f"[FAIL] input PDF not found: {input_pdf}", file=sys.stderr)
        return 2

    try:
        import_check()
        pdf_preprocess_check(input_pdf)
        if args.run_pipeline:
            run_pipeline_check(input_pdf, output_dir)
        else:
            print("[SKIP] pipeline parse not run. Add --run-pipeline after models are ready.")
    except Exception as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print("[OK] MinerU smoke demo completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
