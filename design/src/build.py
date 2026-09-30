"""Builds ../mockup-v1.html from mockup.template.html: inlines the four woff2 subsets and before.jpg as data URIs
(a file:// page can't load fonts from a neighbouring file in Chrome, and the Artifact CSP blocks font CDNs)."""
import base64, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "mockup-v1.html")

def b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")

subs = {
    "RUBIK_HE": b64(os.path.join(HERE, "fonts", "Rubik-hebrew.woff2")),
    "RUBIK_LAT": b64(os.path.join(HERE, "fonts", "Rubik-latin.woff2")),
    "ORB_LAT": b64(os.path.join(HERE, "fonts", "Orbitron-latin.woff2")),
    "STM_LAT": b64(os.path.join(HERE, "fonts", "ShareTechMono-latin.woff2")),
    "BEFORE_IMG": b64(os.path.join(HERE, "before.jpg")),
}
html = open(os.path.join(HERE, "mockup.template.html"), encoding="utf-8").read()
for k, v in subs.items():
    token = "{{" + k + "}}"
    assert token in html, token
    html = html.replace(token, v)
assert "{{" not in html, "unreplaced placeholder"
with open(OUT, "w", encoding="utf-8", newline="\n") as f:
    f.write(html)
print("wrote", os.path.normpath(OUT), round(len(html) / 1024), "KB")
