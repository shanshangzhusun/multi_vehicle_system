from __future__ import annotations

import html
import zipfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "runtime_dataflow_code_trace.md"
OUT = ROOT / "运行数据流代码对应说明.docx"


def _run_xml(text: str, *, bold: bool = False, size: int = 22) -> str:
    safe = html.escape(text, quote=False)
    rpr = f"<w:rPr><w:sz w:val=\"{size}\"/><w:szCs w:val=\"{size}\"/>{'<w:b/>' if bold else ''}</w:rPr>"
    return f"<w:r>{rpr}<w:t xml:space=\"preserve\">{safe}</w:t></w:r>"


def _paragraph_xml(text: str, *, kind: str = "body") -> str:
    if kind == "title":
        return (
            "<w:p><w:pPr><w:jc w:val=\"center\"/></w:pPr>"
            f"{_run_xml(text, bold=True, size=32)}"
            "</w:p>"
        )
    if kind == "h1":
        return f"<w:p>{_run_xml(text, bold=True, size=28)}</w:p>"
    if kind == "h2":
        return f"<w:p>{_run_xml(text, bold=True, size=24)}</w:p>"
    if kind == "bullet":
        return f"<w:p>{_run_xml('• ' + text, size=22)}</w:p>"
    if kind == "number":
        return f"<w:p>{_run_xml(text, size=22)}</w:p>"
    return f"<w:p>{_run_xml(text, size=22)}</w:p>"


def build_document_xml(lines: list[str]) -> str:
    body: list[str] = []
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            body.append("<w:p/>")
            continue
        if line.startswith("# "):
            body.append(_paragraph_xml(line[2:].strip(), kind="title"))
        elif line.startswith("## "):
            body.append(_paragraph_xml(line[3:].strip(), kind="h1"))
        elif line.startswith("### "):
            body.append(_paragraph_xml(line[4:].strip(), kind="h2"))
        elif line.startswith("- "):
            body.append(_paragraph_xml(line[2:].strip(), kind="bullet"))
        elif len(line) > 2 and line[0].isdigit() and ". " in line[:4]:
            body.append(_paragraph_xml(line, kind="number"))
        else:
            body.append(_paragraph_xml(line, kind="body"))
    sect = (
        "<w:sectPr>"
        "<w:pgSz w:w=\"11906\" w:h=\"16838\"/>"
        "<w:pgMar w:top=\"1440\" w:right=\"1440\" w:bottom=\"1440\" w:left=\"1440\" "
        "w:header=\"720\" w:footer=\"720\" w:gutter=\"0\"/>"
        "</w:sectPr>"
    )
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">"
        "<w:body>"
        + "".join(body)
        + sect
        + "</w:body></w:document>"
    )


def main() -> None:
    src_lines = SRC.read_text(encoding="utf-8").splitlines()
    document_xml = build_document_xml(src_lines)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    core_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>全流程运行与数据流代码对应说明</dc:title>
  <dc:creator>Codex</dc:creator>
  <cp:lastModifiedBy>Codex</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>
</cp:coreProperties>
"""
    app_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>Codex</Application>
</Properties>
"""
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>
"""
    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>
"""
    document_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>
"""
    with zipfile.ZipFile(OUT, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("docProps/core.xml", core_xml)
        zf.writestr("docProps/app.xml", app_xml)
        zf.writestr("word/document.xml", document_xml)
        zf.writestr("word/_rels/document.xml.rels", document_rels)
    print(OUT)


if __name__ == "__main__":
    main()
