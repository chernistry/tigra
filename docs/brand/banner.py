"""Draw the README banners and the social preview: uv run --with numpy python docs/brand/banner.py OUTDIR

Writes banner-light.svg, banner-dark.svg (1280x400) and social-preview.svg (1280x640). The graph is
invented (fixed seed); its edges are drawn as tapered stripes in the one accent colour. Colours are the
shared visual system's neutrals plus the tigra accent. Render the SVGs to PNG with any browser, e.g.
  playwright screenshot --channel chrome --viewport-size=2560,800 OUTDIR/banner-light.html banner-light.png
(the .html wrappers written next to each SVG show it at 2x).
"""

import math
import random
import sys
from pathlib import Path

import networkx as nx

THEMES = {
    "light": {"page": "#FFFFFF", "ink": "#1F242B", "accent": "#C45A15"},
    "dark": {"page": "#0D1117", "ink": "#E4E8ED", "accent": "#E8833A"},
}
SERIF = "'Source Serif 4', Georgia, serif"
SANS = "Inter, system-ui, sans-serif"
MONO = "'JetBrains Mono', Menlo, Consolas, monospace"
SUMMARY = "321 contacts · 10 communities · 70 you messaged"  # tigra's own output on the synthetic export


COMMUNITIES = ((12, -105), (10, -15), (9, 75), (8, 165))  # (size, direction from "you" in degrees)


def communities(seed: int = 7) -> list[nx.Graph]:
    rnd, out, start = random.Random(seed), [], 0
    for size, _ in COMMUNITIES:
        g = nx.Graph()
        g.add_nodes_from(range(start, start + size))
        g.add_edges_from((a, b) for a in g for b in g if a < b and rnd.random() < 0.3)
        out.append(g)
        start += size
    return out


def layout(groups: list[nx.Graph], box: tuple[float, float, float, float], seed: int = 7) -> dict:
    """Each community as a round cluster around "you" in the middle of the box."""
    x0, y0, w, h = box
    cx, cy, span = x0 + w / 2, y0 + h / 2, min(w, h)
    pos = {"you": (cx, cy)}
    for i, (g, (size, angle)) in enumerate(zip(groups, COMMUNITIES, strict=True)):
        local = nx.spring_layout(g, seed=seed + i, k=0.9, iterations=200)
        radius = span * 0.19 * math.sqrt(size / 10)
        ax, ay = cx + span * 0.32 * math.cos(math.radians(angle)), cy + span * 0.32 * math.sin(math.radians(angle))
        far = max(math.hypot(*p) for p in local.values()) or 1.0
        pos |= {n: (ax + p[0] / far * radius, ay + p[1] / far * radius) for n, p in local.items()}
    return pos


def stripe(a: tuple, b: tuple, width: float, bow: float) -> str:
    """A tapered stripe from a to b: pointed ends, widest in the middle, slightly bowed."""
    (x1, y1), (x2, y2) = a, b
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy) or 1.0
    nx_, ny_ = -dy / length, dx / length
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    c1 = (mx + nx_ * (bow + width), my + ny_ * (bow + width))
    c2 = (mx + nx_ * (bow - width), my + ny_ * (bow - width))
    return (
        f'<path d="M{x1:.1f},{y1:.1f} Q{c1[0]:.1f},{c1[1]:.1f} {x2:.1f},{y2:.1f} '
        f'Q{c2[0]:.1f},{c2[1]:.1f} {x1:.1f},{y1:.1f}Z"/>'
    )


def network(t: dict, box: tuple, scale: float) -> str:
    groups = communities()
    pos = layout(groups, box)
    you = pos["you"]
    edges = [e for g in groups for e in sorted(g.edges())]
    for g in groups:  # "you" reaches each community through its two nearest members
        near = sorted(g, key=lambda n: math.hypot(pos[n][0] - you[0], pos[n][1] - you[1]))[:2]
        edges += [("you", n) for n in near]
    rnd = random.Random(11)
    stripes = []
    for a, b in edges:
        length = math.hypot(pos[b][0] - pos[a][0], pos[b][1] - pos[a][1])
        bow = length * rnd.uniform(0.08, 0.18) * rnd.choice((-1, 1))
        stripes.append(stripe(pos[a], pos[b], 4.2 * scale, bow))
    dots = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{(8.5 if n == "you" else 4.6) * scale:.1f}"/>'
        for n, (x, y) in sorted(pos.items(), key=lambda item: str(item[0]))
    )
    return (
        f'<g fill="{t["accent"]}">{"".join(stripes)}</g>'
        f'<g fill="{t["ink"]}" stroke="{t["page"]}" stroke-width="{2 * scale:.1f}">{dots}</g>'
    )


def banner(t: dict, w: int, h: int) -> str:
    social = h > 500
    pad = 72 if social else 56
    gx = w * (0.6 if social else 0.62)
    net = network(t, (gx, pad * 0.8, w - gx - pad, h - pad * 1.6), 1.25 if social else 1.0)
    word, claim, proof = (120, 30, 21) if social else (88, 24, 17)
    top = h * (0.36 if social else 0.38)
    lines = [
        f'<text x="{pad}" y="{top:.0f}" font-family="{SERIF}" font-weight="600" font-size="{word}">tigra</text>',
        f'<text x="{pad}" y="{top + claim * 2.1:.0f}" font-family="{SANS}" font-size="{claim}">'
        "Your LinkedIn export as a map of the people you know.</text>",
        f'<text x="{pad}" y="{top + claim * 3.5:.0f}" font-family="{SANS}" font-size="{claim}">'
        "It runs on your computer. Nothing is uploaded.</text>",
        f'<text x="{pad}" y="{h - pad - proof * 1.6:.0f}" font-family="{MONO}" font-size="{proof}">'
        "$ tigra build export.zip</text>",
        f'<text x="{pad}" y="{h - pad:.0f}" font-family="{MONO}" font-size="{proof}">{SUMMARY}</text>',
    ]
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" '
        'aria-label="tigra: a map of the people you know, built from your LinkedIn export on your computer">'
        f'<rect width="{w}" height="{h}" fill="{t["page"]}"/>{net}'
        f'<g fill="{t["ink"]}">{"".join(lines)}</g></svg>'
    )


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    jobs = [
        ("banner-light", "light", 1280, 400),
        ("banner-dark", "dark", 1280, 400),
        ("social-preview", "light", 1280, 640),
    ]
    for name, theme, w, h in jobs:
        svg = banner(THEMES[theme], w, h)
        (out / f"{name}.svg").write_text(svg, encoding="utf-8")
        scale = 2 if h < 500 else 1
        (out / f"{name}.html").write_text(
            f'<!doctype html><meta charset="utf-8"><style>html,body{{margin:0;background:{THEMES[theme]["page"]}}}'
            f"svg{{display:block;width:{w * scale}px;height:{h * scale}px}}</style>{svg}",
            encoding="utf-8",
        )
        print(name, f"{w * scale}x{h * scale}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
