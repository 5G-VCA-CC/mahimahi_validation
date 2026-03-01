from pathlib import Path
from svglib.svglib import svg2rlg
from reportlab.graphics import renderPDF


def svg_to_pdf(svg_path: Path, pdf_path: Path):
    drawing = svg2rlg(str(svg_path))
    renderPDF.drawToFile(drawing, str(pdf_path))


def main():
    folder = Path("svgs")  # change to your folder
    folder_to_save = Path("pdfs")  # change to your desired output folder
    folder_to_save.mkdir(exist_ok=True)
    for svg_file in folder.glob("*.svg"):
        pdf_file = folder_to_save / svg_file.with_suffix(".pdf").name
        svg_to_pdf(svg_file, pdf_file)
        print(f"Converted {svg_file.name} -> {pdf_file.name}")

if __name__ == "__main__":
    main()