"""Build small, valid PDFs for tests: one text page per string (Helvetica, one line per \\n), or an image-only look-alike page (None) that draws a
rectangle and carries NO text layer. No third-party writer: the file is assembled by hand, so tests have no extra dependency."""
from __future__ import annotations


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[str | None]) -> bytes:
    objs: list[bytes] = []                                         # object i is objs[i-1]
    n_pages = len(pages)
    page_ids = [4 + 2 * i for i in range(n_pages)]               # page object ids; each page's content stream is id + 1
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{i} 0 R" for i in page_ids)
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, text in enumerate(pages):
        pid = page_ids[i]
        if text is None:
            stream = b"0.8 g 50 50 500 600 re f"                   # a filled box: a "scan" with nothing to extract
        else:
            lines = text.split("\n")
            body = ["BT", "/F1 9 Tf", "11 TL", "40 760 Td"]
            for ln in lines:
                body.append(f"({_esc(ln)}) Tj T*")
            body.append("ET")
            stream = "\n".join(body).encode("latin-1", "replace")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {pid + 1} 0 R >>".encode())
        objs.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
