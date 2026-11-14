# One-time build step: rasterize glyphs into src/atlas.bin, which the Rust program embeds.
# Usage: python build_atlas.py [OUT]   (fonts from %TEXTIMG_FONTS%, default C:\Windows\Fonts; needs Pillow)
# Two faces: P (prose) = Verdana 14px; M (fixed width, for code blocks, tables, diagrams and the lines layout) =
# Consolas 14px with every glyph on a fixed cell (East Asian wide characters take two cells). A character a face
# lacks comes from the first fallback font whose cmap has it, shifted to that face's baseline. Characters no font
# has, and right-to-left or shaped scripts (Arabic, Hebrew, Indic, Thai), are left out: textimg draws them as ?
# and names the lines so they can be read as text.
# Format (little endian): b'TXA3', u16 line_height, u32 pool_len, pool (deduplicated coverage bitmaps, 0..255),
# then per face: u32 count and per glyph: u32 codepoint, f32 advance, i16 left, i16 top, u16 w, u16 h, u32 pool_offset
import struct, sys, os, unicodedata
from PIL import Image, ImageDraw, ImageFont

FD = os.environ.get('TEXTIMG_FONTS', r'C:\Windows\Fonts')   # a folder holding the fonts named below
CJK = ['msyh.ttc', 'malgun.ttf']
FACES = [('verdana.ttf', ['segoeui.ttf', 'seguisym.ttf', 'arial.ttf', 'consola.ttf', 'cambria.ttc', 'lucon.ttf', 'tahoma.ttf'] + CJK + ['seguiemj.ttf'], False),
         ('consola.ttf', ['seguisym.ttf', 'segoeui.ttf', 'lucon.ttf', 'arial.ttf', 'cambria.ttc'] + CJK + ['seguiemj.ttf'], True)]
SIZE = 14; LH = 17; PAD = 10
RANGES = [(0x20, 0x7E), (0xA0, 0x2FF), (0x370, 0x3FF), (0x400, 0x52F), (0x1E00, 0x1FFF), (0x2000, 0x206F),
          (0x2070, 0x209F), (0x20A0, 0x20CF), (0x2100, 0x218F), (0x2190, 0x21FF), (0x2200, 0x22FF), (0x2300, 0x23FF),
          (0x2400, 0x243F), (0x2460, 0x24FF), (0x2500, 0x25FF), (0x2600, 0x26FF), (0x2700, 0x27BF), (0x27C0, 0x27FF),
          (0x2800, 0x28FF), (0x2900, 0x297F), (0x2980, 0x2AFF), (0x2B00, 0x2BFF), (0x3000, 0x30FF), (0x3130, 0x318F),
          (0x31F0, 0x31FF), (0x3200, 0x32FF), (0x4E00, 0x9FFF), (0xAC00, 0xD7A3), (0xFB00, 0xFB06), (0xFE30, 0xFE4F),
          (0xFF00, 0xFFEF), (0xFFFD, 0xFFFD), (0x1F100, 0x1F1FF), (0x1F300, 0x1F64F), (0x1F680, 0x1F6FF),
          (0x1F900, 0x1F9FF)]
SKIP = set(range(0x200B, 0x2010)) | set(range(0x2028, 0x202F)) | set(range(0x2060, 0x2070)) | {0xAD}   # invisible format chars


def cmap(path):
    """Code points the font maps to a real glyph (font 0 of a collection, like Pillow), from its Unicode cmap."""
    b = open(path, 'rb').read()
    off = struct.unpack('>I', b[12:16])[0] if b[:4] == b'ttcf' else 0
    num = struct.unpack('>H', b[off + 4:off + 6])[0]
    tables = {struct.unpack('>4s', b[off + 12 + 16 * i:off + 16 + 16 * i])[0]: struct.unpack('>I', b[off + 20 + 16 * i:off + 24 + 16 * i])[0] for i in range(num)}
    c = tables[b'cmap']
    subs = {}
    for i in range(struct.unpack('>H', b[c + 2:c + 4])[0]):
        pid, eid, so = struct.unpack('>HHI', b[c + 4 + 8 * i:c + 12 + 8 * i])
        subs[(pid, eid)] = c + so
    cps = set()
    for key in [(3, 10), (0, 4), (0, 6), (3, 1), (0, 3), (0, 1)]:
        if key not in subs: continue
        s = subs[key]; fmt = struct.unpack('>H', b[s:s + 2])[0]
        if fmt == 12:
            for g in range(struct.unpack('>I', b[s + 12:s + 16])[0]):
                a, e, gid = struct.unpack('>III', b[s + 16 + 12 * g:s + 28 + 12 * g])
                cps.update(range(a + (gid == 0), e + 1))
            return cps
        if fmt == 4:
            x2 = struct.unpack('>H', b[s + 6:s + 8])[0]; n = x2 // 2
            ends = struct.unpack(f'>{n}H', b[s + 14:s + 14 + x2])
            starts = struct.unpack(f'>{n}H', b[s + 16 + x2:s + 16 + 2 * x2])
            deltas = struct.unpack(f'>{n}h', b[s + 16 + 2 * x2:s + 16 + 3 * x2])
            rp = s + 16 + 3 * x2
            ros = struct.unpack(f'>{n}H', b[rp:rp + x2])
            for i in range(n):
                for cp in range(starts[i], ends[i] + 1):
                    if cp == 0xFFFF: continue
                    if ros[i] == 0: gid = (cp + deltas[i]) & 0xFFFF
                    else:
                        gp = rp + 2 * i + ros[i] + 2 * (cp - starts[i])
                        gid = struct.unpack('>H', b[gp:gp + 2])[0]
                        if gid: gid = (gid + deltas[i]) & 0xFFFF
                    if gid: cps.add(cp)
            return cps
    return cps


def load(name):
    p = os.path.join(FD, name)
    if not os.path.exists(p): print('missing font', name); return None
    f = ImageFont.truetype(p, SIZE, layout_engine=ImageFont.Layout.BASIC)
    return f, cmap(p), f.getmetrics()[0], name


pool = bytearray(); pool_at = {}
def put(bits):
    k = bytes(bits)
    if k not in pool_at: pool_at[k] = len(pool); pool.extend(k)
    return pool_at[k]

faces = []
for primary, fallback, mono in FACES:
    fonts = [x for x in [load(primary)] + [load(n) for n in fallback] if x]
    base_asc = fonts[0][2]; cell = fonts[0][0].getlength('M')
    body = []; used = {}
    for a, b in RANGES:
        for cp in range(a, b + 1):
            if cp in SKIP or unicodedata.category(chr(cp)) in ('Cn', 'Cs', 'Co', 'Cc'): continue
            ch = chr(cp)
            pick = next(((f, asc, nm) for f, cps, asc, nm in fonts if cp in cps or ch == ' '), None)
            if pick is None: continue
            f, asc, nm = pick
            used[nm] = used.get(nm, 0) + 1
            wide = unicodedata.east_asian_width(ch) in ('W', 'F')
            adv = cell * (2 if wide else 1) if mono else f.getlength(ch)
            im = Image.new('L', (SIZE * 4 + 2 * PAD, LH + 2 * PAD + 8), 0); d = ImageDraw.Draw(im)
            x0 = PAD
            if mono and f is not fonts[0][0]: x0 = PAD + int(round((adv - f.getlength(ch)) / 2))  # centre fallback glyphs in their cells
            d.text((x0, PAD + (base_asc - asc)), ch, font=f, fill=255)
            bb = im.getbbox()
            if bb is None:
                body.append((cp, adv, 0, 0, 0, 0, 0)); continue
            bx0, by0, bx1, by1 = bb
            body.append((cp, adv, bx0 - PAD, by0 - PAD, bx1 - bx0, by1 - by0, put(im.crop(bb).tobytes())))
    faces.append(body)
    print(('mono ' if mono else 'prose'), 'glyphs', len(body), used)

out = [b'TXA3', struct.pack('<HI', LH, len(pool)), bytes(pool)]
for body in faces:
    out.append(struct.pack('<I', len(body)))
    out.extend(struct.pack('<IfhhHHI', *g) for g in body)
data = b''.join(out)
open(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src', 'atlas.bin'), 'wb').write(data)
print('pool', len(pool), 'total', len(data))
