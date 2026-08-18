"""Conservative object-level PDF/A normalization backend."""

from __future__ import annotations

import datetime as dt
from importlib.resources import files
import logging
from pathlib import Path

import pikepdf
from pikepdf import Dictionary, Name, ObjectStreamMode, Pdf

from ..colorspace import normalize_resource_color_spaces
from ..fonts import embed_missing_fonts
from ..icc import embed_icc_profile
from .base import BackendResult, ConversionBackendError

logger = logging.getLogger(__name__)


class PikePDFBackend:
    """Fast path for PDFs whose preflight proves object-level repair is safe."""

    name = "pikepdf"

    def available(self) -> bool:
        return True

    def convert(
        self,
        input_path: str | Path,
        output_path: str | Path,
        *,
        level: str,
        icc_profile: str | Path | None = None,
        font_path: str | Path | None = None,
    ) -> BackendResult:
        input_path = Path(input_path)
        output_path = Path(output_path)
        profile = str(
            icc_profile
            or files("pdf2pdfa").joinpath("data/sRGB.icc.b64")
        )

        try:
            pdf = Pdf.open(str(input_path))
        except Exception as exc:
            raise ConversionBackendError(f"pikepdf could not open {input_path}: {exc}") from exc

        try:
            embed_missing_fonts(pdf, str(font_path) if font_path else None)
            embed_icc_profile(pdf, profile)
            normalize_resource_color_spaces(pdf)

            info = pdf.docinfo or Dictionary()
            title = str(info.get(Name.Title, ""))
            author = str(info.get(Name.Author, ""))
            subject = str(info.get(Name.Subject, ""))
            keywords = str(info.get(Name.Keywords, ""))
            now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()

            with pdf.open_metadata(set_pikepdf_as_editor=True) as md:
                md["pdfaid:part"] = level[0]
                md["pdfaid:conformance"] = level[1].upper()
                md["dc:format"] = "application/pdf"
                if title and not md.get("dc:title"):
                    md["dc:title"] = title
                if author and not md.get("dc:creator"):
                    md["dc:creator"] = [author]
                if subject and not md.get("dc:description"):
                    md["dc:description"] = subject
                if keywords and not md.get("pdf:Keywords"):
                    md["pdf:Keywords"] = keywords
                if not md.get("xmp:CreatorTool"):
                    md["xmp:CreatorTool"] = "pdf2pdfa"
                md["xmp:ModifyDate"] = now
                md["xmp:MetadataDate"] = now
                md["pdf:Producer"] = f"pikepdf {pikepdf.__version__} (pdf2pdfa)"

            save_kwargs: dict[str, object] = {
                "force_version": "1.4" if level.startswith("1") else "1.7",
                "encryption": False,
            }
            if level.startswith("1"):
                save_kwargs["object_stream_mode"] = ObjectStreamMode.disable

            # The package requires pikepdf >= 8.15, where these save controls
            # are part of the supported API. Do not silently retry without
            # force_version/object-stream controls: that could turn a backend
            # compatibility error into an invalid PDF/A-1 candidate.
            pdf.save(str(output_path), **save_kwargs)
        except Exception as exc:
            if isinstance(exc, ConversionBackendError):
                raise
            raise ConversionBackendError(f"pikepdf fast path failed: {exc}") from exc
        finally:
            pdf.close()

        logger.info("pikepdf fast path wrote PDF/A-%s candidate to %s", level, output_path)
        return BackendResult(backend=self.name, output_path=output_path)
