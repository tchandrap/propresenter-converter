#!/usr/bin/env python3
"""pp2mp4 - turn ProPresenter 7 presentations and playlist items into MP4 and/or PDF.

It reads ProPresenter's files read-only (it never writes into the ProPresenter folder), follows the slide
order (and the selected arrangement), each slide's timer and transition (Dissolve), draws every slide's
image layers and RTF text boxes, plays video cues in full, and writes 1920x1080 / 30 fps H.264.

By presentation name (as before):
  pp2mp4 --list                                  # presentations in your ProPresenter library
  pp2mp4 "Sunday Service"                     # -> "Sunday Service.mp4"
  pp2mp4 "Sunday Service" --library "My Library" --pdf     # mp4 + pdf
  pp2mp4 "Sunday Service" --pdf-only          # just the PDF (fast)

From a playlist (ProPresenter's own playlists, or a .proplaylist file that is not imported):
  pp2mp4 --playlists                             # playlist tree with numbered items
  pp2mp4 --placeholders                          # every placeholder and the playlist it belongs to
  pp2mp4 --playlist "Sunday Playlist"             # items of one playlist
  pp2mp4 --playlist "Sunday Playlist" --item 2 --outdir ~/Desktop/out              # item 2 -> mp4
  pp2mp4 --playlist "Sunday Playlist" --item "Announcements" --pdf-only --outdir out
  pp2mp4 --playlist "Sunday Playlist" --item all --pdf-only --outdir out          # every item
  pp2mp4 --proplaylist "Sunday Playlist.proplaylist" --playlists
  pp2mp4 --proplaylist "Sunday Playlist.proplaylist" --item 2 --preview out/prev  # slide thumbnails + sheet.png

Timing: --hold N (seconds for slides without a timer), --picture-seconds N (replace the timer of picture-only slides),
--no-video-hold (go straight to the next slide after a video).
Other options: --stills DIR (one PNG per slide), --no-video, --pdf-skip-video, --bg FILE (stand-in theme
background behind text-only slides), --json (machine-readable output), --progress (progress lines).

Needs: Python 3, Pillow, numpy, an ffmpeg binary (PATH, or the `imageio-ffmpeg` package), and on macOS the
Xcode command line tools (swiftc) for text rendering.
"""
import argparse, hashlib, json, os, re, shutil, struct, subprocess, sys, tempfile, urllib.parse, zlib
import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1920, 1080, 30
AUD_EXT = (".mp3", ".m4a", ".wav", ".aac", ".aif", ".aiff")
IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic")
VID_EXT = (".mp4", ".mov", ".m4v")
PP_ROOT = os.path.expanduser("~/Library/Application Support/RenewedVision/ProPresenter/UserWorkspaces/ProPresenter")
LIB_ROOT = os.path.join(PP_ROOT, "Libraries")
PLAYLIST_FILE = os.path.join(PP_ROOT, "Playlists", "Library")
CACHE = os.path.expanduser("~/Library/Caches/pp2mp4")      # media unpacked from .proplaylist files go here
PROGRESS = False


def progress(frac, stage):
    if PROGRESS:
        print("@@PROGRESS " + json.dumps(dict(done=round(max(0.0, min(1.0, frac)), 4), stage=stage)), flush=True)


# ---------------------------------------------------------------- protobuf (schema-less) reader
def _varint(b, i):
    r = s = 0
    while True:
        x = b[i]; i += 1
        r |= (x & 0x7F) << s; s += 7
        if not x & 0x80:
            return r, i


def parse(b, depth=0, maxd=18):
    out, i = [], 0
    try:
        while i < len(b):
            key, i = _varint(b, i)
            f, w = key >> 3, key & 7
            if f == 0 or f > 4000:
                return None
            if w == 0:
                v, i = _varint(b, i)
            elif w == 1:
                v = struct.unpack("<d", b[i:i + 8])[0]; i += 8
            elif w == 5:
                v = struct.unpack("<f", b[i:i + 4])[0]; i += 4
            elif w == 2:
                n, i = _varint(b, i)
                if i + n > len(b):
                    return None
                raw = b[i:i + n]; i += n
                sub = parse(raw, depth + 1, maxd) if depth < maxd and n > 1 else None
                if sub:
                    v = ("msg", sub)
                else:
                    try:
                        t = raw.decode("utf-8")
                        v = ("str", t) if all(c.isprintable() or c in "\n\r\t" for c in t) else ("raw", raw)
                    except Exception:
                        v = ("raw", raw)
            else:
                return None
            out.append((f, w, v))
    except Exception:
        return None
    return out


def msg(v): return v[1] if isinstance(v, tuple) and v[0] == "msg" else None
def sval(v): return v[1] if isinstance(v, tuple) and v[0] == "str" else None


def first(n, f, d=None):
    for ff, w, v in (n or []):
        if ff == f:
            return v
    return d


def fields(n, f): return [v for ff, w, v in (n or []) if ff == f]


def get(n, *path):
    cur = n
    for f in path:
        nxt = msg(first(cur, f))
        if nxt is None:
            return None
        cur = nxt
    return cur


def uuid_of(n): return sval(first(get(n, 1), 1))


def rgba_hex(n):
    """A colour message (4 floats) -> '#rrggbb', else None."""
    if not n or len(n) < 3 or not all(isinstance(x[2], float) for x in n[:3]):
        return None
    return "#" + "".join(f"{max(0, min(255, int(round(x[2] * 255)))):02x}" for x in n[:3])


# ---------------------------------------------------------------- .proplaylist bundles (read without importing)
class Bundle:
    """A .proplaylist: a zip (ProPresenter writes a slightly broken central directory, so the local headers are
    read directly) or an unzipped folder. Inside: `data` (the playlist), the .pro files at the top, and the media
    under the absolute paths of the computer that exported it."""

    def __init__(self, path):
        self.path = os.path.abspath(os.path.expanduser(path))
        self.entries = {}
        if os.path.isdir(self.path):
            for root, _, files in os.walk(self.path):
                for fn in files:
                    full = os.path.join(root, fn)
                    self.entries[os.path.relpath(full, self.path)] = full
        else:
            self._scan_zip()
        if "data" not in self.entries:
            sys.exit(f"{path}: no playlist 'data' inside; is this a .proplaylist?")
        self.by_base = {}
        for n in self.entries:
            self.by_base.setdefault(os.path.basename(n).lower(), []).append(n)
        st = os.stat(self.path)
        tag = re.sub(r"[^A-Za-z0-9]+", "_", os.path.basename(self.path))[:60]
        self.cache = os.path.join(CACHE, "bundles", f"{tag}-{int(st.st_mtime)}")

    def _scan_zip(self):
        size = os.path.getsize(self.path)
        with open(self.path, "rb") as f:
            off = 0
            while off + 30 <= size:
                f.seek(off)
                h = f.read(30)
                if h[:4] != b"PK\x03\x04":
                    break
                _, flag, meth, _, _, _, cs, us, nl, el = struct.unpack("<HHHHHIIIHH", h[4:30])
                name = f.read(nl).decode("utf-8", "replace")
                extra = f.read(el)
                i = 0
                while i + 4 <= len(extra):                       # ZIP64 sizes
                    hid, ln = struct.unpack("<HH", extra[i:i + 4])
                    if hid == 1 and (cs == 0xFFFFFFFF or us == 0xFFFFFFFF):
                        vals = list(struct.unpack("<" + "Q" * (ln // 8), extra[i + 4:i + 4 + (ln // 8) * 8]))
                        if us == 0xFFFFFFFF and vals:
                            us = vals.pop(0)
                        if cs == 0xFFFFFFFF and vals:
                            cs = vals.pop(0)
                    i += 4 + ln
                if flag & 8 and cs == 0:
                    sys.exit(f"{self.path}: zip entries without sizes are not supported")
                data_off = off + 30 + nl + el
                if not name.endswith("/"):
                    self.entries[name.lstrip("/")] = (data_off, cs, meth)
                off = data_off + cs + (16 if flag & 8 else 0)

    def read(self, name):
        e = self.entries[name]
        if isinstance(e, str):
            return open(e, "rb").read()
        off, cs, meth = e
        with open(self.path, "rb") as f:
            f.seek(off)
            raw = f.read(cs)
        return zlib.decompress(raw, -15) if meth == 8 else raw

    def extract(self, name):
        """-> a real file path (needed by ffmpeg / Pillow). Zip entries are unpacked once into the cache."""
        e = self.entries[name]
        if isinstance(e, str):
            return e
        out = os.path.join(self.cache, name)
        if not os.path.exists(out) or os.path.getsize(out) == 0:
            os.makedirs(os.path.dirname(out), exist_ok=True)
            off, cs, meth = e
            tmp = out + ".part"
            with open(self.path, "rb") as f, open(tmp, "wb") as o:
                f.seek(off)
                if meth == 8:
                    o.write(zlib.decompress(f.read(cs), -15))
                else:
                    left = cs
                    while left:
                        b = f.read(min(left, 8 << 20))
                        if not b:
                            break
                        o.write(b); left -= len(b)
            os.replace(tmp, out)
        return out

    def find(self, ref):
        """Find the entry for a path / URL the playlist or a .pro refers to."""
        if not ref:
            return None
        if ref.startswith("file://"):
            ref = urllib.parse.unquote(ref[7:])
        r = ref.lstrip("/")
        if r in self.entries:
            return r
        hits = self.by_base.get(os.path.basename(ref).lower(), [])
        if not hits:
            return None
        hits.sort(key=lambda n: (n.count("/"), len(n)))           # a .pro sits at the top of the bundle
        return hits[0]


_BUNDLE = None          # the bundle that media references are looked up in while reading its presentations


# ---------------------------------------------------------------- locating presentations by name
def all_presentations():
    for root, _, files in os.walk(LIB_ROOT):
        if ".proplaylist" in root:
            continue
        for fn in files:
            if fn.endswith(".pro"):
                yield os.path.join(root, fn)


def find_presentation(name, library=None):
    hits = [p for p in all_presentations() if os.path.splitext(os.path.basename(p))[0].lower() == name.lower()]
    if library:
        hits = [p for p in hits if library.lower() in os.path.dirname(p).lower()]
    if not hits:
        hits = [p for p in all_presentations() if name.lower() in os.path.basename(p).lower()
                and (not library or library.lower() in os.path.dirname(p).lower())]
    if not hits:
        sys.exit(f"No presentation matching '{name}'. Try --list.")
    hits.sort(key=os.path.getmtime, reverse=True)
    if len(hits) > 1:
        print(f"[note] {len(hits)} matches; using the newest: {os.path.relpath(hits[0], LIB_ROOT)}", file=sys.stderr)
    return hits[0]


# ---------------------------------------------------------------- reading cues
def media_refs(node):
    found = []

    def walk(n):
        for f, w, v in n:
            if msg(v):
                walk(msg(v))
            elif isinstance(v, tuple) and v[0] == "str":
                s = v[1]
                if s.startswith("file://"):
                    found.append(urllib.parse.unquote(s[7:]))
                elif re.match(r"^Media/.+\.\w{2,4}$", s):
                    found.append(os.path.join(PP_ROOT, s))
    walk(node or [])
    return found


def resolve_media(p):
    if os.path.exists(p):
        return p
    if _BUNDLE is not None:
        n = _BUNDLE.find(p)
        if n:
            return _BUNDLE.extract(n)
    local = os.path.join(PP_ROOT, "Media", "Assets", os.path.basename(p))
    return local if os.path.exists(local) else None


def media_path(node):
    """Find a media file referenced somewhere inside `node`; returns an absolute path or None."""
    found = media_refs(node)
    for p in found:
        r = resolve_media(p)
        if r:
            return r
    return found[0] if found else None


def element_info(wrapper):
    e = msg(first(wrapper, 1))
    if not e:
        return None
    b = msg(first(e, 3))
    if not b or len(b) < 2:
        return None
    o, s = msg(b[0][2]), msg(b[1][2])
    x, y = (o[0][2], o[1][2]) if o and len(o) >= 2 else (0, 0)
    w, h = (s[0][2], s[1][2]) if s and len(s) >= 2 else (0, 0)
    rtf = None
    t = msg(first(e, 13))
    if t:
        for f, ww, v in t:
            if f == 5 and sval(v) and sval(v).startswith("{\\rtf"):
                rtf = sval(v)
    fill = msg(first(e, 9))
    color = None
    col = msg(first(fill, 1)) if fill else None
    if col and len(col) == 4:
        color = [c[2] for c in col]
    return dict(name=sval(first(e, 2)) or "", x=x, y=y, w=w, h=h, opacity=first(e, 5, 1.0) or 1.0,
                rtf=rtf, valign=int(first(t, 6, 0) or 0) if t else 0, color=color, media=media_path(fill) if fill else None)


def read_cue(c):
    """One cue (slide, media, sound, or empty) -> dict."""
    item = dict(name=sval(first(c, 2)) or "", timer=float(first(c, 13, 0.0) or 0.0))
    actions = [msg(v) for v in fields(c, 10)]             # a cue can hold several actions (slide, media, ...)
    action, media_act, slide = None, None, None
    for a_ in actions:
        if media_act is None and get(a_, 20) is not None:
            media_act, action = get(a_, 20), a_
        if slide is None and get(a_, 23, 2) is not None:
            slide = get(a_, 23, 2)
    audio = None
    if media_act is not None:
        mp_ = media_path(media_act)
        if mp_ and os.path.splitext(mp_)[1].lower() in AUD_EXT:
            audio, media_act = mp_, None                  # sound only: it plays alongside the slide (or alone)
    item["audio"] = audio
    if media_act is None and slide is not None:
        els = get(slide, 1)
        item["kind"] = "slide"
        bg = msg(first(els, 5))
        item["bg"] = [x[2] for x in bg] if bg and len(bg) >= 3 else [1, 1, 1, 1]
        item["elements"] = [i for i in (element_info(msg(k)) for k in fields(els, 1)) if i]
        tr = get(slide, 5)
        item["transition"] = float(first(tr, 1, 0.0) or 0.0) if tr else 0.0
    elif media_act is None and audio:
        item["kind"] = "audio"
    elif media_act is None:
        item["kind"] = "empty"
    else:
        item["kind"] = "media"
        item["media"] = media_path(media_act)
        # text typed on top of a video / picture cue lives in the cue's slide action
        item["overlay"] = [e for e in (element_info(msg(k)) for k in fields(get(slide, 1), 1)) if e and e["rtf"] and rtf_plain(e["rtf"])] \
            if slide is not None else []
        dur = first(action, 8)
        item["duration"] = float(dur) if isinstance(dur, float) and dur > 0 else None
    return item


def read_presentation(src):
    """`src` is a path or the bytes of a .pro file -> slides in playback order.
    The selected arrangement (top field 10 -> one of field 11) decides the group order when there is one."""
    tree = parse(src if isinstance(src, (bytes, bytearray)) else open(src, "rb").read())
    if tree is None:
        sys.exit("Could not read the presentation file.")
    cues = {}
    for f, w, v in tree:
        if f == 13:
            c = msg(v)
            cues[uuid_of(c)] = c
    groups = {}
    order = []
    for g in fields(tree, 12):
        g = msg(g)
        gi = msg(first(g, 1))
        gid = uuid_of(gi) if gi else None
        info = dict(group=(sval(first(gi, 2)) or "") if gi else "", color=rgba_hex(get(gi, 3)) if gi else None,
                    cues=[sval(first(msg(x), 1)) for x in fields(g, 2)])
        groups[gid] = info
        order.append(gid)
    sel = sval(first(get(tree, 10), 1))
    for a in fields(tree, 11):
        a = msg(a)
        if sel and uuid_of(a) == sel:
            arr = [sval(first(msg(x), 1)) for x in fields(a, 3)]
            if arr and all(g in groups for g in arr):
                order = arr
    slides = []
    for gid in order:
        g = groups[gid]
        for cid in g["cues"]:
            if cid not in cues:
                continue
            item = read_cue(cues[cid])
            item["group"], item["group_color"] = g["group"], g["color"]
            slides.append(item)
    return slides


# ---------------------------------------------------------------- playlists
KIND_NAMES = {3: "header", 4: "presentation", 5: "media", 8: "placeholder"}


def pres_ref(n):
    """A presentation reference inside a playlist item -> dict(rel, url)."""
    if n is None:
        return None
    doc = get(n, 1)
    return dict(rel=sval(first(get(doc, 4), 2)), url=sval(first(doc, 1)))


def item_info(it, idx):
    d = dict(index=idx, uuid=uuid_of(it), name=(sval(first(it, 2)) or "").strip() or "(no name)")
    if first(it, 4) is not None:
        d.update(kind="presentation", ref=pres_ref(get(it, 4)))
    elif first(it, 8) is not None:
        linked = msg(first(get(it, 8), 1)) if get(it, 8) else None
        d.update(kind="placeholder", ref=pres_ref(get(linked, 4)) if linked else None,
                 linked_name=(sval(first(linked, 2)) or "") if linked else None)
    elif first(it, 5) is not None:
        cue = get(it, 5)
        refs = media_refs(cue)
        d.update(kind="media", cue=cue, media_name=os.path.basename(refs[0]) if refs else None)
    elif first(it, 3) is not None:
        d.update(kind="header", color=rgba_hex(get(it, 3, 1)))
    else:
        d.update(kind="unknown")
    return d


def read_playlist_tree(data):
    t = parse(data)
    root = get(t, 3) if t else None
    if root is None:
        sys.exit("Could not read the playlist file.")

    def node(n, path):
        name = sval(first(n, 2)) or ""
        if first(n, 12) is not None:                      # a folder
            return dict(type="folder", name=name, uuid=uuid_of(n), path=path,
                        children=[node(msg(k), path + [name]) for k in fields(get(n, 12), 1)])
        return dict(type="playlist", name=name, uuid=uuid_of(n), path=path,
                    items=[item_info(msg(k), i) for i, k in enumerate(fields(get(n, 13), 1), 1)])
    return [node(msg(k), []) for k in fields(get(root, 12), 1)]


def iter_playlists(tree):
    for n in tree:
        if n["type"] == "folder":
            yield from iter_playlists(n["children"])
        else:
            yield n


class Source:
    """Where playlists come from: ProPresenter's own library, or a .proplaylist bundle."""

    def __init__(self, proplaylist=None):
        global _BUNDLE
        if proplaylist:
            self.bundle = Bundle(proplaylist)
            _BUNDLE = self.bundle
            self.label = os.path.basename(self.bundle.path)
            self.tree = read_playlist_tree(self.bundle.read("data"))
        else:
            self.bundle = None
            self.label = "ProPresenter library"
            if not os.path.exists(PLAYLIST_FILE):
                sys.exit(f"ProPresenter playlist file not found: {PLAYLIST_FILE}. In SlideBridge open Settings to point it at your ProPresenter folder, or open a .proplaylist file.")
            self.tree = read_playlist_tree(open(PLAYLIST_FILE, "rb").read())

    def playlists(self):
        return list(iter_playlists(self.tree))

    def find_playlist(self, key):
        pls = self.playlists()
        if not key:
            if len(pls) == 1:
                return pls[0]
            sys.exit("Several playlists here; pick one with --playlist (see --playlists).")
        k = key.lower().strip()
        for test in (lambda p: (p["uuid"] or "").lower() == k,
                     lambda p: "/".join(p["path"] + [p["name"]]).lower() == k,
                     lambda p: p["name"].lower().strip() == k,
                     lambda p: k in p["name"].lower()):
            hits = [p for p in pls if test(p)]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                names = ", ".join(f"'{'/'.join(p['path'] + [p['name']])}'" for p in hits)
                sys.exit(f"'{key}' matches several playlists: {names}. Use the full path or the uuid.")
        sys.exit(f"No playlist '{key}' in {self.label}. Try --playlists.")

    def locate(self, ref):
        """A presentation reference -> (label, bytes) or None."""
        if not ref:
            return None
        rel, url = ref.get("rel"), ref.get("url")
        if self.bundle:
            for cand in (rel, url):
                n = self.bundle.find(cand) if cand else None
                if n and n.lower().endswith(".pro"):
                    return f"{self.label}: {n}", self.bundle.read(n)
        cands = []
        if rel:
            cands.append(os.path.join(PP_ROOT, rel))
        if url and url.startswith("file://"):
            cands.append(urllib.parse.unquote(url[7:]))
        for c in cands:
            if os.path.isfile(c):
                return os.path.relpath(c, PP_ROOT) if c.startswith(PP_ROOT) else c, open(c, "rb").read()
        base = os.path.basename(rel or urllib.parse.unquote(url or ""))
        for p in all_presentations():                         # moved or renamed library: match the file name
            if os.path.basename(p).lower() == base.lower():
                return os.path.relpath(p, PP_ROOT) + " (found by name)", open(p, "rb").read()
        return None

    def item_slides(self, it):
        """-> (label, slides) for a playlist item, or exit with a reason a person can act on."""
        if it["kind"] == "header":
            sys.exit(f"Item {it['index']} '{it['name']}' is a header; it has no slides.")
        if it["kind"] == "media":
            return f"media item {it.get('media_name')}", [read_cue(it["cue"])]
        if it["kind"] == "placeholder" and not it.get("ref"):
            sys.exit(f"Item {it['index']} '{it['name']}' is an EMPTY placeholder. " + GUIDE_EMPTY_PLACEHOLDER)
        hit = self.locate(it.get("ref"))
        if not hit:
            sys.exit(f"Item {it['index']} '{it['name']}': presentation file not found "
                     f"({(it.get('ref') or {}).get('rel')}).")
        return hit[0], read_presentation(hit[1])


def item_status(src, it):
    """Short status of an item for listings: does its presentation exist?"""
    if it["kind"] in ("presentation", "placeholder") and it.get("ref"):
        return "ok" if src.locate(it["ref"]) else "missing"
    if it["kind"] == "placeholder":
        return "empty"
    return "ok" if it["kind"] == "media" else ""


GUIDE_EMPTY_PLACEHOLDER = ("In ProPresenter, drag the presentation from the Library onto this placeholder in the "
                           "playlist, then run this again (or click Refresh in the app).")
GUIDE_NEW_PROPLAYLIST = """A .proplaylist that is not in the list?
  1. Easiest: open it directly, nothing is imported:   pp2mp4 --proplaylist "FILE.proplaylist" --playlists
     (in the app: 'Open .proplaylist...').
  2. Or import it into ProPresenter (double-click the file, or drag it onto the Playlists sidebar), let
     ProPresenter save, then list again: it then shows up under the ProPresenter library."""


def item_dict(src, it):
    d = {k: v for k, v in it.items() if k != "cue"}
    d["status"] = item_status(src, it)
    return d


def find_items(pl, keys):
    items = pl["items"]
    if not keys or keys == ["all"]:
        return [it for it in items if it["kind"] != "header"]
    out = []
    for key in keys:
        k = key.strip()
        if k.isdigit() and 1 <= int(k) <= len(items):
            out.append(items[int(k) - 1]); continue
        hits = [it for it in items if (it["uuid"] or "").lower() == k.lower()] or \
               [it for it in items if it["name"].lower().strip() == k.lower()] or \
               [it for it in items if k.lower() in it["name"].lower()]
        hits = [h for h in hits if h["kind"] != "header"] or hits
        if not hits:
            sys.exit(f"No item '{key}' in playlist '{pl['name']}'.")
        if len(hits) > 1:
            print(f"[note] '{key}' matches {len(hits)} items; using #{hits[0]['index']} '{hits[0]['name']}'", file=sys.stderr)
        out.append(hits[0])
    return out


def print_tree(src, nodes, ind=0, only=None):
    for n in nodes:
        if n["type"] == "folder":
            print("  " * ind + f"[folder] {n['name']}")
            print_tree(src, n["children"], ind + 1, only)
            continue
        if only and n is not only:
            continue
        print("  " * ind + f"[playlist] {n['name']}   ({len(n['items'])} items)")
        for it in n["items"]:
            st = item_status(src, it)
            tag = {"header": "--", "presentation": "  ", "media": "MED", "placeholder": "PH ", "unknown": "?? "}[it["kind"]]
            extra = ""
            if it["kind"] == "placeholder":
                extra = "  [placeholder: EMPTY]" if st == "empty" else f"  [placeholder -> {it.get('linked_name')}]"
            if st == "missing":
                extra += "  [file missing]"
            if it["kind"] == "header":
                print("  " * ind + f"   {it['index']:3d}  -- {it['name']} --")
            else:
                print("  " * ind + f"   {it['index']:3d}  {tag} {it['name']}{extra}")


def placeholders(src):
    out = []
    for pl in src.playlists():
        for it in pl["items"]:
            if it["kind"] == "placeholder":
                out.append(dict(playlist="/".join(pl["path"][1:] + [pl["name"]]) if pl["path"] else pl["name"],
                                playlist_uuid=pl["uuid"], index=it["index"], name=it["name"],
                                linked=it.get("linked_name"), status=item_status(src, it),
                                file=(it.get("ref") or {}).get("rel"), source=src.label))
    return out


# ---------------------------------------------------------------- drawing
SWIFT_SRC = r'''
import AppKit
let a = CommandLine.arguments
let data = try! Data(contentsOf: URL(fileURLWithPath: a[1]))
let attr = try! NSAttributedString(data: data, options: [.documentType: NSAttributedString.DocumentType.rtf], documentAttributes: nil)
let w = Int(a[2])!, h = Int(a[3])!
let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: w, pixelsHigh: h, bitsPerSample: 8, samplesPerPixel: 4,
                           hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
// a[5]: vertical alignment of the text box in ProPresenter (0 top, 1 middle, 2 bottom)
let va = a.count > 5 ? (Int(a[5]) ?? 0) : 0
let bh = ceil(attr.boundingRect(with: NSSize(width: CGFloat(w), height: 100000), options: [.usesLineFragmentOrigin]).height)
let oy: CGFloat = va == 1 ? (CGFloat(h) - bh) / 2 : (va == 2 ? 0 : CGFloat(h) - bh)
attr.draw(with: NSRect(x: 0, y: oy, width: CGFloat(w), height: bh), options: [.usesLineFragmentOrigin])
NSGraphicsContext.restoreGraphicsState()
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: a[4]))
'''
_swift_bin = None


def swift_helper():
    global _swift_bin
    if _swift_bin:
        return _swift_bin
    bundled = os.environ.get("PP2MP4_RTF2PNG")            # the packaged app ships a precompiled renderer (no Xcode tools needed)
    if bundled and os.path.exists(bundled):
        _swift_bin = bundled
        return bundled
    d = os.path.join(tempfile.gettempdir(), "pp2mp4_swift")
    os.makedirs(d, exist_ok=True)
    tag = hashlib.sha1(SWIFT_SRC.encode()).hexdigest()[:8]      # a new renderer source gets a new binary
    src, binp = os.path.join(d, f"rtf2png-{tag}.swift"), os.path.join(d, f"rtf2png-{tag}")
    if not os.path.exists(binp):
        open(src, "w").write(SWIFT_SRC)
        print("[setup] compiling the text renderer (once)...", file=sys.stderr)
        subprocess.check_call(["swiftc", "-O", src, "-o", binp])
    _swift_bin = binp
    return binp


def render_text(rtf, w, h, valign=0):
    w, h = max(2, int(round(w))), max(2, int(round(h)))
    with tempfile.TemporaryDirectory() as td:
        rp, op = os.path.join(td, "t.rtf"), os.path.join(td, "t.png")
        open(rp, "w", encoding="latin-1", errors="replace").write(rtf)
        subprocess.check_call([swift_helper(), rp, str(w), str(h), op, str(int(valign))])
        return Image.open(op).convert("RGBA")


def paste_clipped(canvas, layer, x, y):
    big = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    big.paste(layer, (x, y))
    canvas.alpha_composite(big)


def draw_slide(sl, bg_media=None):
    """-> dict(below=RGB array, vid=element or None, above=RGBA array or None).
    If a slide has a video layer, `below` holds everything under it and `above` everything over it."""
    r, g, b = [int(round(c * 255)) for c in sl["bg"][:3]]
    below = Image.new("RGBA", (W, H), (r, g, b, 255))
    above, vid, target = None, None, below
    if bg_media:                                       # stand-in for the theme background ProPresenter would show
        if os.path.splitext(bg_media)[1].lower() in VID_EXT:
            vid = dict(name="--bg", media=bg_media, x=0, y=0, w=W, h=H)
            below = Image.new("RGBA", (W, H), (0, 0, 0, 255))
            above = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            target = above
        else:
            below = fit_on_black(bg_media).convert("RGBA")
            target = below
    for el in reversed(sl["elements"]):                # ProPresenter lists the top layer first
        x, y, w, h = int(round(el["x"])), int(round(el["y"])), int(round(el["w"])), int(round(el["h"]))
        if w <= 0 or h <= 0:
            continue
        ext = os.path.splitext(el["media"] or "")[1].lower()
        if ext in VID_EXT:
            if vid is None and os.path.exists(el["media"]):
                vid = el
                above = Image.new("RGBA", (W, H), (0, 0, 0, 0))
                target = above
            elif vid is None:
                print(f"[warn] video not found: {el['media']}", file=sys.stderr)
            continue
        layer = None
        if ext in IMG_EXT:
            try:
                layer = Image.open(el["media"]).convert("RGBA").resize((w, h), Image.LANCZOS)
            except Exception as ex:
                print(f"[warn] {el['name']}: {ex}", file=sys.stderr)
        elif el["color"] and el["color"][3] > 0 and not el["rtf"]:
            layer = Image.new("RGBA", (w, h), tuple(int(round(c * 255)) for c in el["color"]))
        if layer is not None:
            if el["opacity"] < 0.999:
                layer.putalpha(layer.getchannel("A").point(lambda v, o=el["opacity"]: int(v * o)))
            paste_clipped(target, layer, x, y)
        if el["rtf"]:
            try:
                paste_clipped(target, render_text(el["rtf"], w, h, el.get("valign", 0)), x, y)
            except Exception as ex:
                print(f"[warn] text '{el['name']}': {ex}", file=sys.stderr)
    return dict(below=np.asarray(below.convert("RGB")), vid=vid, above=(np.asarray(above) if above is not None else None))


def needs_bg(sl):
    """A slide that is only text on a plain white background: its real background comes from a ProPresenter theme."""
    els = sl["elements"]
    big = lambda e: e["w"] * e["h"] >= 0.4 * W * H      # a full-slide picture / fill counts as a background; a small logo does not
    return (bool(els) and all(c > 0.98 for c in sl["bg"][:3]) and any(e["rtf"] for e in els)
            and not any(big(e) and (e["media"] or (e["color"] and e["color"][3] > 0 and not e["rtf"])) for e in els))


def text_is_light(sl):
    """True when every text colour in the slide is (nearly) white: such text needs a dark background."""
    cols = []
    for e in sl["elements"]:
        if e["rtf"]:
            m_ = re.search(r"\\colortbl(.*?)\}", e["rtf"], re.S)
            for r_, g_, b_ in re.findall(r"\\red(\d+)\\green(\d+)\\blue(\d+)", m_.group(1) if m_ else ""):
                cols.append((int(r_) + int(g_) + int(b_)) / 765)
    return bool(cols) and sum(cols) / len(cols) > 0.6      # a black outline colour in the table must not hide white fill text


def rtf_plain(rtf, n=40):
    """First line of readable text from an RTF string (for slide labels)."""
    out, i, depth, skip = [], 0, 0, None
    while i < len(rtf):
        c = rtf[i]
        if c == "{":
            depth += 1
            if skip is None and re.match(r"\{\\(?:\*\\)?(fonttbl|colortbl|expandedcolortbl|stylesheet|info)", rtf[i:i + 30]):
                skip = depth
        elif c == "}":
            if skip == depth:
                skip = None
            depth -= 1
        elif c == "\\":
            m_ = re.match(r"\\'([0-9a-f]{2})", rtf[i:])
            if m_:
                if skip is None:
                    out.append(bytes.fromhex(m_.group(1)).decode("cp1252", "replace"))
                i += 4
                continue
            m_ = re.match(r"\\([a-zA-Z]+)-?\d* ?", rtf[i:])
            if m_:
                if skip is None and m_.group(1) in ("par", "line"):
                    out.append("\n")
                i += m_.end()
                continue
            i += 2
            continue
        elif skip is None:
            out.append(c)
        i += 1
    for line in "".join(out).splitlines():
        if line.strip(" ;"):
            return line.strip(" ;")[:n]
    return ""


STANDIN_BG = [0.043, 0.086, 0.19, 1.0]       # dark navy, used when a text-only slide's theme background is not in the file


def fit_on_black(path):
    im = Image.open(path).convert("RGB")
    s = min(W / im.width, H / im.height)
    im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.LANCZOS)
    c = Image.new("RGB", (W, H), (0, 0, 0))
    c.paste(im, ((W - im.width) // 2, (H - im.height) // 2))
    return c


# ---------------------------------------------------------------- video assembly
def ffmpeg_bin():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def probe(path):
    """-> (duration seconds or None, has_audio)"""
    t = subprocess.run([ffmpeg_bin(), "-i", path], capture_output=True, text=True).stderr
    m_ = re.search(r"Duration: (\d+):(\d+):([\d.]+)", t)
    dur = int(m_.group(1)) * 3600 + int(m_.group(2)) * 60 + float(m_.group(3)) if m_ else None
    return dur, "Audio:" in t


def video_frames(path, limit_s=None):
    cmd = [ffmpeg_bin(), "-loglevel", "error", "-i", path] + (["-t", str(limit_s)] if limit_s else []) + [
        "-vf", f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2",
        "-r", str(FPS), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    fb = W * H * 3
    while True:
        b = p.stdout.read(fb)
        if len(b) < fb:
            break
        yield np.frombuffer(b, np.uint8).reshape(H, W, 3)
    p.wait()


def poster_frame(path, t):
    """One frame of a video (for a PDF page), letterboxed to 1920x1080."""
    cmd = [ffmpeg_bin(), "-loglevel", "error", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
           "-vf", f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    b = subprocess.run(cmd, capture_output=True).stdout
    return np.frombuffer(b, np.uint8).reshape(H, W, 3) if len(b) == W * H * 3 else None


def save_pdf(pages, path):
    ims = [Image.fromarray(p) for p in pages]
    ims[0].save(path, "PDF", resolution=144.0, save_all=True, append_images=ims[1:], quality=92)


def cover_frames(path, w, h, seconds):
    """Frames of `path` scaled to cover a w x h box (looping if shorter than `seconds`)."""
    cmd = [ffmpeg_bin(), "-loglevel", "error", "-stream_loop", "-1", "-i", path, "-t", f"{seconds:.3f}",
           "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
           "-r", str(FPS), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    fb = w * h * 3
    while True:
        b = p.stdout.read(fb)
        if len(b) < fb:
            break
        yield np.frombuffer(b, np.uint8).reshape(h, w, 3)
    p.kill()
    p.wait()


def compose(d, vf):
    out = d["below"].copy()
    el = d["vid"]
    x, y = int(round(el["x"])), int(round(el["y"]))
    h, w = vf.shape[:2]
    x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    if x1 > x0 and y1 > y0:
        out[y0:y1, x0:x1] = vf[y0 - y:y1 - y, x0 - x:x1 - x]
    if d["above"] is not None:
        al = d["above"][..., 3:4].astype(np.float32) / 255
        out = (out.astype(np.float32) * (1 - al) + d["above"][..., :3].astype(np.float32) * al).astype(np.uint8)
    return out


def overlay_layer(sl):
    """Text on top of a video / picture cue -> RGBA array (H, W, 4), or None when the cue has no text."""
    els = sl.get("overlay") or []
    if not els:
        return None
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    for el in reversed(els):                           # ProPresenter lists the top layer first
        x, y, w, h = int(round(el["x"])), int(round(el["y"])), int(round(el["w"])), int(round(el["h"]))
        if w <= 0 or h <= 0:
            continue
        try:
            paste_clipped(layer, render_text(el["rtf"], w, h, el.get("valign", 0)), x, y)
        except Exception as ex:
            print(f"[warn] text '{el['name']}': {ex}", file=sys.stderr)
    return np.asarray(layer)


def over(frame, layer):
    if layer is None:
        return frame
    al = layer[..., 3:4].astype(np.float32) / 255
    return (frame.astype(np.float32) * (1 - al) + layer[..., :3].astype(np.float32) * al).astype(np.uint8)


def cover_frame_at(path, w, h, t):
    """One frame of `path` at time t, scaled to cover a w x h box."""
    cmd = [ffmpeg_bin(), "-loglevel", "error", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
           "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    b = subprocess.run(cmd, capture_output=True).stdout
    return np.frombuffer(b, np.uint8).reshape(h, w, 3) if len(b) == w * h * 3 else None


# ---------------------------------------------------------------- picture timing
def picture_only(sl):
    """True for a slide (or media cue) that is only a picture: no text, no video."""
    ext = lambda p: os.path.splitext(p or "")[1].lower()
    if sl["kind"] == "media":
        return ext(sl.get("media")) in IMG_EXT and not sl.get("overlay")
    if sl["kind"] != "slide":
        return False
    els = sl["elements"]
    area = lambda e: e["w"] * e["h"]
    big_text = any(e["rtf"] and rtf_plain(e["rtf"]) and area(e) >= 0.05 * W * H for e in els)   # a tiny corner label is not "text"
    big_pic = any(ext(e["media"]) in IMG_EXT and area(e) >= 0.4 * W * H for e in els)
    return big_pic and not big_text and not any(ext(e["media"]) in VID_EXT for e in els)


def still_seconds(sl, a):
    """How long a still lasts: --picture-seconds for picture-only slides (replaces their timer), else the slide's own
    timer, else --hold."""
    if a.picture_seconds is not None and picture_only(sl):
        return float(a.picture_seconds)
    return sl["timer"] if sl["timer"] > 0 else a.hold


# ---------------------------------------------------------------- planning (shared by mp4, pdf, stills, preview)
def plan_slides(slides, a, want_posters):
    """Fill sl['plan'], sl['still'] (RGB array or None) and sl['desc'] for every slide."""
    n = len(slides)
    for i, sl in enumerate(slides, 1):
        progress(0.1 * (i - 1) / max(1, n), f"reading slide {i}/{n}")
        desc, still = "", None
        st = (getattr(a, "settings", None) or {}).get(i, {})      # per-slide choices from the app: seconds, poster
        want_s = float(st["seconds"]) if st.get("seconds") else None
        if want_s is None and getattr(a, "all_seconds", None) and sl["kind"] not in ("audio", "empty"):
            want_s = float(a.all_seconds)                  # one time for every slide (a slide's own choice wins)
        sl["vlen"], sl["poster"] = 0.0, None
        if sl["kind"] == "slide":
            use_bg = a.bg if (a.bg and needs_bg(sl)) else None
            standin = False
            if not use_bg and needs_bg(sl) and text_is_light(sl):
                sl["bg"] = list(STANDIN_BG)
                standin = True
            d = draw_slide(sl, use_bg)
            if d["vid"] is None:
                sl["plan"] = ("still", d["below"], want_s or still_seconds(sl, a))
                still, desc = d["below"], "still" + (" (theme background is not in the file: dark stand-in)" if standin else "")
            else:
                vd, _ = probe(d["vid"]["media"])
                dur = want_s or (sl["timer"] if sl["timer"] > 0 else min(vd or a.hold, a.max_hold))
                sl["plan"] = ("slidevideo", d, dur)
                vw, vh = int(round(d["vid"]["w"])), int(round(d["vid"]["h"]))
                sl["vlen"] = vd or 0.0
                pt = min(max(0.0, float(st.get("poster", 0.0))), max(0.0, (vd or 0) - 0.05))
                sl["poster"] = pt
                fr0 = cover_frame_at(d["vid"]["media"], vw, vh, pt) if pt > 0 else None
                still = compose(d, fr0 if fr0 is not None else next(cover_frames(d["vid"]["media"], vw, vh, 1)))
                desc = f"video background ({os.path.basename(d['vid']['media'])})"
        elif sl["kind"] in ("audio", "empty"):
            ad, _ = probe(sl["audio"]) if sl.get("audio") else (None, False)
            sl["plan"] = ("audio", sl.get("audio"), ad)
            desc = f"sound only ({os.path.basename(sl['audio'])}, {ad or 0:.1f}s)" if sl.get("audio") else "(empty cue)"
        else:
            mp = sl["media"]
            if mp and os.path.exists(mp) and os.path.splitext(mp)[1].lower() in VID_EXT:
                vd, has_audio = probe(mp)
                dur = sl.get("duration") or vd or a.hold
                sl["ov"] = overlay_layer(sl)
                if want_s:                                                  # chosen in the app: play up to that long, hold the last frame after
                    play = min(want_s, dur)
                    sl["plan"] = ("video", mp, play, has_audio)
                    sl["vhold"] = max(0.0, want_s - play)
                else:
                    sl["plan"] = ("video", mp, dur, has_audio)
                    sl["vhold"] = 0.0 if a.no_video_hold else sl["timer"]  # the cue timer is a hold on the last frame
                sl["vlen"] = dur
                pt = st.get("poster")
                pt = min(3.0, (dur or 2) / 2) if pt is None else min(max(0.0, float(pt)), max(0.0, dur - 0.05))
                sl["poster"] = pt
                if want_posters:
                    pf = poster_frame(mp, pt)
                    still = over(pf, sl["ov"]) if pf is not None else None
                desc = f"video {dur:.1f}s" + (" + text" if sl["ov"] is not None else "") + (" + audio" if has_audio else "")
            elif mp and os.path.exists(mp):
                sl["ov"] = overlay_layer(sl)
                still = over(np.asarray(fit_on_black(mp)), sl["ov"])
                sl["plan"] = ("still", still, want_s or still_seconds(sl, a))
                desc = "image" + (" + text" if sl["ov"] is not None else "")
            else:
                still = np.zeros((H, W, 3), np.uint8)
                sl["plan"] = ("still", still, still_seconds(sl, a))
                desc = f"(media not found: {os.path.basename(mp or '?')})"
        sl["still"], sl["desc"] = still, desc
        kind_ = sl["plan"][0]
        extra = f" (+{sl['vhold']:g}s hold)" if kind_ == "video" and sl.get("vhold", 0) > 0 else (f" {sl['plan'][2]:g}s" if kind_ in ("still", "slidevideo") else "")
        snd = f"  + sound {os.path.basename(sl['audio'])}" if sl.get("audio") and kind_ != "audio" else ""
        sl["seconds"] = slide_seconds(sl)
        print(f"  {i:2d}. {sl['name'][:42]:42s} {desc}{extra}{snd}", file=sys.stderr if a.json else sys.stdout)


def slide_seconds(sl):
    p = sl["plan"]
    if p[0] in ("still", "slidevideo"):
        return float(p[2])
    if p[0] == "video":
        return float(p[2] or 0) + sl.get("vhold", sl["timer"])
    return sl["timer"]


def safe_name(s):
    s = re.sub(r'[/\\:*?"<>|]+', "-", s).strip().strip(".")
    return s or "output"


def write_outputs(slides, base, a, outdir):
    """Write the PDF and/or MP4 for one presentation; returns the files written."""
    written = []
    want_pdf = a.pdf is not None or a.pdf_only
    if a.stills:
        os.makedirs(a.stills, exist_ok=True)
        for i, sl in enumerate(slides, 1):
            if sl["still"] is not None:
                Image.fromarray(sl["still"]).save(os.path.join(a.stills, f"{i:02d}_{re.sub(r'[^A-Za-z0-9]+', '_', sl['name'])[:30]}.png"))
    if want_pdf:
        pages = [sl["still"] for sl in slides if sl["plan"][0] != "audio" and sl["still"] is not None
                 and not (a.pdf_skip_video and sl["plan"][0] == "video")]
        pdf_path = a.pdf if a.pdf else os.path.join(outdir, base + ".pdf")
        if pages:
            save_pdf(pages, pdf_path)
            print(f"wrote {pdf_path}  ({len(pages)} pages)")
            written.append(pdf_path)
        else:
            print("[warn] nothing to put in the PDF")
    if a.no_video or a.pdf_only:
        progress(1.0, "done")
        return written
    out = a.out or os.path.join(outdir, base + ".mp4")
    written.append(encode_mp4(slides, a, out))
    return written


def encode_mp4(slides, a, out):
    audio_segs = []                                            # (start_seconds, path, seconds) for clips that carry sound
    tmp_out = out + ".silent.tmp.mp4"
    expected = max(1, int(sum(sl["seconds"] for sl in slides) * FPS))
    enc = subprocess.Popen([ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
                            "-i", "-", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", tmp_out],
                           stdin=subprocess.PIPE)
    last, total = None, 0

    def put(fr):
        nonlocal total
        enc.stdin.write(fr.tobytes()); total += 1
        if total % 15 == 0:
            progress(0.1 + 0.88 * min(1.0, total / expected), f"encoding {total / FPS:.0f}s of {expected / FPS:.0f}s")
    for sl in slides:
        plan = sl["plan"]
        fade = a.fade if a.fade is not None else (sl.get("transition") or 0.7)
        nfade = int(round(fade * FPS)) if last is not None else 0
        start_frame = total
        if sl.get("audio"):
            ad, has_a = probe(sl["audio"])
            if has_a and ad:
                audio_segs.append((start_frame / FPS, sl["audio"], ad))
        if plan[0] == "audio":
            if last is not None:
                for _ in range(int(round(sl["timer"] * FPS))):
                    put(last)
            continue
        if plan[0] == "still":
            frames = (plan[1] for _ in range(max(1, int(round(plan[2] * FPS)))))
        elif plan[0] == "slidevideo":
            d, dur = plan[1], plan[2]
            frames = (compose(d, vf) for vf in cover_frames(d["vid"]["media"], int(round(d["vid"]["w"])), int(round(d["vid"]["h"])), dur))
        else:
            frames = (over(f, sl.get("ov")) for f in video_frames(plan[1], plan[2]))
            if plan[3]:
                audio_segs.append((start_frame / FPS, plan[1], plan[2]))
        k, cur = 0, None
        for fr in frames:
            if k < nfade and last is not None:
                al = (k + 1) / (nfade + 1)
                fr = (last.astype(np.float32) * (1 - al) + fr.astype(np.float32) * al).astype(np.uint8)
            put(fr); k += 1
            cur = fr
        if plan[0] == "video" and cur is not None:            # a video cue's timer is a hold on its last frame
            for _ in range(int(round(sl.get("vhold", sl["timer"]) * FPS))):
                put(cur)
        last = cur if cur is not None else last
    enc.stdin.close()
    enc.wait()
    if total == 0:
        os.remove(tmp_out) if os.path.exists(tmp_out) else None
        sys.exit("Nothing to put in the video (no slides with pictures).")
    progress(0.99, "adding sound")
    if audio_segs:                                             # carry the clips' sound into the mp4
        cmd = [ffmpeg_bin(), "-y", "-loglevel", "error", "-i", tmp_out]
        for _, p, _ in audio_segs:
            cmd += ["-i", p]
        parts = []
        for n, (t0, _, dur) in enumerate(audio_segs, 1):
            ms = int(round(t0 * 1000))
            parts.append(f"[{n}:a]atrim=duration={dur:.3f},asetpts=PTS-STARTPTS,adelay={ms}|{ms}[a{n}]")
        mix = "".join(f"[a{n}]" for n in range(1, len(audio_segs) + 1))
        fc = ";".join(parts) + (f";{mix}amix=inputs={len(audio_segs)}:normalize=0[aout]" if len(audio_segs) > 1 else f";[a1]anull[aout]")
        cmd += ["-filter_complex", fc, "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-t", f"{total / FPS:.3f}", "-movflags", "+faststart", out]
        subprocess.check_call(cmd)
        os.remove(tmp_out)
    else:
        os.replace(tmp_out, out)
    progress(1.0, "done")
    print(f"wrote {out}  ({total / FPS:.1f} s)" + (f", with sound from {len(audio_segs)} clip(s)" if audio_segs else ""))
    return out


# ---------------------------------------------------------------- preview: thumbnails + a ProPresenter-like sheet
def _font(size, bold=False):
    for p in (["/System/Library/Fonts/HelveticaNeue.ttc"] + ["/System/Library/Fonts/Helvetica.ttc", "/Library/Fonts/Arial.ttf"]):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size, index=1 if bold else 0)
            except Exception:
                try:
                    return ImageFont.truetype(p, size)
                except Exception:
                    pass
    return ImageFont.load_default()


def tile_image(sl, tw, th):
    if sl["still"] is not None:
        return Image.fromarray(sl["still"]).resize((tw, th), Image.LANCZOS)
    im = Image.new("RGB", (tw, th), (24, 24, 28))
    d = ImageDraw.Draw(im)
    label = ("Sound: " + os.path.basename(sl["audio"])) if sl.get("audio") else sl.get("desc") or sl["kind"]
    d.text((12, th // 2 - 10), label[:40], fill=(200, 200, 210), font=_font(15))
    return im


def slide_label(sl):
    g = sl.get("group") or ""
    g = "" if g.lower() == "group" else g
    nm = sl["name"] or ""
    if not nm or re.match(r"^Slide rId\d+$", nm):
        nm = next((rtf_plain(e["rtf"]) for e in sl.get("elements", []) + sl.get("overlay", []) if e["rtf"] and rtf_plain(e["rtf"])), "")
        if not nm and sl.get("media"):
            nm = os.path.basename(sl["media"])
    return f"{g} - {nm}" if g and nm and g != nm else (g or nm or sl["kind"])


def make_preview(slides, title, subtitle, outdir):
    """Writes thumbs/NN.jpg, preview.json and sheet.png (a grid that looks like ProPresenter's slide view)."""
    os.makedirs(os.path.join(outdir, "thumbs"), exist_ok=True)
    tw, th = 384, 216
    info = []
    for i, sl in enumerate(slides, 1):
        t = tile_image(sl, tw, th)
        fn = os.path.join(outdir, "thumbs", f"{i:02d}.jpg")
        t.save(fn, quality=88)
        info.append(dict(n=i, file=fn, name=slide_label(sl), group=sl.get("group") or "", color=sl.get("group_color"),
                         kind=sl["plan"][0], desc=sl["desc"], seconds=round(sl["seconds"], 2),
                         vlen=round(sl.get("vlen") or 0, 2), poster=None if sl.get("poster") is None else round(sl["poster"], 2),
                         transition=sl.get("transition") or 0, audio=os.path.basename(sl["audio"]) if sl.get("audio") else None))
    cols = 4
    pad, bar, head = 18, 34, 92
    rows = max(1, (len(slides) + cols - 1) // cols)
    sheet = Image.new("RGB", (pad + cols * (tw + pad), head + rows * (th + bar + pad) + pad), (30, 30, 33))
    d = ImageDraw.Draw(sheet)
    d.text((pad, 18), title, fill=(240, 240, 240), font=_font(26, True))
    d.text((pad, 54), subtitle, fill=(150, 150, 158), font=_font(16))
    for k, sl in enumerate(slides):
        r, c = divmod(k, cols)
        x, y = pad + c * (tw + pad), head + r * (th + bar + pad)
        sheet.paste(tile_image(sl, tw, th), (x, y))
        col = sl.get("group_color") or "#3a3a40"
        d.rectangle([x, y + th, x + tw - 1, y + th + bar], fill=col)
        rgb = tuple(int(col[j:j + 2], 16) for j in (1, 3, 5))
        fg = (0, 0, 0) if sum(rgb) > 450 else (255, 255, 255)
        label = slide_label(sl)
        d.text((x + 8, y + th + 8), f"{k + 1}", fill=fg, font=_font(16, True))
        d.text((x + 38, y + th + 8), label[:34], fill=fg, font=_font(16))
        secs = f"{sl['seconds']:g}s" if sl["seconds"] else ""
        if secs:
            d.text((x + tw - 52, y + th + 8), secs, fill=fg, font=_font(14))
    sheet_path = os.path.join(outdir, "sheet.png")
    sheet.save(sheet_path)
    total = sum(s["seconds"] for s in info)
    res = dict(title=title, subtitle=subtitle, slides=info, sheet=sheet_path, total_seconds=round(total, 2))
    json.dump(res, open(os.path.join(outdir, "preview.json"), "w"), indent=1)
    return res


# ---------------------------------------------------------------- command line
def main():
    global PROGRESS, PP_ROOT, LIB_ROOT, PLAYLIST_FILE
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", nargs="?", help="presentation name, e.g. 'Sunday Service'")
    ap.add_argument("--library", help="only look in libraries whose folder name contains this text")
    ap.add_argument("--list", action="store_true", help="list presentations in the ProPresenter library")
    ap.add_argument("--pp-root", metavar="DIR", help="ProPresenter workspace folder (the one that holds Libraries and Playlists); default: ~/Library/Application Support/RenewedVision/ProPresenter/UserWorkspaces/ProPresenter")
    ap.add_argument("--proplaylist", metavar="FILE", help="read playlists from this .proplaylist file (no import needed)")
    ap.add_argument("--playlists", action="store_true", help="show the playlist tree with numbered items")
    ap.add_argument("--placeholders", action="store_true", help="list every placeholder and the playlist it belongs to")
    ap.add_argument("--playlist", help="playlist name, folder/name, or uuid (see --playlists)")
    ap.add_argument("--item", action="append", help="item number, name or uuid in the playlist; 'all' for every item (repeatable)")
    ap.add_argument("--outdir", help="folder for the mp4 / pdf files (default: current folder)")
    ap.add_argument("--out", help="output mp4 file (single presentation only)")
    ap.add_argument("--fade", type=float, default=None, help="dissolve seconds between slides (default: the presentation's own, else 0.7)")
    ap.add_argument("--hold", type=float, default=5.0, help="seconds for slides that have no timer and no video (default 5)")
    ap.add_argument("--picture-seconds", type=float, default=None, metavar="N", help="seconds each picture-only slide stays on screen (replaces the slide's own timer)")
    ap.add_argument("--all-seconds", type=float, default=None, metavar="N", help="show every slide for N seconds (videos are cut, or hold their last frame); per-slide settings win")
    ap.add_argument("--no-video-hold", action="store_true", help="after a video, go straight to the next slide (ignore the cue's hold on the last frame)")
    ap.add_argument("--slide-settings", metavar="FILE", help='JSON {"3": {"seconds": 10, "poster": 4.5}}: per-slide seconds on screen and the video frame used for stills / PDF')
    ap.add_argument("--max-hold", type=float, default=30.0, help="longest a video-background slide without a timer may play (default 30)")
    ap.add_argument("--bg", help="video or image to show behind text-only slides (ProPresenter themes are not read; this stands in for the theme background)")
    ap.add_argument("--pdf", nargs="?", const="", metavar="FILE", help="also write a PDF, one page per slide (default name: '<name>.pdf')")
    ap.add_argument("--pdf-only", action="store_true", help="write the PDF only, no mp4")
    ap.add_argument("--pdf-skip-video", action="store_true", help="leave video cues out of the PDF (by default each gets a still frame)")
    ap.add_argument("--stills", help="also save every slide as a PNG in this folder")
    ap.add_argument("--no-video", action="store_true", help="do not encode the mp4 (use with --stills)")
    ap.add_argument("--preview", metavar="DIR", help="write slide thumbnails, preview.json and a ProPresenter-like sheet.png, nothing else")
    ap.add_argument("--json", action="store_true", help="print machine-readable JSON (for --playlists, --placeholders, --preview)")
    ap.add_argument("--progress", action="store_true", help="print @@PROGRESS lines (used by the desktop app)")
    a = ap.parse_args()
    PROGRESS = a.progress
    if a.pp_root:
        PP_ROOT = os.path.abspath(os.path.expanduser(a.pp_root))
        LIB_ROOT = os.path.join(PP_ROOT, "Libraries")
        PLAYLIST_FILE = os.path.join(PP_ROOT, "Playlists", "Library")
    a.settings = {}
    if a.slide_settings:
        try:
            a.settings = {int(k): v for k, v in json.load(open(a.slide_settings)).items()}
        except Exception as ex:
            sys.exit(f"Could not read --slide-settings: {ex}")

    if a.list:
        for p in sorted(all_presentations()):
            print(os.path.relpath(p, LIB_ROOT))
        return

    playlist_mode = a.proplaylist or a.playlists or a.placeholders or a.playlist or a.item
    if not playlist_mode:
        if not a.name:
            ap.error("give a presentation name, or use --playlists / --playlist (see --help)")
        path = find_presentation(a.name, a.library)
        jobs = [(os.path.relpath(path, PP_ROOT), os.path.splitext(os.path.basename(path))[0], read_presentation(path), None)]
    else:
        src = Source(a.proplaylist)
        if a.placeholders:
            ph = placeholders(src)
            if a.json:
                print(json.dumps(dict(source=src.label, placeholders=ph), indent=1))
                return
            print(f"Placeholders in {src.label}: {len(ph)}")
            w = max([len(p["name"]) for p in ph] + [10])
            for p in ph:
                st = "EMPTY - drag a presentation onto it in ProPresenter" if p["status"] == "empty" else \
                     (f"-> {p['linked']}" + ("  [file missing]" if p["status"] == "missing" else ""))
                print(f"  {p['name']:{w}s}  in '{p['playlist']}' (item {p['index']})   {st}")
            return
        if a.playlists or (a.playlist and not a.item):
            only = src.find_playlist(a.playlist) if a.playlist else None
            if a.json:
                def strip(n):
                    n = dict(n)
                    if n["type"] == "folder":
                        n["children"] = [strip(c) for c in n["children"]]
                    else:
                        n["items"] = [item_dict(src, it) for it in n["items"]]
                    return n
                tree = [strip(only)] if only else [strip(n) for n in src.tree]
                print(json.dumps(dict(source=src.label, source_path=src.bundle.path if src.bundle else PLAYLIST_FILE,
                                      tree=tree), indent=1))
                return
            print(f"Source: {src.label}")
            print_tree(src, src.tree, 0, only)
            if not only:
                print("\n" + GUIDE_NEW_PROPLAYLIST)
            return
        pl = src.find_playlist(a.playlist)
        jobs = []
        for it in find_items(pl, a.item):
            label, slides = src.item_slides(it)
            jobs.append((label, it["name"], slides, it))
        if len(jobs) > 1 and a.out:
            sys.exit("--out works with one item only; use --outdir.")

    outdir = os.path.abspath(os.path.expanduser(a.outdir)) if a.outdir else os.getcwd()
    os.makedirs(outdir, exist_ok=True)
    if a.preview and len(jobs) > 1:
        sys.exit("--preview works with one item at a time.")
    all_written = []
    for n, (label, base, slides, it) in enumerate(jobs, 1):
        if len(jobs) > 1:
            print(f"\n[{n}/{len(jobs)}] {base}")
        print(f"{label}: {len(slides)} slides", file=sys.stderr if a.json else sys.stdout)
        if a.preview:
            plan_slides(slides, a, want_posters=True)
            res = make_preview(slides, base, label, os.path.abspath(os.path.expanduser(a.preview)))
            print(json.dumps(res, indent=1) if a.json else f"wrote {res['sheet']}  ({len(slides)} slides, {res['total_seconds']:.1f} s)")
            continue
        want_posters = (a.pdf is not None or a.pdf_only) and not a.pdf_skip_video or bool(a.stills)
        plan_slides(slides, a, want_posters)
        all_written += write_outputs(slides, safe_name(base), a, outdir)
    if all_written and a.json:
        print(json.dumps(dict(written=all_written)))


if __name__ == "__main__":
    main()
