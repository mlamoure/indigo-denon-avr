"""Draw the Denon AVR plugin icon: a stylized AV receiver, no vendor branding. 512x512 PNG."""

import math
import sys

from PIL import Image, ImageDraw, ImageFilter

S = 4  # supersampling factor
W = 512 * S


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(len(a)))


def vertical_gradient(size, top, bottom):
    w, h = size
    img = Image.new("RGBA", size)
    px = img.load()
    for y in range(h):
        c = lerp(top, bottom, y / max(1, h - 1))
        for x in range(w):
            px[x, y] = c
    return img


def rounded_mask(size, radius):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius=radius, fill=255)
    return m


def paste_gradient_rrect(canvas, box, radius, top, bottom):
    x0, y0, x1, y1 = box
    size = (x1 - x0, y1 - y0)
    grad = vertical_gradient(size, top, bottom)
    canvas.paste(grad, (x0, y0), rounded_mask(size, radius))


def knob(canvas, cx, cy, r, angle_deg):
    d = ImageDraw.Draw(canvas)
    # shadow ring
    d.ellipse(
        [cx - r - 3 * S, cy - r - 3 * S, cx + r + 3 * S, cy + r + 3 * S], fill=(12, 14, 18, 255)
    )
    # metallic body: concentric gradient
    steps = 60
    for i in range(steps):
        t = i / steps
        rr = r * (1 - t)
        c = lerp((150, 156, 166, 255), (226, 230, 236, 255), t**0.7)
        d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=c)
    # inner cap
    ri = r * 0.62
    d.ellipse(
        [cx - ri, cy - ri, cx + ri, cy + ri],
        fill=(196, 201, 209, 255),
        outline=(120, 126, 136, 255),
        width=S,
    )
    # indicator
    a = math.radians(angle_deg)
    ix, iy = cx + math.cos(a) * r * 0.75, cy + math.sin(a) * r * 0.75
    rd = r * 0.09
    d.ellipse([ix - rd, iy - rd, ix + rd, iy + rd], fill=(60, 205, 255, 255))


def main(out_path):
    canvas = Image.new("RGBA", (W, W), (0, 0, 0, 0))

    # Tile background
    paste_gradient_rrect(canvas, (0, 0, W, W), int(W * 0.22), (24, 42, 70, 255), (9, 16, 28, 255))

    # Sound waves above the receiver (cyan arcs, glowing)
    waves = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    dw = ImageDraw.Draw(waves)
    cx, cy = W // 2, int(W * 0.47)
    for i, (rad, alpha) in enumerate([(0.17, 255), (0.25, 200), (0.33, 140)]):
        r = int(W * rad)
        dw.arc(
            [cx - r, cy - r, cx + r, cy + r],
            start=232,
            end=308,
            fill=(70, 210, 255, alpha),
            width=int(10 * S),
        )
    glow = waves.filter(ImageFilter.GaussianBlur(10 * S))
    canvas.alpha_composite(glow)
    canvas.alpha_composite(waves)

    # Receiver body
    bx0, bx1 = int(W * 0.10), int(W * 0.90)
    by0, by1 = int(W * 0.46), int(W * 0.78)
    # drop shadow
    shadow = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [bx0, by0 + 10 * S, bx1, by1 + 14 * S], radius=int(W * 0.035), fill=(0, 0, 0, 170)
    )
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(9 * S)))
    paste_gradient_rrect(
        canvas, (bx0, by0, bx1, by1), int(W * 0.035), (78, 84, 94, 255), (38, 42, 48, 255)
    )
    d = ImageDraw.Draw(canvas)
    # top highlight line
    d.line(
        [bx0 + 14 * S, by0 + 3 * S, bx1 - 14 * S, by0 + 3 * S],
        fill=(150, 156, 166, 255),
        width=2 * S,
    )
    # feet
    for fx in (bx0 + int(W * 0.08), bx1 - int(W * 0.08)):
        d.rounded_rectangle(
            [fx - 16 * S, by1, fx + 16 * S, by1 + 9 * S], radius=3 * S, fill=(20, 22, 26, 255)
        )

    bh = by1 - by0
    mid = (by0 + by1) // 2

    # Volume knob (left)
    knob(canvas, bx0 + int(bh * 0.48), mid, int(bh * 0.31), -55)

    # Display (center)
    dx0, dx1 = bx0 + int(bh * 0.92), bx1 - int(bh * 0.92)
    dy0, dy1 = mid - int(bh * 0.22), mid + int(bh * 0.22)
    d.rounded_rectangle(
        [dx0, dy0, dx1, dy1],
        radius=6 * S,
        fill=(6, 18, 24, 255),
        outline=(20, 30, 36, 255),
        width=2 * S,
    )
    bars = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    db = ImageDraw.Draw(bars)
    heights = [0.3, 0.55, 0.8, 0.5, 0.9, 0.65, 0.95, 0.6, 0.4, 0.75, 0.5, 0.35, 0.6, 0.25]
    n = len(heights)
    pad = 10 * S
    span = (dx1 - dx0) - 2 * pad
    bw = span / (n * 1.6)
    for i, h in enumerate(heights):
        x = dx0 + pad + i * (span / n) + (span / n - bw) / 2
        top = dy1 - pad - (dy1 - dy0 - 2 * pad) * h
        db.rounded_rectangle([x, top, x + bw, dy1 - pad], radius=2 * S, fill=(70, 210, 255, 255))
    canvas.alpha_composite(bars.filter(ImageFilter.GaussianBlur(5 * S)))
    canvas.alpha_composite(bars)

    # Input knob and buttons (right)
    rx = bx1 - int(bh * 0.46)
    knob(canvas, rx, mid - int(bh * 0.11), int(bh * 0.18), 200)
    d = ImageDraw.Draw(canvas)
    for i in range(3):
        bxx = rx - int(bh * 0.19) + i * int(bh * 0.19)
        d.rounded_rectangle(
            [bxx - 7 * S, mid + int(bh * 0.24), bxx + 7 * S, mid + int(bh * 0.24) + 6 * S],
            radius=2 * S,
            fill=(110, 116, 126, 255),
        )
    # power LED
    lx, ly = bx0 + int(bh * 0.12), by1 - int(bh * 0.14)
    led = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    ImageDraw.Draw(led).ellipse(
        [lx - 5 * S, ly - 5 * S, lx + 5 * S, ly + 5 * S], fill=(90, 230, 140, 255)
    )
    canvas.alpha_composite(led.filter(ImageFilter.GaussianBlur(4 * S)))
    canvas.alpha_composite(led)

    canvas.resize((512, 512), Image.LANCZOS).save(out_path, optimize=True)


if __name__ == "__main__":
    main(sys.argv[1])
