"""Render the plain-language methodology (methodology.md next to this module) as HTML for the portal.

Only the Markdown the methodology uses is supported: headings, paragraphs, bullet and numbered
lists, tables, quotes, bold, inline code and links.
"""

import re
from html import escape
from importlib.resources import files

_INLINE = (
    (re.compile(r"\*\*(.+?)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"`(.+?)`"), r"<code>\1</code>"),
    (re.compile(r"\[(.+?)\]\((https?://[^)\s]+)\)"), r'<a href="\2">\1</a>'),
)


def _inline(text: str) -> str:
    html = escape(text)
    for pattern, replacement in _INLINE:
        html = pattern.sub(replacement, html)
    return html


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def markdown_to_html(text: str, *, heading_offset: int = 1) -> str:
    """Convert the methodology Markdown to HTML. The document title (# heading) is dropped."""
    blocks: list[str] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue
        if heading := re.match(r"(#{1,6})\s+(.*)", stripped):
            level = len(heading.group(1))
            if level > 1:
                tag = f"h{min(6, level + heading_offset)}"
                blocks.append(f"<{tag}>{_inline(heading.group(2))}</{tag}>")
            index += 1
        elif stripped.startswith("|"):
            rows = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append(_cells(lines[index]))
                index += 1
            header, body = rows[0], [row for row in rows[1:] if not all(re.fullmatch(r":?-+:?", cell) for cell in row)]
            head = "".join(f'<th scope="col">{_inline(cell)}</th>' for cell in header) if any(header) else ""
            rows_html = "".join("<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in row) + "</tr>" for row in body)
            thead = f"<thead><tr>{head}</tr></thead>" if head else ""
            blocks.append(f'<div class="table-scroll"><table>{thead}<tbody>{rows_html}</tbody></table></div>')
        elif stripped.startswith(">"):
            quote = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quote.append(lines[index].strip().lstrip(">").strip())
                index += 1
            blocks.append(f"<blockquote><p>{_inline(' '.join(quote))}</p></blockquote>")
        elif re.match(r"(- |\d+\. )", stripped):
            ordered = bool(re.match(r"\d+\. ", stripped))
            indentation = len(line) - len(line.lstrip())
            items: list[str] = []
            while index < len(lines):
                current = lines[index]
                marker = re.match(r"\s*(?:- |\d+\. )(.*)", current)
                if marker and len(current) - len(current.lstrip()) <= indentation:
                    items.append(marker.group(1).strip())
                elif current.startswith("  ") and current.strip() and items:
                    items[-1] += " " + current.strip()
                else:
                    break
                index += 1
            tag = "ol" if ordered else "ul"
            blocks.append(f"<{tag}>" + "".join(f"<li>{_inline(item)}</li>" for item in items) + f"</{tag}>")
        else:
            paragraph = []
            while index < len(lines):
                current = lines[index].strip()
                if not current or re.match(r"(#{1,6}\s|\||>|- |\d+\. )", current):
                    break
                paragraph.append(current)
                index += 1
            blocks.append(f"<p>{_inline(' '.join(paragraph))}</p>")
    return "\n".join(blocks)


def methodology_html() -> str:
    return markdown_to_html(files("purchase_planning").joinpath("methodology.md").read_text(encoding="utf-8"))
