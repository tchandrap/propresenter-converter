"""Build a ProPresenter playlist (.proplaylist) from a worship script (PDF) and/or PowerPoint decks.

Inputs (any one is enough):
  * the worship script PDF: order of service, Nats Panggilan, Bible reading, and the full lyrics of every song
  * the preacher's deck (PPTX, usually pictures): becomes one presentation with a slide per picture
  * the master deck from GII national (PPTX): used when there is no PDF, and for texts the PDF does not carry

The look of every slide (fonts, colours, text boxes, groups, actions) is copied from a TEMPLATE playlist: the last
finished playlist (a .proplaylist) or a playlist in the ProPresenter library. Only the texts that change from week
to week are replaced; everything else is copied as it is. ProPresenter files are protobuf and are rewritten
losslessly (this module never guesses a schema: it only swaps strings inside the copied structure).

The engine module (pp2mp4) is passed in through init(): this file reuses its protobuf reader and playlist reader.
"""
import datetime, os, random, re, struct, sys, urllib.parse, uuid, zipfile

P = None                                   # the engine module, set by init()


def init(engine_module):
    global P
    P = engine_module


# ---------------------------------------------------------------- protobuf: write what the engine reads
def _venc(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def enc(nodes):
    o = bytearray()
    for f, w, v in nodes:
        o += _venc(f << 3 | w)
        if w == 0:
            o += _venc(v)
        elif w == 1:
            o += struct.pack("<d", v)
        elif w == 5:
            o += struct.pack("<f", v)
        else:
            raw = enc(v[1]) if v[0] == "msg" else (v[1].encode("utf-8") if v[0] == "str" else v[1])
            o += _venc(len(raw)) + raw
    return bytes(o)


S = lambda s: ("str", s)
M = lambda nodes: ("msg", nodes)
UUID_RE = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")


def new_id():
    return str(uuid.uuid4()).upper()


def remap_ids(nodes, m=None):
    """Copy of `nodes` where every UUID string is replaced by a new one (the same old id always becomes the same new id)."""
    m = {} if m is None else m
    out = []
    for f, w, v in nodes:
        if isinstance(v, tuple):
            if v[0] == "msg":
                v = M(remap_ids(v[1], m))
            elif v[0] == "str" and UUID_RE.match(v[1]):
                v = S(m.setdefault(v[1], new_id()))
        out.append((f, w, v))
    return out


def map_path(nodes, path, fn):
    """Apply fn to every sub-message reached by following the field numbers in `path` (all repeats at each step)."""
    if not path:
        return fn(nodes)
    out = []
    for f, w, v in nodes:
        m = P.msg(v)
        if f == path[0] and m is not None:
            out.append((f, w, M(map_path(m, path[1:], fn))))
        else:
            out.append((f, w, v))
    return out


def set_str(nodes, f, s):
    """Replace the first string field number `f`."""
    out, done = [], False
    for ff, w, v in nodes:
        if ff == f and not done and isinstance(v, tuple) and v[0] in ("str", "raw"):
            out.append((ff, w, S(s)))
            done = True
        else:
            out.append((ff, w, v))
    return out


def replace_strings(nodes, fn):
    """Apply fn(old_string) -> new_string to every string in the tree."""
    out = []
    for f, w, v in nodes:
        if isinstance(v, tuple):
            if v[0] == "msg":
                v = M(replace_strings(v[1], fn))
            elif v[0] == "str":
                v = S(fn(v[1]))
        out.append((f, w, v))
    return out


# ---------------------------------------------------------------- RTF text
def rtf_escape(text):
    out = []
    for ch in text:
        if ch in "\\{}":
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\\n")
        elif ch == "\t":
            out.append("\\tab ")
        elif ord(ch) < 128:
            out.append(ch)
        else:
            try:
                out.append("\\'%02x" % ch.encode("cp1252")[0])
            except Exception:
                out.append("\\u%d?" % (ord(ch) if ord(ch) < 32768 else ord(ch) - 65536))
    return "".join(out)


def rtf_set(rtf, text):
    """Replace the text of the LAST run in an RTF string (keeps font, size, colour and outline of that run).
    `text` may contain newlines; they become line breaks."""
    runs = list(re.finditer(r"\\fs\d+ ", rtf))
    if not runs:
        return rtf
    start = runs[-1].end()
    start += re.match(r"(?:\\[A-Za-z]+-?\d*[ ]?)*", rtf[start:]).end()
    return rtf[:start] + rtf_escape(text) + "}"


def title_case_words(s):
    return " ".join(w[:1].upper() + w[1:] for w in s.split())


def plain(info):
    return P.rtf_plain(info["rtf"], 100000) if info and info["rtf"] else ""


# ---------------------------------------------------------------- a presentation split into reusable pieces
class Pres:
    """A parsed .pro: top-level fields, cues by id, and the groups in playback order."""

    def __init__(self, data):
        self.tree = P.parse(data)
        if self.tree is None:
            sys.exit("A template presentation could not be read.")
        self.top = [x for x in self.tree if x[0] not in (12, 13)]
        self.cues = {P.uuid_of(P.msg(v)): P.msg(v) for f, w, v in self.tree if f == 13}
        self.groups = []                                   # (group_nodes, [cue ids])
        for g in P.fields(self.tree, 12):
            gn = P.msg(g)
            ids = [P.sval(P.first(P.msg(x), 1)) for x in P.fields(gn, 2)]
            self.groups.append((gn, [i for i in ids if i in self.cues]))

    def info(self, cid):
        return P.read_cue(self.cues[cid])

    def group_name(self, gn):
        gi = P.msg(P.first(gn, 1))
        return (P.sval(P.first(gi, 2)) or "") if gi else ""


def group_node(look, cue_ids, name=None):
    """A group copied from `look` (colour, hot key, ...) with a new id, holding the given cues."""
    out = []
    for f, w, v in look:
        if f == 2:
            continue
        if f == 1:
            info = [x for x in P.msg(v) if x[0] != 1]
            info = [(1, 2, M([(1, 2, S(new_id()))]))] + info
            if name is not None:
                info = set_str(info, 2, name)
            v = M(info)
        out.append((f, w, v))
    return out + [(2, 2, M([(1, 2, S(c))])) for c in cue_ids]


def retext(cue, repl):
    """A copy of a slide cue where repl(i, info) -> new text (or None to keep) is applied to its i-th text box."""
    idx = [0]

    def el_fn(el):
        info = P.element_info([(1, 2, M(el))])
        i = idx[0]
        idx[0] += 1
        if not info or not info["rtf"]:
            return el
        new = repl(i, info)
        if new is None:
            return el
        return map_path(el, [13], lambda t: set_str(t, 5, rtf_set(info["rtf"], new)))
    return map_path(cue, [10, 23, 2, 1, 1, 1], el_fn)


def fresh(cue):
    """A copy of a cue with all ids renewed."""
    return remap_ids(cue)


def compose_pres(top, name, groups_with_cues):
    """groups_with_cues: [(look_nodes, name_or_None, [cue_nodes])] in playback order -> .pro bytes."""
    top = [(f, w, S(name)) if f == 3 else ((f, w, M([(1, 2, S(new_id()))])) if f == 2 else (f, w, v)) for f, w, v in top]
    low = [x for x in top if x[0] < 12]
    high = [x for x in top if x[0] > 13]
    gnodes, cnodes = [], []
    for look, gname, cues in groups_with_cues:
        ids = [P.uuid_of(c) for c in cues]
        gnodes.append((12, 2, M(group_node(look, ids, gname))))
        cnodes += [(13, 2, M(c)) for c in cues]
    return enc(low + gnodes + cnodes + high)


# ---------------------------------------------------------------- reading the script PDF
SECTION_RE = re.compile(r"^(verse\s*\d*|chorus|pre-?chorus|bridge|interlude|intro|outro|end|ending|tag|coda|refrain|reff?)\b(.*)$", re.I)
CREDIT_RE = re.compile(r"^(words|music|translation|©|\(c\)|arr\.|lyrics)|ccli|public domain|copyright", re.I)
ARR_RE = re.compile(r"^(?P<title>[^|]{2,80}?)\s*\|\s*(?P<arr>.+)$")
LEADER_RE = re.compile(r"\s+(?:[A-Z]{2,3})$")
MONTHS = {"januari": 1, "februari": 2, "maret": 3, "april": 4, "mei": 5, "juni": 6, "juli": 7, "agustus": 8,
          "september": 9, "oktober": 10, "november": 11, "desember": 12}


def looks_like_title_line(line):
    m = ARR_RE.match(line)
    if not m or CREDIT_RE.search(m.group("title")):
        return None
    arr = m.group("arr")
    if re.search(r"\b[A-G][b#]?\s*:", arr) or re.match(r"^[VBRCE]\d?(?:\s*-\s*[VBRCE]\d?)+", arr) or re.search(r"\bbpm\b", arr, re.I):
        return m.group("title").strip(), arr.strip()
    return None


def parse_script_pdf(path):
    try:
        import pypdf
    except Exception:
        sys.exit("Reading PDF files needs the 'pypdf' package (pip install pypdf).")
    r = pypdf.PdfReader(path)
    pages = [(p.extract_text() or "").replace("\u00a0", " ") for p in r.pages]
    lines0 = [l.strip() for l in pages[0].splitlines() if l.strip()]
    res = dict(title=lines0[0] if lines0 else "", date=None, theme="", items=[], songs=[])
    for l in lines0[:6]:
        m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", l)
        if m and not res["date"] and MONTHS.get(m.group(2).lower()):
            res["date"] = datetime.date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))
        if l.lower().startswith("worship theme"):
            res["theme"] = l.split(":", 1)[-1].strip()
    cur = None                                              # order of service: "N. Title  JB", the lines below belong to it
    for l in lines0:
        m = re.match(r"^(\d{1,2})\.\s+(.*)$", l)
        if m:
            cur = dict(no=int(m.group(1)), title=LEADER_RE.sub("", m.group(2)).strip(), body=[])
            res["items"].append(cur)
        elif cur is not None:
            cur["body"].append(l)
    for pi, text in enumerate(pages[1:], 2):                # songs: the following pages; one page can carry several
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        i, head = 0, None
        while i < len(lines):
            m = re.match(r"^Pujian Jemaat\s*\((\d+)\)", lines[i], re.I)
            if m:
                head = int(m.group(1))
                i += 1
                continue
            t = looks_like_title_line(lines[i])
            if not t:
                i += 1
                continue
            song = dict(n=head, title=t[0], arrangement=t[1], credits=[], sections=[], page=pi)
            res["songs"].append(song)
            i += 1
            while i < len(lines) and CREDIT_RE.search(lines[i]) and not looks_like_title_line(lines[i]):
                song["credits"].append(lines[i])
                i += 1
            while i < len(lines):
                ln = lines[i]
                if re.match(r"^Pujian Jemaat\s*\(\d+\)", ln, re.I) or looks_like_title_line(ln):
                    break
                sm = SECTION_RE.match(ln)
                if sm and len(ln) < 40 and not re.search(r"[,.;!?]$", ln):
                    label = sm.group(1).strip().lower()
                    song["sections"].append(dict(label="interlude" if label.startswith("interlude") else label, lines=[]))
                elif re.match(r"^\(.*\)$", ln):
                    pass                                    # (soft), (4 bar): a performance note, not a lyric
                else:
                    if not song["sections"]:
                        song["sections"].append(dict(label=None, lines=[]))
                    song["sections"][-1]["lines"].append(ln)
                i += 1
    return res


def reading_parts(body_lines):
    """['P: text', 'more', 'J: ...'] -> [('P', 'text\\nmore'), ('J', ...)]"""
    parts = []
    for l in body_lines:
        m = re.match(r"^(P\+J|P|J)\s*:\s*(.*)$", l)
        if m:
            parts.append([m.group(1), m.group(2)])
        elif parts:
            parts[-1][1] += "\n" + l
    return [(a, b.strip()) for a, b in parts]


# ---------------------------------------------------------------- reading PowerPoint decks
def _pptx():
    try:
        import pptx
        return pptx
    except Exception:
        sys.exit("Reading PowerPoint files needs the 'python-pptx' package (pip install python-pptx).")


def slide_texts(slide):
    out = []
    for sh in slide.shapes:
        if getattr(sh, "has_text_frame", False) and sh.text_frame.text.strip():
            out.append(sh.text_frame.text.replace("\x0b", "\n").strip())
    return out


def parse_khotbah_pptx(path, outdir):
    """-> dict(name, slides=[dict(image=path or None, text=[..])]). Pictures are written into outdir."""
    prs = _pptx().Presentation(path)
    os.makedirs(outdir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(path))[0]
    slides = []
    for i, s in enumerate(prs.slides, 1):
        pics = [sh for sh in s.shapes if sh.shape_type == 13]
        img = None
        if pics:
            big = max(pics, key=lambda sh: (sh.width or 0) * (sh.height or 0))
            fn = os.path.join(outdir, f"{stem}_slide{i:02d}.{(big.image.ext or 'png').lower()}")
            with open(fn, "wb") as f:
                f.write(big.image.blob)
            img = fn
        slides.append(dict(image=img, text=slide_texts(s)))
    return dict(name=stem, slides=slides)


def slide_background(slide, outdir, name):
    """The picture that fills the slide (its background), saved into outdir -> path or None."""
    if not outdir:
        return None
    ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main", "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    for blip in slide._element.findall(".//a:blip", ns):
        rid = blip.get("{%s}embed" % ns["r"])
        try:
            part = slide.part.related_part(rid)
        except Exception:
            continue
        ext = os.path.splitext(part.partname)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png"):
            continue
        os.makedirs(outdir, exist_ok=True)
        fn = os.path.join(outdir, f"{name}{ext}")
        with open(fn, "wb") as f:
            f.write(part.blob)
        return fn
    return None


def parse_master_pptx(path, outdir=None):
    """The master deck from Church Center (every liturgy item as slides of text) -> dict(songs, nats, reading, date).
    The background picture of each song's title slide is saved into outdir."""
    prs = _pptx().Presentation(path)
    res = dict(songs=[], nats=None, reading=None, date=None)
    cur, number, just_after_song = None, 0, False
    for s in prs.slides:
        texts = slide_texts(s)
        up = [t.upper() for t in texts]
        if not texts:
            continue
        if res["date"] is None:
            for t in texts:
                m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", t)
                if m:
                    res["date"] = datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        if any("NATS PANGGILAN" in u for u in up):
            quote = max(texts, key=len)
            ref = next((t for t in texts if re.search(r"\d+:\d+", t) and len(t) < 40), "")
            res["nats"] = dict(text=quote.strip("“”\" "), ref=ref.title())
            cur, just_after_song = None, False
            continue
        credit = next((t for t in texts if CREDIT_RE.search(t.split("\n")[0])), None)
        if credit:
            title = next((t for t in texts if t is not credit), "")
            if not just_after_song:                         # two songs back to back are 4 and 4b, like the PDF
                number += 1
            cur = dict(n=number, title=title_case_words(title.lower()), arrangement="",
                       credits=[credit.replace("\n", " ")], sections=[dict(label=None, lines=[])], page=0, partial=True,
                       bg=slide_background(s, outdir, f"master_song_{len(res['songs']) + 1}_background"))
            res["songs"].append(cur)
            just_after_song = True
            continue
        marker = [t for t in texts if t in ("P:", "J:", "P+J:", "P", "J", "P+J")]
        ref = next((t for t in texts if re.search(r"\d+:\d+", t) and t.isupper()), None)
        if ref and (marker or res["reading"] is not None or any("BACAAN" in u for u in up)):
            body = [t for t in texts if t not in marker and t != ref and "BACAAN" not in t.upper()]
            res["reading"] = res["reading"] or dict(ref=ref.title(), parts=[])
            if marker and body:
                res["reading"]["parts"].append((marker[0].rstrip(":"), body[0].replace("\n", " ").strip()))
            cur, just_after_song = None, False
            continue
        if any(u.startswith("KEBAKTIAN") for u in up):
            cur, just_after_song = None, False
            continue
        if cur is not None and len(texts) == 1:
            cur["sections"][-1]["lines"].extend([l.strip() for l in texts[0].split("\n") if l.strip()])
        else:
            cur = None
            just_after_song = False
    return res


# ---------------------------------------------------------------- template playlist
class Template:
    """The playlist whose look is copied: items as raw nodes, presentations as bytes."""

    def __init__(self, key):
        is_file = bool(key) and key.lower().endswith(".proplaylist")
        self.src = P.Source(key if is_file else None)
        self.bundle = self.src.bundle
        if is_file:
            pls = self.src.playlists()
            pl = pls[0] if len(pls) == 1 else self.src.find_playlist(None)
        elif key:
            pl = self.src.find_playlist(key)
        else:
            pl = self._auto()
        self.pl = pl
        raw = self.bundle.read("data") if self.bundle else open(P.PLAYLIST_FILE, "rb").read()
        self.data = P.parse(raw)
        self.root = P.get(self.data, 3)
        self.node = self._find(P.fields(P.get(self.root, 12), 1), pl["uuid"])
        if self.node is None:
            sys.exit("The template playlist could not be found in its file.")
        self.items = [P.msg(k) for k in P.fields(P.get(self.node, 13), 1)]
        self.infos = pl["items"]

    def _auto(self):
        cands = []
        for p in self.src.playlists():
            if any(i["name"].lower().startswith("pujian jemaat") for i in p["items"]):
                cands.append((("template" in p["name"].lower()), len(p["items"]), p))
        if not cands:
            sys.exit("No template playlist found: choose a finished playlist (a .proplaylist) that has 'Pujian Jemaat' songs in it.")
        cands.sort(key=lambda c: (c[0], c[1]))
        return cands[-1][2]

    def _find(self, nodes, uid):
        for k in nodes:
            n = P.msg(k)
            if P.uuid_of(n) == uid:
                return n
            if P.first(n, 12) is not None:
                hit = self._find(P.fields(P.get(n, 12), 1), uid)
                if hit is not None:
                    return hit
        return None

    def pres(self, info):
        return self.src.locate(info.get("ref")) if info.get("ref") else None

    def by_name(self, name):
        for it, inf in zip(self.items, self.infos):
            if inf["kind"] != "header" and inf["name"].strip().lower() == name.strip().lower():
                return it, inf
        return None, None

    def starting(self, prefix):
        return [(it, inf) for it, inf in zip(self.items, self.infos) if inf["name"].lower().startswith(prefix.lower())]


class Parts:
    """Reusable pieces found in the template: motion cues, a title cue, a lyric cue, group looks, a picture cue."""

    def __init__(self, tpl):
        self.motions, self.title_cue, self.lyric_cue, self.song_top = [], None, None, None
        self.group_looks, self.group_blank = {}, None
        self.image_cue = self.image_look = self.image_top = None
        for it, inf in tpl.starting("Pujian Jemaat"):
            hit = tpl.pres(inf)
            if not hit:
                continue
            pr = Pres(hit[1])
            if self.song_top is None:
                self.song_top = pr.top
            for gn, ids in pr.groups:
                nm = pr.group_name(gn)
                self.group_looks.setdefault(nm, gn)
                if self.group_blank is None and not nm:
                    self.group_blank = gn
                for cid in ids:
                    c = pr.info(cid)
                    if c["kind"] == "media" and os.path.splitext(c.get("media") or "")[1].lower() in P.VID_EXT:
                        self.motions.append(pr.cues[cid])
                    elif c["kind"] == "slide":
                        texts = [plain(e) for e in c["elements"]]
                        if self.title_cue is None and any(t.upper() == "PUJIAN JEMAAT" for t in texts):
                            self.title_cue = pr.cues[cid]
                        elif self.lyric_cue is None and len(c["elements"]) == 2 and any(e["w"] > 1500 and e["h"] > 700 and plain(e) for e in c["elements"]):
                            self.lyric_cue = pr.cues[cid]
        if not (self.motions and self.title_cue and self.lyric_cue and self.group_blank):
            sys.exit("The template playlist has no complete 'Pujian Jemaat' song to copy the look from.")
        it, inf = tpl.by_name("KHOTBAH")
        hit = tpl.pres(inf) if inf and inf.get("ref") else None
        if hit:
            pr = Pres(hit[1])
            for gn, ids in pr.groups:
                for cid in ids:
                    if pr.info(cid)["kind"] == "media" and self.image_cue is None:
                        self.image_cue, self.image_look, self.image_top = pr.cues[cid], gn, pr.top


# ---------------------------------------------------------------- builders
def motion_available(cue):
    """True when the motion video of this cue exists on this computer (where the cue points, or in ProPresenter's media folder)."""
    for r in P.media_refs(cue):
        if os.path.exists(r) or os.path.exists(os.path.join(P.PP_ROOT, "Media", "Assets", os.path.basename(r))):
            return True
    return False


def image_cue(parts, image_path, media_root):
    """A picture cue (copied from the template's KHOTBAH slide) showing image_path -> (cue nodes, file name)."""
    fn = os.path.basename(image_path)
    try:
        from PIL import Image
        w, h = Image.open(image_path).size
    except Exception:
        w, h = 1920, 1080
    url = "file://" + urllib.parse.quote(f"{media_root}/{fn}")

    def fix(t):
        if t.startswith("file://"):
            return url
        if t.startswith("Media/Assets/"):
            return "Media/Assets/" + fn
        if t.lower().endswith((".png", ".jpg", ".jpeg")) and "/" not in t:
            return fn
        return t
    c = replace_strings(remap_ids(parts.image_cue), fix)
    c = map_path(c, [10, 20, 5, 5, 1, 5], lambda d: [(f, wt, float(w if n == 0 else h)) for n, (f, wt, v) in enumerate(d)])
    return c, fn


def assign_labels(songs):
    """'Pujian Jemaat (4)' with two songs: 4 and 4b."""
    seen = {}
    for s in songs:
        n = s["n"] or 0
        k = seen.get(n, 0)
        seen[n] = k + 1
        s["label"] = f"{n}" + ("" if k == 0 else chr(ord("a") + k))


def chunk(lines, n):
    return [lines[i:i + n] for i in range(0, len(lines), n)]


def group_title(label, counters):
    if label in ("chorus", "refrain", "reff", "ref"):
        return "Chorus"
    if label.startswith("verse"):
        num = re.sub(r"\D", "", label) or str(counters.get("verse", 0) + 1)
        counters["verse"] = int(num)
        return "Verse" if int(num) == 1 else f"Verse {num}"
    if label in ("end", "ending", "outro", "coda", "tag"):
        return "Outro"
    if label.startswith("pre"):
        return "Pre-Chorus"
    return label.title()


def build_song(parts, song, per_slide=2, media_root=""):
    """-> (.pro bytes, [(file name, path)], note) for one song: a motion, a title slide, then the lyrics `per_slide` lines at a time.
    The motion is picked at random among the template's motions that exist on this computer. When none does, the
    background picture from the master deck is used; with neither, a template motion is kept as a link."""
    files, note = [], ""
    here = [m for m in parts.motions if motion_available(m)]
    if here:
        media = fresh(random.choice(here))
    elif song.get("bg") and parts.image_cue is not None:
        c, fn = image_cue(parts, song["bg"], media_root)
        media, files, note = c, [(fn, song["bg"])], "no motion on this computer: background taken from the master deck"
    else:
        media = fresh(random.choice(parts.motions))
        note = "the template's motions are not on this computer: relink the motion in ProPresenter"
    credits = song["credits"][0] if song["credits"] else ""
    title_cue = fresh(retext(parts.title_cue, lambda i, info: (
        credits if info["y"] > 800 else (song["title"] if plain(info).upper() != "PUJIAN JEMAAT" and info["h"] > 500 else None))))

    def lyric(lines):
        text = "\n".join(lines)
        return fresh(retext(parts.lyric_cue, lambda i, info: text if info["w"] > 1500 and info["h"] > 700 else None))
    groups = [(parts.group_blank, None, [media])]
    if any(s["label"] for s in song["sections"]):
        groups.append((parts.group_blank, None, [title_cue]))
        counters = {}
        for sec in song["sections"]:
            if sec["label"] == "interlude" or not sec["lines"]:
                continue
            gname = group_title(sec["label"] or "verse", counters)
            look = parts.group_looks.get(gname) or parts.group_looks.get("Verse") or parts.group_blank
            groups.append((look, gname, [lyric(ch) for ch in chunk(sec["lines"], per_slide)]))
    else:
        cues = [title_cue]
        for sec in song["sections"]:
            cues += [lyric(ch) for ch in chunk(sec["lines"], per_slide)]
        groups.append((parts.group_blank, None, cues))
    return compose_pres(parts.song_top, f"Pujian Jemaat {song['label']} - {song['title']}", groups), files, note


def split_verse(text, limit=125):
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return [text]
    pieces, cur = [], ""
    for seg in re.split(r"(?<=[,;.:!?])\s+", text):
        if cur and len(cur) + len(seg) + 1 > limit:
            pieces.append(cur)
            cur = seg
        else:
            cur = (cur + " " + seg).strip()
    if cur:
        pieces.append(cur)
    return pieces


def build_nats(tpl_data, name, nats):
    """Votum & Nats Panggilan: the Nats slides are rebuilt from the verse (split over slides when it is long)."""
    pr = Pres(tpl_data)
    nats_cues = [(cid, pr.cues[cid], pr.info(cid)) for gn, ids in pr.groups for cid in ids
                 if pr.info(cid)["kind"] == "slide" and any("NATS" in plain(e).upper() for e in pr.info(cid)["elements"])]
    with_ref = next((x for x in nats_cues if any(re.search(r"\d+:\d+", plain(e)) for e in x[2]["elements"])), None)
    without = next((x for x in nats_cues if x is not with_ref), with_ref)
    pieces = split_verse(nats["text"])
    out, done = [], False
    for gn, ids in pr.groups:
        cues = []
        for cid in ids:
            info = pr.info(cid)
            if any(cid == x[0] for x in nats_cues):
                if done or not with_ref:
                    continue
                done = True
                for k, piece in enumerate(pieces):
                    base = with_ref if k == len(pieces) - 1 else without
                    cues.append(fresh(retext(base[1], lambda i, e, piece=piece: (
                        nats["ref"] if re.search(r"\d+:\d+", plain(e)) and e["h"] < 200 else (piece if e["w"] > 1500 and e["h"] > 700 else None)))))
            else:
                cues.append(fresh(pr.cues[cid]))
        out.append((gn, None, cues))
    return compose_pres(pr.top, name, out)


def build_reading(tpl_data, name, reading):
    """Bacaan Bertanggapan: a title slide, then P+J pairs on one slide and P+J together on another."""
    pr = Pres(tpl_data)
    ref, parts_ = reading["ref"], list(reading["parts"])
    flat = [(gn, cid, pr.cues[cid], pr.info(cid)) for gn, ids in pr.groups for cid in ids]
    media = [x for x in flat if x[3]["kind"] == "media"]
    title = next((x for x in flat if x[3]["kind"] == "slide" and any(plain(e).upper() == "BACAAN ALKITAB" for e in x[3]["elements"])), None)
    pair = next((x for x in flat if x[3]["kind"] == "slide" and sum(1 for e in x[3]["elements"] if plain(e) in ("P", "J")) == 2), None)
    single = next((x for x in flat if x[3]["kind"] == "slide" and any(plain(e) == "P+J" for e in x[3]["elements"])), None)
    out = [(gn, None, [fresh(c)]) for gn, cid, c, inf in media]
    if title:
        out.append((title[0], None, [fresh(retext(title[2], lambda i, e: ref if re.search(r"\d+:\d+", plain(e)) else None))]))
    k = 0
    while k < len(parts_):
        who, text = parts_[k]
        if who == "P" and k + 1 < len(parts_) and parts_[k + 1][0] == "J" and pair:
            texts = {"P": text, "J": parts_[k + 1][1]}
            els = pair[3]["elements"]

            def pp(i, e, texts=texts, els=els):
                t = plain(e)
                if t in ("P", "J"):
                    return None
                if "Bacaan Alkitab" in t and re.search(r"\d+:\d+", t):
                    return "Bacaan Alkitab\n" + ref
                prev = plain(els[i - 1]) if i > 0 else ""
                return texts.get(prev)
            out.append((pair[0], None, [fresh(retext(pair[2], pp))]))
            k += 2
            continue
        if single:
            def sp(i, e, who=who, text=text):
                t = plain(e)
                if t == "P+J":
                    return who
                if re.search(r"\d+:\d+", t):
                    return "Bacaan Alkitab\n" + ref
                return text if e["w"] > 1000 and e["h"] > 200 else None
            out.append((single[0], None, [fresh(retext(single[2], sp))]))
        k += 1
    return compose_pres(pr.top, name, out)


def build_image_deck(parts, name, deck, media_root):
    """One presentation with a slide per picture. -> (.pro bytes, [(file name, path)])."""
    files, cues = [], []
    for s in deck["slides"]:
        if s["image"]:
            c, fn = image_cue(parts, s["image"], media_root)
            cues.append(c)
            files.append((fn, s["image"]))
        elif s["text"]:
            lines = [l for l in "\n".join(s["text"]).split("\n") if l.strip()]
            for ch in chunk(lines, 2):
                t = "\n".join(ch)
                cues.append(fresh(retext(parts.lyric_cue, lambda i, info, t=t: t if info["w"] > 1500 and info["h"] > 700 else None)))
    if not cues:
        sys.exit("The preacher's deck has no pictures or text to use.")
    return compose_pres(parts.image_top, name, [(parts.image_look, None, cues)]), files


# ---------------------------------------------------------------- playlist assembly
def pres_item(old_item, name):
    """A playlist item copied from `old_item` but pointing at '<name>.pro' in the same library folder."""
    def fix(t):
        if t.startswith("Libraries/") and t.endswith(".pro"):
            return t.rsplit("/", 1)[0] + f"/{name}.pro"
        if t.startswith("file://") and t.lower().endswith(".pro"):
            return t.rsplit("/", 1)[0] + "/" + urllib.parse.quote(name + ".pro")
        return t
    return set_str(replace_strings(remap_ids(old_item), fix), 2, name)


def item_name(item_nodes):
    return P.sval(P.first(item_nodes, 2)) or ""


def write_bundle(out_path, data_bytes, pro_files, media_files):
    tmp = out_path + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
        z.writestr("data", data_bytes)
        for n, b in pro_files.items():
            z.writestr(n, b)
        for n, p in media_files.items():
            z.write(p, n)
    os.replace(tmp, out_path)


def run(a):
    """Entry point from the engine's command line -> a result dict."""
    pdf = parse_script_pdf(a.import_script) if a.import_script else None
    master = parse_master_pptx(a.import_master, os.path.join(P.CACHE, "import-media", "master")) if a.import_master else None
    if not (pdf or master or a.import_khotbah):
        sys.exit("Give at least one of: the worship script (PDF), the preacher's deck (PPTX), the master deck (PPTX).")
    per = max(1, int(a.lines_per_slide or 2))
    tpl = Template(a.template_playlist)
    parts = Parts(tpl)
    work = os.path.join(P.CACHE, "import-media")
    report, notes = [], []
    pro_files, media_files = {}, {}
    media_root = os.path.join(P.PP_ROOT, "Media", "Assets")

    songs = pdf["songs"] if pdf and pdf["songs"] else (master["songs"] if master else [])
    assign_labels(songs)
    if master and songs is not master["songs"]:             # the master deck's background for each song of the script
        assign_labels(master["songs"])
        import difflib
        by_label = {m["label"]: m for m in master["songs"]}
        for sg in songs:
            m = by_label.get(sg["label"])
            if m is None or difflib.SequenceMatcher(None, m["title"].lower(), sg["title"].lower()).ratio() < 0.35:
                m = max(master["songs"], key=lambda x: difflib.SequenceMatcher(None, x["title"].lower(), sg["title"].lower()).ratio(), default=None)
            if m is not None:
                sg["bg"] = m.get("bg")
    nats = reading = None
    date = (pdf["date"] if pdf else None) or (master["date"] if master else None)
    if pdf:
        for it in pdf["items"]:
            low = it["title"].lower()
            m = re.split(r"\s+[–-]\s+", it["title"], maxsplit=1)
            tail = m[1].strip() if len(m) > 1 else ""
            if low.startswith("nats panggilan"):
                nats = dict(text=" ".join(it["body"]).strip().strip("“”\" "), ref=tail)
            elif low.startswith("bacaan alkitab"):
                reading = dict(ref=tail, parts=reading_parts(it["body"]))
    if master:
        nats = nats or master["nats"]
        if not (reading and reading["parts"]) and master["reading"] and master["reading"]["parts"]:
            reading = master["reading"]

    replaced = {}
    for s in songs:
        name = f"Pujian Jemaat {s['label']} - {s['title']}"
        b, sfiles, mnote = build_song(parts, s, per, media_root)
        pro_files[name + ".pro"] = b
        for fn, path in sfiles:
            media_files[media_root.lstrip("/") + "/" + fn] = path
        replaced[s["label"]] = name
        note = f"{sum(len(x['lines']) for x in s['sections'])} lines, {per} per slide" + (f"; {mnote}" if mnote else "")
        if s.get("partial"):
            note = "only the first lyric slide is in the master deck: add the PDF to get every lyric"
        report.append(dict(item=name, status="partial" if s.get("partial") else "generated", note=note))

    nv_it, nv_inf = tpl.by_name("Votum & Nats Panggilan")
    hit = tpl.pres(nv_inf) if nv_inf and nv_inf.get("ref") else None
    if nats and nats.get("text") and hit:
        pro_files["Votum & Nats Panggilan.pro"] = build_nats(hit[1], "Votum & Nats Panggilan", nats)
        report.append(dict(item="Votum & Nats Panggilan", status="generated", note=nats.get("ref", "")))
    rd_it, rd_inf = None, None
    for it, inf in zip(tpl.items, tpl.infos):
        if inf["name"].lower().startswith("bacaan") and inf.get("ref"):
            rd_it, rd_inf = it, inf
            break
    hit = tpl.pres(rd_inf) if rd_inf else None
    if reading and reading["parts"] and hit:
        rname = rd_inf["name"].strip()
        pro_files[rname + ".pro"] = build_reading(hit[1], rname, reading)
        report.append(dict(item=rname, status="generated", note=reading["ref"]))

    khot_name = None
    if a.import_khotbah:
        if parts.image_cue is None:
            sys.exit("The template playlist has no 'KHOTBAH' picture slide to copy for the preacher's pictures.")
        deck = parse_khotbah_pptx(a.import_khotbah, os.path.join(work, "khotbah"))
        khot_name = deck["name"]
        b, files = build_image_deck(parts, khot_name, deck, media_root)
        pro_files[khot_name + ".pro"] = b
        for fn, path in files:
            media_files[media_root.lstrip("/") + "/" + fn] = path
        report.append(dict(item=khot_name, status="generated", note=f"{len(deck['slides'])} slides"))

    lon_it, lon_inf = tpl.by_name("Lonceng")
    hit = tpl.pres(lon_inf) if lon_inf and lon_inf.get("ref") else None
    if date and hit:                                        # the date on the welcome slides
        new = date.strftime("%d.%m.%Y")
        tr = replace_strings(P.parse(hit[1]), lambda t: re.sub(r"\d{2}\.\d{2}\.\d{4}", new, t) if "\\rtf" in t else t)
        pro_files["Lonceng.pro"] = enc(tr)
        report.append(dict(item="Lonceng", status="generated", note=f"date {new}"))

    # the playlist: the template's items in the template's order, the changed ones swapped in
    items_out, used, skip_next = [], set(), False
    song_item = tpl.starting("Pujian Jemaat")[0][0] if tpl.starting("Pujian Jemaat") else None
    pres_template = next((x for x, y in zip(tpl.items, tpl.infos) if y["kind"] == "presentation"), None)
    for it, inf in zip(tpl.items, tpl.infos):
        nm, low = inf["name"].strip(), inf["name"].strip().lower()
        if skip_next and inf["kind"] == "presentation":
            skip_next = False
            continue
        skip_next = False
        if low.startswith("pujian jemaat") and songs:
            m = re.match(r"pujian jemaat\s*(\d+)([a-z]?)", low)
            label = (m.group(1) + m.group(2)) if m else None
            if label in replaced:
                items_out.append(pres_item(it, replaced[label]))
                used.add(label)
            continue
        if low == "votum & nats panggilan" and "Votum & Nats Panggilan.pro" in pro_files:
            items_out.append(pres_item(pres_template, nm) if inf["kind"] == "placeholder" else remap_ids(it))
            continue
        items_out.append(it)
        if khot_name and low == "khotbah" and inf["kind"] != "header":
            items_out.append(pres_item(song_item or pres_template, khot_name))
            skip_next = True                                # the old preacher's deck is replaced
    extra = [s for s in songs if s["label"] not in used]
    if extra and song_item is not None:
        last = max((i for i, x in enumerate(items_out) if item_name(x).lower().startswith("pujian jemaat")), default=len(items_out) - 1)
        for k, s in enumerate(extra, 1):
            items_out.insert(last + k, pres_item(song_item, replaced[s["label"]]))

    for x in items_out:                                     # presentations that stay as they are travel inside the file
        info = P.item_info(x, 0)
        ref = info.get("ref")
        if info["kind"] in ("presentation", "placeholder") and ref:
            fn = os.path.basename(ref["rel"] or "")
            if fn and fn not in pro_files:
                got = tpl.src.locate(ref)
                if got:
                    pro_files[fn] = got[1]
                else:
                    report.append(dict(item=info["name"], status="missing", note="presentation not found in the template: left as a link"))

    name = a.playlist_name or (f"KU - {date.strftime('%Y%m%d')}" if date else "KU - new")
    pl_nodes = [(1, 2, M([(1, 2, S(new_id()))])), (2, 2, S(name)), (13, 2, M([(1, 2, M(x)) for x in items_out]))]
    root = [x for x in tpl.root if x[0] != 12] + [(12, 2, M([(1, 2, M(pl_nodes))]))]
    data = enc([x for x in tpl.data if x[0] != 3] + [(3, 2, M(root))])
    out = os.path.abspath(os.path.expanduser(a.import_out))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    write_bundle(out, data, pro_files, media_files)

    for it, inf in zip(tpl.items, tpl.infos):
        low = inf["name"].strip().lower()
        if low == "khotbah" and inf["kind"] == "presentation":
            notes.append("KHOTBAH (the sermon title picture) is copied from the template: replace its picture and date by hand.")
        if re.match(r"^\d{6}\s+warta", low):
            notes.append(f"'{inf['name'].strip()}' (announcements) is copied from the template: update it by hand.")
    return dict(out=out, playlist=name, report=report, notes=sorted(set(notes)), songs=[s["title"] for s in songs], items=len(items_out))
