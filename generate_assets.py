from PIL import Image, ImageDraw, ImageFont
import os

os.makedirs("assets", exist_ok=True)

W, H = 1600, 500
img = Image.new("RGB", (W, H), "#0b1220")
draw = ImageDraw.Draw(img)

# الخط الافتراضي فقط
font = ImageFont.load_default()

draw.text((60, 40), "GOHAR DL ULTRA", fill="white", font=font)
draw.text((60, 80), "Supported Platforms", fill="gray", font=font)

platforms = [
    ("YouTube", "#FF0000"),
    ("TikTok", "#111111"),
    ("Instagram", "#C13584"),
    ("Facebook", "#1877F2"),
    ("X", "#FFFFFF"),
]

start_x = 60
y = 150
box_w = 260
box_h = 200
gap = 25

for i, (name, color) in enumerate(platforms):
    x = start_x + i * (box_w + gap)

    draw.rectangle(
        [x, y, x + box_w, y + box_h],
        fill="#111827",
        outline="#374151",
        width=3
    )

    draw.ellipse(
        [x + 90, y + 20, x + 170, y + 100],
        fill=color
    )

    draw.text(
        (x + 90, y + 130),
        name,
        fill="white",
        font=font
    )

img.save("assets/platforms_banner.png")
print("Banner created successfully")

