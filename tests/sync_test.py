"""Checks for the playlist sync (creator's file + operator's ProPresenter, merged). Everything is built from small made-up
files in a temporary folder: no ProPresenter files are touched.
Usage:  python3 tests/sync_test.py        (run from the repository root)"""
import hashlib, os, shutil, sys, tempfile, uuid
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))
import pp2mp4 as P
import ppimport as I
import ppsync as Y

I.init(P)
Y.init(P, I)
S, M, enc = Y.S, Y.M, I.enc
fails = []


def check(name, ok):
    print(("PASS  " if ok else "FAIL  ") + name)
    if not ok:
        fails.append(name)


def U(x):
    """A real-looking id for a short name (made-up strings of other shapes can be read as nested messages by the file reader)."""
    return str(uuid.UUID(hashlib.md5(x.encode()).hexdigest())).upper()


def idn(u): return (1, 2, M([(1, 2, S(U(u)))]))
def cue(u, text): return (13, 2, M([idn(u), (2, 2, S(text))]))
def group(g, name, cues): return (12, 2, M([(1, 2, M([idn(g), (2, 2, S(name))]))] + [(2, 2, M([(1, 2, S(U(c)))])) for c in cues]))
def arrangement(a, name, groups): return (11, 2, M([idn(a), (2, 2, S(name))] + [(3, 2, M([(1, 2, S(U(g)))])) for g in groups]))


def pro(uid, name, groups, cues, arrs):
    return enc([(1, 2, M([(1, 0, 1)])), (2, 2, M([(1, 2, S(U(uid)))])), (3, 2, S(name))] + arrs + groups + cues)


URL = "file:///Users/someone/Library/Application%20Support/RenewedVision/ProPresenter/UserWorkspaces/ProPresenter/"


def item(u, name, rel):
    return (1, 2, M([idn(u), (2, 2, S(name)),
                     (4, 2, M([(1, 2, M([(1, 2, S(URL + rel.replace(" ", "%20"))), (3, 0, 1), (4, 2, M([(1, 0, 10), (2, 2, S(rel))]))])), (4, 2, M([(1, 0, 7)]))]))]))


def doc(playlists):
    pls = [(1, 2, M([(1, 2, M([(1, 2, S(u))])), (2, 2, S(n)), (13, 2, M(items))])) for u, n, items in playlists]
    return enc([(1, 2, M([(1, 0, 1)])), (2, 0, 0), (3, 2, M([(1, 2, M([(1, 0, 1)])), (12, 2, M(pls))]))])


def bundle(path, playlist, pros):
    I.write_bundle(path, doc([playlist]), pros, {})


def song(uid, title, verse, chorus, order):
    """Verse (2 slides), Chorus (1 slide); `order` is the arrangement: 'VCV' = verse, chorus, verse."""
    cv1, cv2, cc = uid + "-c1", uid + "-c2", uid + "-c3"
    gv, gc = uid + "-gv", uid + "-gc"
    arr = arrangement(uid + "-a", "Sunday", [gv if x == "V" else gc for x in order])
    return pro(uid, title, [group(gv, "Verse", [cv1, cv2]), group(gc, "Chorus", [cc])],
               [cue(cv1, verse[0]), cue(cv2, verse[1]), cue(cc, chorus)], [arr])


def texts(data):
    return [P.sval(P.first(P.msg(v), 2)) for f, w, v in P.parse(data) if f == 13]


def arr_order(data):
    t = P.parse(data)
    names = {Y.chain(v): P.sval(P.first(P.get(P.msg(v), 1), 2)) for f, w, v in t if f == 12}
    a = [v for f, w, v in t if f == 11][0]
    return "".join(names[P.sval(P.first(P.msg(x), 1))][0] for x in P.fields(P.msg(a), 3))


tmp = tempfile.mkdtemp(prefix="sync_test_")
root, state = os.path.join(tmp, "PP"), os.path.join(tmp, "state")
os.makedirs(os.path.join(root, "Playlists"))
os.makedirs(os.path.join(root, "Libraries", "Church"))
open(os.path.join(root, "Playlists", "Library"), "wb").write(doc([(U("other"), "Other", [item("i-o", "Other item", "Libraries/Church/Other.pro")])]))
UID = U("playlist")
REL = lambda n: f"Libraries/Church/{n}.pro"
lf = os.path.join(root, "Playlists", "Library")
rd = lambda n: open(os.path.join(root, "Libraries", "Church", n + ".pro"), "rb").read()


def warta(t): return pro("W1", "Warta", [group("W-g", "", ["W-c"])], [cue("W-c", t)], [])


base_items = [item("i-w", "Warta", REL("Warta")), item("i-a", "Song A", REL("Song A")), item("i-b", "Song B", REL("Song B"))]
v1_pros = {"Warta.pro": warta("Warta week 1"),
           "Song A.pro": song("A", "Song A", ("a1", "a2"), "chorus A", "VCV"),
           "Song B.pro": song("B", "Song B", ("b1", "b2"), "chorus B", "VC")}
f1 = os.path.join(tmp, "v1.proplaylist")
bundle(f1, (UID, "KU test", base_items), v1_pros)


def sync(f, **kw):
    return Y.run(f, root, state, allow_running=True, **kw)


def mine(u=None):
    d = P.parse(open(lf, "rb").read())
    return next(p for p in Y.find_playlists(P.msg(P.first(P.get(d, 3), 12))) if P.uuid_of(p) == (u or UID))


# ---- the first sync on a computer that has nothing yet: everything arrives
plan = sync(f1)
check("a plan writes nothing", not plan["applied"] and not os.path.exists(rd.__code__ and os.path.join(root, "Libraries", "Church", "Warta.pro")))
res = sync(f1, apply=True)
check("first sync applied, with a backup", res["applied"] and os.path.isdir(res["backup"]))
check("first sync: the presentations were written", all(os.path.exists(os.path.join(root, "Libraries", "Church", n)) for n in ("Warta.pro", "Song A.pro", "Song B.pro")))
pls = Y.find_playlists(P.msg(P.first(P.get(P.parse(open(lf, "rb").read()), 3), 12)))
check("the other playlist is untouched and the new one is added", sorted(P.sval(P.first(p, 2)) for p in pls) == ["KU test", "Other"])
check("addresses in the written files point to this computer", b"/Users/someone/" not in enc(mine()) and root.encode().replace(b" ", b"%20") in enc(mine()))
res = sync(f1)
check("syncing the same file again changes nothing", res["summary"]["creator"] == 0 and res["summary"]["merged"] == 0 and res["summary"]["conflicts"] == 0 and all(r["status"] == "same" for r in res["presentations"]))


# ---- ProPresenter gives an imported playlist new ids, and this church keeps its songs in another library folder
LUID, LOCAL = U("church-playlist"), "Local Lib"
os.rename(os.path.join(root, "Libraries", "Church"), os.path.join(root, "Libraries", LOCAL))
rd = lambda n: open(os.path.join(root, "Libraries", LOCAL, n + ".pro"), "rb").read()
ld = P.parse(open(lf, "rb").read())
cur = mine()
its = []
for k, it in Y.playlist_items(cur):
    it2 = I.remap_ids(it)
    kind, rel, nm = Y.item_ref(it)
    its.append((1, 2, M(Y.retarget(it2, rel, rel.replace("Church", LOCAL)))))
pls2, _ = Y.replace_playlist(P.msg(P.first(P.get(ld, 3), 12)), UID, [(1, 2, M([(1, 2, S(LUID))])), (2, 2, P.first(cur, 2)), (13, 2, M(its))])
open(lf, "wb").write(enc([(f, w, M([(ff, ww, M(pls2) if ff == 12 else vv) for ff, ww, vv in P.msg(v)]) if f == 3 else v) for f, w, v in ld]))
res = sync(f1)
check("after the playlist got new ids and another folder, the same file still shows nothing to do", res["summary"]["creator"] == 0 and res["summary"]["merged"] == 0 and not any(r["conflict"] for r in res["items"]) and all(r["status"] == "same" for r in res["presentations"]))

# ---- the operator practises at church: Song A arrangement, Song B text, an extra item
open(os.path.join(root, "Libraries", LOCAL, "Song A.pro"), "wb").write(song("A", "Song A", ("a1", "a2"), "chorus A", "VVC"))
open(os.path.join(root, "Libraries", LOCAL, "Song B.pro"), "wb").write(song("B", "Song B", ("b1 (practice)", "b2"), "chorus B", "VC"))
open(os.path.join(root, "Libraries", LOCAL, "Note.pro"), "wb").write(warta("church note"))
ld = P.parse(open(lf, "rb").read())
cur = mine(LUID)
items = [(1, 2, M(it)) for k, it in Y.playlist_items(cur)] + [item("i-n", "Church note", "Libraries/%s/Note.pro" % LOCAL)]
new_pl = [x for x in cur if x[0] != 13] + [(13, 2, M(items))]
pls2, _ = Y.replace_playlist(P.msg(P.first(P.get(ld, 3), 12)), LUID, new_pl)
open(lf, "wb").write(enc([(f, w, M([(ff, ww, M(pls2) if ff == 12 else vv) for ff, ww, vv in P.msg(v)]) if f == 3 else v) for f, w, v in ld]))

# ---- the creator updates from home, starting from the version the church had: Warta, a Song A lyric, a new Song C
v2_pros = {"Warta.pro": warta("Warta week 2"),
           "Song A.pro": song("A", "Song A", ("a1", "a2 fixed"), "chorus A", "VCV"),
           "Song B.pro": v1_pros["Song B.pro"],
           "Song C.pro": song("C", "Song C", ("c1", "c2"), "chorus C", "VC")}
v2_items = base_items + [item("i-c", "Song C", REL("Song C"))]
f2 = os.path.join(tmp, "v2.proplaylist")
bundle(f2, (UID, "KU test", v2_items), v2_pros)

plan = sync(f2)
st = {r["item"]: r["status"] for r in plan["presentations"]}
check("Warta: only the creator changed it -> theirs", st["Warta"] == "creator changed")
check("Song A: both changed different pieces -> merged, no conflict", st["Song A"] == "merged" and plan["summary"]["conflicts"] == 0)
check("Song B: only the operator changed it -> kept", st["Song B"] == "operator changed")
check("Song C: new from the creator", st["Song C"] == "new")
its = {r["item"]: r["status"] for r in plan["items"]}
check("the operator's extra item is kept, the new item is new", its["Church note"] == "added at church" and its["Song C"] == "new from creator")
res = sync(f2, apply=True)
check("Warta now has the creator's text", texts(rd("Warta")) == ["Warta week 2"])
check("Song A has the creator's lyric AND the operator's arrangement", "a2 fixed" in texts(rd("Song A")) and arr_order(rd("Song A")) == "VVC")
check("Song B keeps the operator's text", texts(rd("Song B"))[0] == "b1 (practice)")
check("Song C arrived", os.path.exists(os.path.join(root, "Libraries", LOCAL, "Song C.pro")))
names = [P.sval(P.first(it, 2)) for k, it in Y.playlist_items(mine(LUID))]
check("playlist: the creator's items in order, the operator's extra and the new song both present", names[:3] == ["Warta", "Song A", "Song B"] and set(names[3:]) == {"Church note", "Song C"})
check("the backup holds the files that were replaced", os.path.exists(os.path.join(res["backup"], "Libraries", LOCAL, "Warta.pro")) and os.path.exists(os.path.join(res["backup"], "Playlists-Library")))
res = sync(f2)
check("after the update, the same file shows nothing left to do", res["summary"]["creator"] == 0 and res["summary"]["conflicts"] == 0 and res["summary"]["merged"] == 0)

# ---- a real conflict: the creator and the operator both change Song B's first slide
v3_pros = dict(v2_pros)
v3_pros["Song B.pro"] = song("B", "Song B", ("b1 by creator", "b2"), "chorus B", "VC")
f3 = os.path.join(tmp, "v3.proplaylist")
bundle(f3, (UID, "KU test", v2_items), v3_pros)
plan = sync(f3)
conf = [c for r in plan["presentations"] for c in r["conflicts"]]
check("same slide changed twice: one conflict, the operator's is kept by default", len(conf) == 1 and conf[0]["kind"] == "slide" and conf[0]["kept"] == "operator")
shutil.copytree(root, root + ".before")
shutil.copytree(state, state + ".before")
sync(f3, apply=True)
check("the operator's text stays", texts(rd("Song B"))[0] == "b1 (practice)")
shutil.rmtree(root)
shutil.rmtree(state)
shutil.move(root + ".before", root)
shutil.move(state + ".before", state)
sync(f3, prefer="creator", apply=True)
check("asked to prefer the creator: their text wins", texts(rd("Song B"))[0] == "b1 by creator")

# ---- the safety rules
if Y.propresenter_running():
    res = Y.run(f3, root, state, apply=True)
    check("ProPresenter is open: refuses to write", "error" in res and not res["applied"])
else:
    print("skip  ProPresenter is not running: the 'open' check was not exercised")

print([os.path.relpath(os.path.join(a, f), tmp) for a, d, fs in os.walk(root) for f in fs if f.endswith(".pro")]); shutil.rmtree(tmp)
print()
if fails:
    print("FAILED:", ", ".join(fails))
    sys.exit(1)
print("All checks passed")
