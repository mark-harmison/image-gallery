#!/usr/bin/env python3
"""
photos_to_gallery.py

Reads JPG images from a folder, extracts two text fields from XMP metadata
using exiftool, and produces:

  • Caption  – short title   (XMP fields tried in order: Title, Headline)
  • Comment  – longer note   (XMP fields tried in order: Description, Caption,
                               Subject joined with ", ")

Produces:
  1. An HTML file with a lightbox/slideshow gallery.
       - Images are referenced by relative URL (NOT embedded in the HTML).
       - Caption shown as the tooltip on each thumbnail.
       - Caption shown as a heading in the lightbox above the full image.
       - Comment shown as italic text below the image in the lightbox.
       NOTE: The HTML file must live in the same folder as the images so that
             the relative URLs resolve correctly when opened in a browser.
             If --output-dir is used it must equal the image folder.
  2. A PDF file with one image per page.
       - Caption shown as a bold heading at the top of each page.
       - Comment shown as italic text below the image.

Usage:
    python photos_to_gallery.py [image_folder] [--output-dir OUTPUT_DIR] [--title TITLE]

Dependencies:
    exiftool  (must be installed and on PATH — https://exiftool.org)
    pip install Pillow reportlab

Arguments:
    image_folder   Folder containing .jpg/.jpeg/.png files (default: current directory)
    --output-dir   Where to write the output files (default: same as image_folder)
    --title        Title shown in the gallery (default: "Photo Gallery")
"""

import argparse
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

# ── third-party ──────────────────────────────────────────────────────────────
try:
    from PIL import Image, ImageOps
except ImportError:
    sys.exit("ERROR: Pillow is not installed.  Run:  pip install Pillow")

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_CENTER
    from reportlab.platypus import (
        SimpleDocTemplate, Image as RLImage, Paragraph, Spacer, PageBreak
    )
    from reportlab.lib import colors
except ImportError:
    sys.exit("ERROR: reportlab is not installed.  Run:  pip install reportlab")


# ─────────────────────────────────────────────────────────────────────────────
# exiftool helpers
# ─────────────────────────────────────────────────────────────────────────────

def check_exiftool() -> str:
    """Return the path to exiftool, or exit with a helpful message."""
    path = shutil.which("exiftool")
    if not path:
        sys.exit(
            "ERROR: exiftool is not installed or not on PATH.\n"
            "  Install it from https://exiftool.org  (or via your package manager:\n"
            "    macOS:   brew install exiftool\n"
            "    Ubuntu:  sudo apt install libimage-exiftool-perl\n"
            "    Windows: download the installer from https://exiftool.org)"
        )
    return path


def _scalar(value) -> str:
    """Flatten an XMP value to a plain string (handles lists and alt-lang dicts)."""
    if value is None:
        return ""
    if isinstance(value, list):
        # e.g. Subject is a list of keywords
        return ", ".join(str(v) for v in value if v)
    if isinstance(value, dict):
        # Alt-lang structs look like {"en": "text", "_": "text"}
        for key in ("en", "en-US", "en-GB", "_", "x-default"):
            if key in value:
                return str(value[key]).strip()
        # Fall back to first value
        return str(next(iter(value.values()))).strip()
    return str(value).strip()


def read_xmp_fields(paths: list[Path], exiftool_bin: str) -> dict[str, tuple[str, str]]:
    """
    Run exiftool once over all files and return a dict mapping
    absolute path string -> (caption, comment).

    XMP fields tried for caption  : XMP:Title, XMP:Headline
    XMP fields tried for comment  : XMP:Description, XMP:Caption, XMP:Subject
    """
    if not paths:
        return {}

    cmd = [
        exiftool_bin,
        "-json",           # JSON output
        "-XMP:Title",
        "-XMP:Headline",
        "-XMP:Description",
        "-XMP:Caption",
        "-XMP:Subject",
        "--",              # end of options; filenames follow
    ] + [str(p) for p in paths]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:
        sys.exit(f"ERROR: Failed to run exiftool: {exc}")

    try:
        records = json.loads(result.stdout)
    except json.JSONDecodeError:
        sys.exit(
            f"ERROR: exiftool did not return valid JSON.\n"
            f"stderr: {result.stderr[:500]}"
        )

    out: dict[str, tuple[str, str]] = {}
    for rec in records:
        # exiftool reports the path in "SourceFile"
        src = str(Path(rec.get("SourceFile", "")).resolve())

        # ── Caption: first non-empty of Title, Headline ──
        caption = ""
        for field in ("Title", "Headline"):
            val = _scalar(rec.get(f"XMP:{field}") or rec.get(field))
            if val:
                caption = val
                break

        # ── Comment: first non-empty of Description, Caption, Subject ──
        comment = ""
        for field in ("Description", "Caption", "Subject"):
            val = _scalar(rec.get(f"XMP:{field}") or rec.get(field))
            if val:
                comment = val
                break

        # Suppress comment if it duplicates the caption
        if caption and comment and caption.strip() == comment.strip():
            comment = ""

        out[src] = (caption, comment)

    return out


# ─────────────────────────────────────────────────────────────────────────────
# Image collection
# ─────────────────────────────────────────────────────────────────────────────

def collect_images(folder: Path, exiftool_bin: str) -> list[tuple[Path, str, str]]:
    """Return sorted list of (path, caption, comment) for all JPEG and PNG files in folder."""
    extensions = {".jpg", ".jpeg", ".png"}
    paths = sorted(p for p in folder.iterdir() if p.suffix.lower() in extensions)
    if not paths:
        return []

    metadata = read_xmp_fields(paths, exiftool_bin)

    items = []
    for p in paths:
        caption, comment = metadata.get(str(p.resolve()), ("", ""))
        if not caption and not comment:
            caption = p.stem.replace("_", " ")
        items.append((p, caption, comment))
    return items


# ─────────────────────────────────────────────────────────────────────────────
# HTML generator  (images referenced by relative URL, NOT embedded)
# ─────────────────────────────────────────────────────────────────────────────

# Maximum pixel length of the longest side of a thumbnail image.
THUMB_MAX_PX = 320

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: Georgia, 'Times New Roman', serif;
    background: #1a1a1a;
    color: #f0f0f0;
    min-height: 100vh;
  }}

  header {{
    text-align: center;
    padding: 2.5rem 1rem 1.5rem;
    background: linear-gradient(180deg, #111 0%, #1a1a1a 100%);
    border-bottom: 1px solid #333;
  }}
  header h1 {{
    font-size: clamp(1.6rem, 4vw, 2.8rem);
    font-weight: normal;
    letter-spacing: 0.05em;
    color: #e8d8b8;
  }}
  header p.subtitle {{
    margin-top: 0.4rem;
    font-size: 0.9rem;
    color: #888;
    font-style: italic;
  }}

  .grid {{
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    padding: 2rem;
    max-width: 1400px;
    margin: 0 auto;
    justify-content: center;
    align-items: flex-start;
  }}
  .thumb {{
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border-radius: 4px;
    background: #111;
    position: relative;
    border: 2px solid transparent;
    transition: border-color 0.2s, transform 0.2s;
    max-width: {{thumb_max_px}}px;
    max-height: {{thumb_max_px}}px;
  }}
  .thumb:hover {{
    border-color: #e8d8b8;
    transform: scale(1.03);
    z-index: 1;
  }}
  .thumb img {{
    display: block;
    max-width: {{thumb_max_px}}px;
    max-height: {{thumb_max_px}}px;
    width: auto;
    height: auto;
    border-radius: 2px;
  }}
  .thumb .thumb-caption {{
    position: absolute;
    bottom: 0; left: 0; right: 0;
    background: linear-gradient(0deg, rgba(0,0,0,0.8) 0%, transparent 100%);
    color: #fff;
    font-size: 0.75rem;
    padding: 1.5rem 0.5rem 0.4rem;
    opacity: 0;
    transition: opacity 0.25s;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .thumb:hover .thumb-caption {{ opacity: 1; }}

  #lightbox {{
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.93);
    z-index: 1000;
    align-items: center;
    justify-content: center;
    flex-direction: column;
  }}
  #lightbox.active {{ display: flex; }}

  #lb-img-wrap {{
    position: relative;
    max-width: 90vw;
    max-height: 75vh;
    display: flex;
    align-items: center;
    justify-content: center;
  }}
  #lb-img {{
    max-width: 90vw;
    max-height: 75vh;
    border-radius: 3px;
    box-shadow: 0 8px 40px rgba(0,0,0,0.7);
    display: block;
  }}

  #lb-heading {{
    margin-top: 0.9rem;
    font-size: clamp(1rem, 2.5vw, 1.4rem);
    color: #ffffff;
    text-align: center;
    max-width: 80vw;
    line-height: 1.4;
    font-weight: bold;
    font-style: normal;
    font-family: Georgia, serif;
    letter-spacing: 0.02em;
  }}
  #lb-caption {{
    margin-top: 0.4rem;
    font-size: clamp(0.85rem, 1.8vw, 1.1rem);
    color: #e8d8b8;
    text-align: center;
    max-width: 80vw;
    line-height: 1.5;
    font-style: italic;
  }}
  #lb-counter {{
    margin-top: 0.5rem;
    font-size: 0.8rem;
    color: #666;
    font-style: normal;
    font-family: sans-serif;
  }}
  #lb-filename {{
    margin-top: 0.3rem;
    font-size: 0.75rem;
    color: #555;
    font-style: normal;
    font-family: 'Courier New', Courier, monospace;
    letter-spacing: 0.03em;
  }}

  .lb-btn {{
    position: fixed;
    top: 50%;
    transform: translateY(-50%);
    background: rgba(255,255,255,0.1);
    border: none;
    color: #fff;
    font-size: 2rem;
    width: 3rem;
    height: 5rem;
    cursor: pointer;
    border-radius: 3px;
    transition: background 0.2s;
    z-index: 1001;
    line-height: 1;
  }}
  .lb-btn:hover {{ background: rgba(255,255,255,0.25); }}
  #lb-prev {{ left: 0.5rem; }}
  #lb-next {{ right: 0.5rem; }}

  #lb-close {{
    position: fixed;
    top: 1rem; right: 1rem;
    background: rgba(255,255,255,0.1);
    border: none;
    color: #fff;
    font-size: 1.6rem;
    width: 2.5rem;
    height: 2.5rem;
    cursor: pointer;
    border-radius: 50%;
    transition: background 0.2s;
    z-index: 1001;
    line-height: 1;
  }}
  #lb-close:hover {{ background: rgba(255,255,255,0.3); }}

  .hint {{
    text-align: center;
    color: #555;
    font-size: 0.75rem;
    padding-bottom: 2rem;
    font-family: sans-serif;
  }}
</style>
</head>
<body>

<header>
  <h1>{title}</h1>
  <p class="subtitle">{count} photo{plural} &mdash; click any image to view full size</p>
</header>

<div class="grid">
{thumbnails}
</div>

<p class="hint">&#x2190; &#x2192; arrow keys or click arrows to navigate &bull; Esc to close</p>

<div id="lightbox" role="dialog" aria-modal="true" aria-label="Photo viewer">
  <button id="lb-close" aria-label="Close">&times;</button>
  <button class="lb-btn" id="lb-prev" aria-label="Previous">&#8249;</button>
  <div id="lb-img-wrap">
    <img id="lb-img" src="" alt="">
  </div>
  <button class="lb-btn" id="lb-next" aria-label="Next">&#8250;</button>
  <p id="lb-heading"></p>
  <p id="lb-caption"></p>
  <p id="lb-filename"></p>
  <p id="lb-counter"></p>
</div>

<script>
const photos = {photos_json};

let current = 0;
const lb      = document.getElementById('lightbox');
const lbImg   = document.getElementById('lb-img');
const lbHead  = document.getElementById('lb-heading');
const lbCap   = document.getElementById('lb-caption');
const lbFname = document.getElementById('lb-filename');
const lbCtr   = document.getElementById('lb-counter');

function show(index) {{
  current = (index + photos.length) % photos.length;
  const p = photos[current];
  lbImg.src = p.src;
  lbImg.alt = p.caption || p.comment;
  lbHead.textContent = p.caption;
  lbHead.style.display = p.caption ? '' : 'none';
  lbCap.textContent  = p.comment;
  lbCap.style.display = p.comment ? '' : 'none';
  lbFname.textContent = p.filename;
  lbCtr.textContent  = (current + 1) + ' / ' + photos.length;
  lb.classList.add('active');
  lb.focus();
}}

function close() {{ lb.classList.remove('active'); }}

document.getElementById('lb-close').addEventListener('click', close);
document.getElementById('lb-prev').addEventListener('click', () => show(current - 1));
document.getElementById('lb-next').addEventListener('click', () => show(current + 1));
lb.addEventListener('click', e => {{ if (e.target === lb) close(); }});

document.addEventListener('keydown', e => {{
  if (!lb.classList.contains('active')) return;
  if (e.key === 'ArrowLeft')  show(current - 1);
  if (e.key === 'ArrowRight') show(current + 1);
  if (e.key === 'Escape')     close();
}});

let touchStartX = 0;
lb.addEventListener('touchstart', e => {{ touchStartX = e.changedTouches[0].screenX; }}, {{passive: true}});
lb.addEventListener('touchend',   e => {{
  const dx = e.changedTouches[0].screenX - touchStartX;
  if (Math.abs(dx) > 50) show(current + (dx < 0 ? 1 : -1));
}});
</script>
</body>
</html>
"""

def make_thumbnail(src_path: Path, thumb_dir: Path, max_px: int) -> Path:
    """
    Create a scaled-down copy of src_path inside thumb_dir, constrained so
    that neither side exceeds max_px pixels.  Returns the path of the new file.
    The thumbnail is always saved as JPEG regardless of the source format.
    """
    thumb_dir.mkdir(parents=True, exist_ok=True)
    thumb_path = thumb_dir / (src_path.stem + ".jpg")

    img = Image.open(src_path).convert("RGB")
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass

    w, h = img.size
    scale = min(max_px / w, max_px / h, 1.0)   # never upscale
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    img.save(thumb_path, format="JPEG", quality=82, optimize=True)
    return thumb_path


THUMB_TEMPLATE = (
    '  <div class="thumb" onclick="show({idx})" tabindex="0" '
    'onkeydown="if(event.key===\'Enter\')show({idx})" '
    'role="button" aria-label="{tooltip_attr}" title="{tooltip_attr}">\n'
    '    <img src="{thumb_src}" alt="{tooltip_attr}" loading="lazy">\n'
    '    <span class="thumb-caption">{overlay_html}</span>\n'
    '  </div>'
)


def build_html(items: list[tuple[Path, str, str]], title: str, output_path: Path):
    import html as html_mod

    photos_json_list = []
    thumb_parts = []

    # Thumbnails go into a "thumbs" subfolder next to the HTML file.
    thumb_dir = output_path.parent / "thumbs"

    print(f"  Building HTML — {len(items)} image(s), generating thumbnails in '{thumb_dir.name}/'…")
    for idx, (path, caption, comment) in enumerate(items):
        print(f"    [{idx+1}/{len(items)}] {path.name}")

        # Full-size relative URL (for the lightbox).
        try:
            rel_src = path.relative_to(output_path.parent).as_posix()
        except ValueError:
            rel_src = path.name

        # Scaled-down thumbnail (for the grid).
        thumb_path = make_thumbnail(path, thumb_dir, THUMB_MAX_PX)
        try:
            thumb_rel = thumb_path.relative_to(output_path.parent).as_posix()
        except ValueError:
            thumb_rel = f"thumbs/{thumb_path.name}"

        tooltip = caption or comment
        tooltip_attr = html_mod.escape(tooltip, quote=True)
        overlay_html = html_mod.escape(caption or comment)

        photos_json_list.append({
            "src":      rel_src,
            "caption":  caption,
            "comment":  comment,
            "filename": path.name,
        })
        thumb_parts.append(
            THUMB_TEMPLATE.format(
                idx=idx,
                thumb_src=html_mod.escape(thumb_rel, quote=True),
                tooltip_attr=tooltip_attr,
                overlay_html=overlay_html,
            )
        )

    thumbnails_html = "\n".join(thumb_parts)
    photos_json     = json.dumps(photos_json_list, ensure_ascii=False)
    count  = len(items)
    plural = "s" if count != 1 else ""

    html_out = HTML_TEMPLATE.format(
        title=html_mod.escape(title),
        count=count,
        plural=plural,
        thumbnails=thumbnails_html,
        photos_json=photos_json,
        thumb_max_px=THUMB_MAX_PX,
    )

    output_path.write_text(html_out, encoding="utf-8")
    size_kb = output_path.stat().st_size / 1024
    print(f"  ✓ HTML saved → {output_path}  ({size_kb:.1f} KB)")
    if output_path.parent.resolve() != items[0][0].parent.resolve():
        print("  ⚠  WARNING: HTML is in a different folder from the images.")
        print("     Relative URLs (for both full-size images and thumbs/) will break.")
        print("     Use --output-dir equal to the image folder.")


# ─────────────────────────────────────────────────────────────────────────────
# PDF generator
# ─────────────────────────────────────────────────────────────────────────────

def build_pdf(items: list[tuple[Path, str, str]], title: str, output_path: Path):
    PAGE_W, PAGE_H = A4
    MARGIN    = 1.8 * cm
    IMG_MAX_W = PAGE_W - 2 * MARGIN
    IMG_MAX_H = PAGE_H * 0.62     # room for heading + caption + filename

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN,  bottomMargin=MARGIN,
        title=title,
    )

    title_style = ParagraphStyle(
        "GalleryTitle",
        fontName="Times-Roman", fontSize=22, leading=28,
        alignment=TA_CENTER, textColor=colors.HexColor("#2c2c2c"), spaceAfter=6,
    )
    heading_style = ParagraphStyle(
        "PageHeading",
        fontName="Times-Bold", fontSize=16, leading=20,
        alignment=TA_CENTER, textColor=colors.HexColor("#111111"), spaceAfter=8,
    )
    caption_style = ParagraphStyle(
        "Caption",
        fontName="Times-Italic", fontSize=12, leading=16,
        alignment=TA_CENTER, textColor=colors.HexColor("#333333"), spaceBefore=10,
    )
    filename_style = ParagraphStyle(
        "Filename",
        fontName="Courier", fontSize=9, leading=12,
        alignment=TA_CENTER, textColor=colors.HexColor("#888888"), spaceBefore=4,
    )
    num_style = ParagraphStyle(
        "PageNum",
        fontName="Helvetica", fontSize=9, leading=12,
        alignment=TA_CENTER, textColor=colors.HexColor("#999999"), spaceBefore=4,
    )

    story = []

    # Cover page
    story.append(Spacer(1, PAGE_H * 0.35))
    story.append(Paragraph(title, title_style))
    story.append(Spacer(1, 0.3 * cm))
    count = len(items)
    story.append(Paragraph(f"{count} photograph{'s' if count != 1 else ''}", num_style))
    story.append(PageBreak())

    print(f"  Building PDF — processing {len(items)} image(s)…")
    for idx, (path, caption, comment) in enumerate(items):
        print(f"    [{idx+1}/{len(items)}] {path.name}")

        # Caption heading at top of page
        if caption:
            story.append(Paragraph(caption, heading_style))
        else:
            story.append(Spacer(1, 0.4 * cm))

        # Open & auto-rotate via exif orientation
        img = Image.open(path).convert("RGB")
        try:
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass

        iw, ih = img.size
        scale  = min(IMG_MAX_W / iw, IMG_MAX_H / ih, 1.0)
        draw_w, draw_h = iw * scale, ih * scale

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        buf.seek(0)

        story.append(RLImage(buf, width=draw_w, height=draw_h))

        if comment:
            story.append(Paragraph(comment, caption_style))
        story.append(Paragraph(path.name, filename_style))
        story.append(Paragraph(f"{idx + 1} / {count}", num_style))
        story.append(PageBreak())

    doc.build(story)
    size_mb = output_path.stat().st_size / 1_048_576
    print(f"  ✓ PDF  saved → {output_path}  ({size_mb:.1f} MB)")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Convert a folder of JPEGs (with XMP metadata) into an HTML "
            "lightbox gallery and a PDF.  Requires exiftool on PATH."
        )
    )
    parser.add_argument(
        "image_folder", nargs="?", default=".",
        help="Folder containing .jpg/.jpeg/.png files (default: current directory)",
    )
    parser.add_argument(
        "--output-dir", default=None,
        help="Where to save output files (default: same as image_folder). "
             "For HTML, this MUST equal the image folder so relative URLs work.",
    )
    parser.add_argument(
        "--title", default="Photo Gallery",
        help='Gallery title (default: "Photo Gallery")',
    )
    args = parser.parse_args()

    exiftool_bin = check_exiftool()

    image_folder = Path(args.image_folder).resolve()
    if not image_folder.is_dir():
        sys.exit(f"ERROR: '{image_folder}' is not a directory.")

    output_dir = Path(args.output_dir).resolve() if args.output_dir else image_folder
    output_dir.mkdir(parents=True, exist_ok=True)

    items = collect_images(image_folder, exiftool_bin)
    if not items:
        sys.exit(f"No .jpg/.jpeg/.png files found in '{image_folder}'.")

    print(f"\nFound {len(items)} image(s) in: {image_folder}")
    for p, cap, com in items:
        print(f"  {p.name}")
        if cap: print(f"    caption : \"{cap}\"")
        if com: print(f"    comment : \"{com}\"")
    print()

    safe_title = "".join(c if c.isalnum() or c in " _-" else "_" for c in args.title).strip()
    stem      = safe_title.replace(" ", "_")
    html_path = output_dir / f"{stem}.html"
    pdf_path  = output_dir / f"{stem}.pdf"

    build_html(items, args.title, html_path)
    build_pdf(items, args.title, pdf_path)

    print("\nDone! 🎉")
    print(f"  HTML → {html_path}")
    print(f"  PDF  → {pdf_path}")


if __name__ == "__main__":
    main()
