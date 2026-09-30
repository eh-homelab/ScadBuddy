"""dollhouse-kit's template activities (spec 2026-09-27 §5.2): plain Python run in its
own process on the render worker."""

from xml.sax.saxutils import escape


def assembly_guide(house, entries):
    """An SVG checklist: the footprint grid, then every piece with its count."""
    cell, pad = 40, 20
    width = max(house["cols"] * cell + 2 * pad, 360)
    grid_h = house["rows"] * cell
    lines = [
        f'<rect x="{pad + c * cell}" y="{pad + r * cell}" width="{cell}" height="{cell}" '
        f'fill="none" stroke="#333"/>'
        for r in range(house["rows"])
        for c in range(house["cols"])
    ]
    y = pad + grid_h + 30
    for entry in entries:
        lines.append(
            f'<text x="{pad}" y="{y}" font-family="sans-serif" font-size="14">'
            f"{entry['count']} x {escape(entry['label'])}</text>"
        )
        y += 20
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{y + pad}">'
        f'<text x="{pad}" y="14" font-family="sans-serif" font-size="14">'
        f"{house['storeys']} storey(s), {house['cols']} x {house['rows']} modules</text>"
        + "".join(lines)
        + "</svg>"
    )
