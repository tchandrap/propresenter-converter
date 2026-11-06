#!/usr/bin/env python3
"""Self-test for the SlideBridge engine using a synthetic .proplaylist (no ProPresenter install, no real media).

  python3 tests/engine_test.py python3 engine/pp2mp4.py          # test the Python engine
  python3 tests/engine_test.py build/engine-dist/pp2mp4-engine/pp2mp4-engine   # test the frozen engine

Everything after the script name is the engine command. Exit code 0 = all checks passed."""
import json, os, re, struct, subprocess, sys, tempfile, zipfile


# ---- a tiny protobuf writer (the wire format is all the engine reads)
def varint(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def tag(f, w): return varint((f << 3) | w)
def ld(f, b): return tag(f, 2) + varint(len(b)) + b
def s(f, text): return ld(f, text.encode())
def f32(f, x): return tag(f, 5) + struct.pack("<f", x)
def m(f, *parts): return ld(f, b"".join(parts))


def cue(uuid, name, timer, rgb):
    """One slide: a full-screen colour fill. Layout as ProPresenter writes it: cue(13) > action(10) > 23 > slide(2) >
    elements(1) > wrapper(1) > element(1); the slide background is field 5 of the elements, the transition field 5 of the slide."""
    fill = m(1, f32(1, rgb[0]), f32(2, rgb[1]), f32(3, rgb[2]), f32(4, 1.0))
    element = b"".join([s(2, "Fill"),
                        m(3, m(1, f32(1, 0), f32(2, 0)), m(2, f32(1, 1920), f32(2, 1080))),
                        f32(5, 1.0), m(9, fill)])
    wrapper = ld(1, element)
    elements = ld(1, wrapper) + m(5, f32(1, 0.2), f32(2, 0.2), f32(3, 0.2), f32(4, 1.0))
    slide = ld(1, elements) + m(5, f32(1, 0.7))
    action = m(10, m(23, ld(2, slide)))
    return m(13, m(1, s(1, uuid)), s(2, name), (f32(13, timer) if timer else b""), action)


def presentation(slides):
    cues = [cue(u, n, t, c) for (u, n, t, c) in slides]
    group = m(12, m(1, m(1, s(1, "00000000-aaaa-4bbb-8ccc-groupuuid0001")), s(2, "Verse")),
              *[m(2, s(1, u)) for (u, _, _, _) in slides])
    return b"".join(cues) + group


def pres_item(uuid, name, rel):
    return m(1, m(1, s(1, uuid)), s(2, name), m(4, m(1, s(1, "file:///nowhere/" + rel), m(4, s(2, rel)))))


def playlist_data():
    items = [
        m(1, m(1, s(1, "11111111-aaaa-4bbb-8ccc-item00000001")), s(2, "Header"), m(3, m(1, f32(1, .5), f32(2, .5), f32(3, .5), f32(4, 1)))),
        pres_item("11111111-aaaa-4bbb-8ccc-item00000002", "Songs", "Songs.pro"),
        m(1, m(1, s(1, "11111111-aaaa-4bbb-8ccc-item00000003")), s(2, "Announcements"),
          m(8, m(1, m(1, s(1, "11111111-aaaa-4bbb-8ccc-item00000013")), s(2, "Linked"),
                 m(4, m(1, s(1, "file:///nowhere/Linked.pro"), m(4, s(2, "Linked.pro"))))))),
        m(1, m(1, s(1, "11111111-aaaa-4bbb-8ccc-item00000004")), s(2, "Sermon"), ld(8, b"")),
    ]
    playlist = m(1, m(1, s(1, "22222222-aaaa-4bbb-8ccc-play00000001")), s(2, "Test Playlist"), m(13, *items))
    return m(3, m(1, s(1, "33333333-aaaa-4bbb-8ccc-root00000001")), s(2, "PLAYLIST"), m(12, playlist))


def make_fixture(path):
    songs = presentation([("44444444-aaaa-4bbb-8ccc-cue000000001", "One", 2.0, (0.8, 0.1, 0.1)),
                          ("44444444-aaaa-4bbb-8ccc-cue000000002", "Two", 0.0, (0.1, 0.5, 0.8))])  # no timer on purpose
    linked = presentation([("55555555-aaaa-4bbb-8ccc-cue000000003", "Only", 1.0, (0.1, 0.7, 0.2))])
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        z.writestr("data", playlist_data())
        z.writestr("Songs.pro", songs)
        z.writestr("Linked.pro", linked)


# ---- the checks
FAILS = []


def check(name, ok, extra=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"   {extra}" if extra and not ok else ""))
    if not ok:
        FAILS.append(name)


def main():
    engine = [os.path.abspath(a) if os.path.exists(a) else a for a in sys.argv[1:]]   # the engine runs from a temp folder
    if not engine:
        sys.exit(__doc__)
    work = tempfile.mkdtemp(prefix="slidebridge-test-")
    pl = os.path.join(work, "Test.proplaylist")
    make_fixture(pl)

    def run(*args, ok=True):
        r = subprocess.run(engine + ["--proplaylist", pl, *args], capture_output=True, text=True, cwd=work)
        if ok and r.returncode != 0:
            print(r.stdout[-600:], r.stderr[-600:])
        return r

    r = run("--playlists", "--json")
    tree = json.loads(r.stdout)["tree"] if r.returncode == 0 else []
    items = tree[0]["items"] if tree else []
    check("playlist is read", bool(tree) and tree[0]["name"] == "Test Playlist", r.stderr[-200:])
    check("items: header, presentation, placeholder, empty placeholder",
          [i["kind"] for i in items] == ["header", "presentation", "placeholder", "placeholder"], str([i["kind"] for i in items]))
    check("statuses: ok, ok, empty placeholder flagged", [i["status"] for i in items][1:] == ["ok", "ok", "empty"])

    r = run("--placeholders", "--json")
    ph = json.loads(r.stdout)["placeholders"] if r.returncode == 0 else []
    check("2 placeholders listed with their playlist", len(ph) == 2 and all(p["playlist"] == "Test Playlist" for p in ph))

    r = run("--playlist", "Test Playlist", "--item", "2", "--preview", os.path.join(work, "prev"), "--json")
    total = json.loads(r.stdout)["total_seconds"] if r.returncode == 0 else None
    check("preview length = timer 2 s + default 5 s", total == 7.0, str(total))
    r = run("--playlist", "Test Playlist", "--item", "2", "--preview", os.path.join(work, "prev2"), "--json", "--hold", "4")
    total = json.loads(r.stdout)["total_seconds"] if r.returncode == 0 else None
    check("--hold 4 changes the slide without a timer: 2 + 4 s", total == 6.0, str(total))

    out = os.path.join(work, "out")
    r = run("--playlist", "Test Playlist", "--item", "2", "--pdf-only", "--outdir", out)
    pdf = os.path.join(out, "Songs.pdf")
    pages = len(re.findall(rb"/Type\s*/Page\b(?!s)", open(pdf, "rb").read())) if os.path.exists(pdf) else 0
    check("PDF is written with 2 pages", r.returncode == 0 and pages == 2, f"pages={pages}")

    r = run("--playlist", "Test Playlist", "--item", "3", "--outdir", out)           # a placeholder with a linked presentation
    mp4 = os.path.join(out, "Announcements.mp4")
    check("MP4 is written for a linked placeholder", r.returncode == 0 and os.path.exists(mp4) and os.path.getsize(mp4) > 1000)

    r = run("--playlist", "Test Playlist", "--item", "4", "--pdf-only", "--outdir", out, ok=False)
    check("empty placeholder gives a clear message", r.returncode != 0 and "EMPTY placeholder" in (r.stderr + r.stdout))

    print("\n%d check(s) failed" % len(FAILS) if FAILS else "\nAll checks passed")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
