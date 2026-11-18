"""Fonts for the PDFs the app makes (invoices, quotes, contracts written out as PDF).

The PDF built-in Helvetica covers Western European letters only, so Chinese names came out as "?".
When the open-licensed Noto Sans SC font (SIL Open Font License) is in fonts/, it is embedded instead,
and fpdf2 keeps only the characters used, so PDFs stay small.
"""

import re

from gmail_auto.paths import CODE_ROOT

FONT_DIR = CODE_ROOT / "fonts"
REGULAR = "NotoSansSC-Regular.otf"
BOLD = "NotoSansSC-Bold.otf"
_CJK = re.compile(r"[⺀-鿿가-힯＀-￯]")
_LATIN = str.maketrans({"—": "-", "–": "-", "‘": "'", "’": "'", "“": '"', "”": '"', "…": "...", "•": "-", " ": " "})


def unicode_ready() -> bool:
    return (FONT_DIR / REGULAR).is_file()


def setup(pdf, needed: bool = True) -> tuple[str, bool]:
    """Register the font on this PDF. Returns (family name, whether it covers Chinese).

    Loading the Chinese font takes a moment, so it is only used when the document has Chinese in it
    (needed=False keeps the built-in font).
    """
    if not needed or not unicode_ready():
        return "Helvetica", False
    regular = str(FONT_DIR / REGULAR)
    bold = FONT_DIR / BOLD
    pdf.add_font("NotoSC", "", regular)
    pdf.add_font("NotoSC", "B", str(bold) if bold.is_file() else regular)
    return "NotoSC", True


def clean(text, unicode: bool) -> str:
    """Text the chosen font can draw: everything with Noto; Western European letters only with Helvetica."""
    text = str(text or "")
    if unicode:
        return text
    return text.translate(_LATIN).encode("latin-1", "replace").decode("latin-1")


def has_cjk(text: str) -> bool:
    return bool(_CJK.search(text or ""))


class PdfText:
    """A plain A4 document: a heading and paragraphs. Used when a template cannot be filled in place."""

    def __init__(self, chinese: bool = True):
        from fpdf import FPDF

        self.pdf = FPDF(format="A4")
        self.pdf.set_auto_page_break(True, margin=18)
        self.pdf.set_margins(20, 20, 20)
        self.pdf.add_page()
        self.family, self.unicode = setup(self.pdf, chinese)

    def heading(self, text: str) -> None:
        self._write(text, 16, "B", 9)
        self.pdf.ln(3)

    def paragraph(self, text: str) -> None:
        if text.strip():
            self._write(text, 10.5, "", 5.6)
            self.pdf.ln(2.5)

    def output(self) -> bytes:
        return bytes(self.pdf.output())

    def _write(self, text: str, size: float, style: str, height: float) -> None:
        from fpdf.enums import XPos, YPos

        self.pdf.set_font(self.family, style, size)
        self.pdf.set_text_color(34, 30, 26)
        # Word wrapping keeps English words whole and still breaks long Chinese lines by character.
        self.pdf.multi_cell(0, height, clean(text, self.unicode), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
