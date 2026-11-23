"""Keep the ProPresenter on one computer in step with a .proplaylist from the content creators, without losing what the
operator changed in the meantime.

Three copies are compared, for every playlist item and every piece of every presentation (arrangements, groups, slides):
  base   : the creator's file as it was at the last sync (kept in the state folder)
  local  : what ProPresenter has now (the operator's work)
  remote : the creator's new file
Only the creator changed it -> take theirs. Only the operator changed it -> keep it. Both -> a conflict: the operator's
version stays unless asked otherwise, and the conflict is reported. Nothing is written without apply=True, ProPresenter must
be closed, and the files that are replaced are backed up first.

PROTOTYPE: the merge is tested on synthetic and generated files. It has not been run against a live ProPresenter."""
import glob, hashlib, json, os, re, shutil, subprocess, sys, time, urllib.parse

P = I = None                                                # the engine module and ppimport, set by init()
ROOT_URL = re.compile(r"^file://.*?/(?=(?:Libraries|Media)/)")   # everything before the workspace-relative part
PLACEHOLDER = "file://@ROOT@/"


def init(engine_module, import_module):
    global P, I
    P, I = engine_module, import_module


S = lambda s: ("str", s)
M = lambda n: ("msg", n)


# ---------------------------------------------------------------- tree helpers
def norm(nodes):
    """Absolute file:// paths differ from computer to computer: compare them with the workspace part replaced."""
    return I.replace_strings(nodes, lambda t: ROOT_URL.sub(PLACEHOLDER, t) if t.startswith("file://") else t)


def localize(nodes, root):
    url = "file://" + urllib.parse.quote(os.path.abspath(root).rstrip("/")) + "/"
    return I.replace_strings(nodes, lambda t: ROOT_URL.sub(url, t).replace(PLACEHOLDER, url) if t.startswith("file://") else t)


def canon(nodes):
    return I.enc(norm(nodes))


def chain(v):
    """The id of an arrangement, group, cue or item: the first string along the first fields."""
    n = P.msg(v)
    while n:
        x = P.first(n, 1)
        if isinstance(x, tuple) and x[0] == "str":
            return x[1]
        n = P.msg(x)
    return None


def fp(tree):
    return canon([x for x in tree if x[0] not in (1, 2)])   # application info and the presentation's own id are not content


def three(b, l, r, prefer):
    """-> ('l'|'r', conflict?) for one piece; each argument is the piece's fingerprint (None = absent)."""
    if l == r:
        return "l", False
    if l == b:
        return "r", False
    if r == b:
        return "l", False
    return ("r" if prefer == "creator" else "l"), True


def merge_order(b, l, r):
    if l == b:
        return list(r)
    if r == b:
        return list(l)
    return list(l) + [k for k in r if k not in l and k not in b]


# ---------------------------------------------------------------- presentations
def split_pro(tree):
    d = dict(low=[], high=[], arr={}, groups={}, gorder=[], cues={}, corder=[])
    for f, w, v in tree:
        if f == 11:
            d["arr"][chain(v)] = (f, w, v)
        elif f == 12:
            k = chain(v)
            d["groups"][k] = (f, w, v)
            d["gorder"].append(k)
        elif f == 13:
            k = chain(v)
            d["cues"][k] = (f, w, v)
            d["corder"].append(k)
        elif f < 12:
            d["low"].append((f, w, v))
        else:
            d["high"].append((f, w, v))
    return d


def piece_name(kind, node, idx):
    if kind == "group":
        return P.sval(P.first(P.get(P.msg(node[2]), 1), 2)) or f"group {idx + 1}"
    if kind == "arrangement":
        return P.sval(P.first(P.msg(node[2]), 2)) or "arrangement"
    return P.sval(P.first(P.msg(node[2]), 2)) or f"slide {idx + 1}"


def merge_pro(base, local, remote, prefer):
    """Three trees -> (tree, [conflicts]). Pieces are matched by their ids."""
    B, L, R = split_pro(base), split_pro(local), split_pro(remote)
    conflicts = []

    def pick(kind, bd, ld, rd, order):
        out = {}
        for k in list(dict.fromkeys(list(ld) + list(rd) + list(bd))):
            f = lambda d: canon([d[k]]) if k in d else None
            ch, bad = three(f(bd), f(ld), f(rd), prefer)
            node = (ld if ch == "l" else rd).get(k)
            if bad:
                src = ld.get(k) or rd.get(k) or bd.get(k)
                conflicts.append(dict(kind=kind, name=piece_name(kind, src, order.index(k) if k in order else 0), kept="creator" if ch == "r" else "operator"))
            if node is not None:
                out[k] = node
        return out
    arr = pick("arrangement", B["arr"], L["arr"], R["arr"], [])
    groups = pick("group", B["groups"], L["groups"], R["groups"], L["gorder"])
    cues = pick("slide", B["cues"], L["cues"], R["cues"], L["corder"])
    gord = [k for k in merge_order(B["gorder"], L["gorder"], R["gorder"]) if k in groups]
    gord += [k for k in groups if k not in gord]
    cord = [k for k in merge_order(B["corder"], L["corder"], R["corder"]) if k in cues]
    cord += [k for k in cues if k not in cord]
    meta = lambda s: canon([x for x in s["low"] + s["high"] if x[0] not in (1, 2)])
    ch, bad = three(meta(B), meta(L), meta(R), prefer)
    if bad:
        conflicts.append(dict(kind="settings", name="name or settings", kept="creator" if ch == "r" else "operator"))
    src = L if ch == "l" else R
    low = [x for x in L["low"] if x[0] in (1, 2)] + [x for x in src["low"] if x[0] not in (1, 2)]
    low = sorted(low, key=lambda x: x[0])
    return low + list(arr.values()) + [groups[k] for k in gord] + [cues[k] for k in cord] + src["high"], conflicts


def sync_pres(rel, base, local, remote, root, prefer):
    """-> dict(status, data (bytes to write or None), conflicts)."""
    if remote is None:
        return dict(rel=rel, status="not in the file", data=None, conflicts=[])
    rt = P.parse(remote)
    if rt is None:
        return dict(rel=rel, status="unreadable", data=None, conflicts=[])
    rt = localize(rt, root)
    if local is None:
        return dict(rel=rel, status="new", data=I.enc(rt), conflicts=[])
    lt = P.parse(local)
    if lt is None:
        return dict(rel=rel, status="unreadable", data=None, conflicts=[])
    if fp(lt) == fp(rt):
        return dict(rel=rel, status="same", data=None, conflicts=[])
    if base is None:                                        # first sync: nobody can tell who changed it
        return dict(rel=rel, status="first sync: differs", data=I.enc(rt) if prefer == "creator" else None, conflicts=[], review=True)
    bt = P.parse(base)
    if fp(bt) == fp(rt):
        return dict(rel=rel, status="operator changed", data=None, conflicts=[])
    if fp(bt) == fp(lt):
        return dict(rel=rel, status="creator changed", data=I.enc(rt), conflicts=[])
    tree, conflicts = merge_pro(bt, lt, rt, prefer)
    return dict(rel=rel, status="merged with conflicts" if conflicts else "merged", data=I.enc(tree), conflicts=conflicts)


# ---------------------------------------------------------------- playlists
def find_playlists(pls, out=None):
    out = [] if out is None else out
    for k in P.fields(pls, 1):
        pl = P.msg(k)
        sub = P.first(pl, 12)
        if sub is not None:
            find_playlists(P.msg(sub), out)
        else:
            out.append(pl)
    return out


def replace_playlist(pls, uid, new):
    out, hit = [], False
    for f, w, v in pls:
        pl = P.msg(v)
        if f == 1 and pl is not None:
            if P.uuid_of(pl) == uid:
                out.append((f, w, M(new)))
                hit = True
                continue
            sub = P.first(pl, 12)
            if sub is not None:
                inner, h = replace_playlist(P.msg(sub), uid, new)
                if h:
                    out.append((f, w, M([(ff, ww, M(inner) if ff == 12 else vv) for ff, ww, vv in pl])))
                    hit = True
                    continue
        out.append((f, w, v))
    return out, hit


def playlist_items(pl):
    box = P.first(pl, 13)
    return [(P.uuid_of(P.msg(v)), P.msg(v)) for v in P.fields(P.msg(box), 1)] if box is not None else []


def item_ref(item):
    info = P.item_info(item, 0)
    return info["kind"], (info.get("ref") or {}).get("rel"), info["name"]


LIB_FOLDER = re.compile(r"(Libraries/)[^/]+/")


def norm_item(item):
    """An item without its id, and without the library folder name: both can differ between the creator's computer and ours."""
    def fix(t):
        if t.startswith("file://"):
            return LIB_FOLDER.sub(r"\1@/", ROOT_URL.sub(PLACEHOLDER, t))
        return LIB_FOLDER.sub(r"\1@/", t) if t.startswith("Libraries/") else t
    return I.enc(I.replace_strings([x for x in item if x[0] != 1], fix))


def item_keys(items):
    """ProPresenter gives every playlist and item new ids when a file is imported, so items are matched by what they are:
    kind, file name (or name for headers) and the n-th time that appears."""
    seen, out = {}, []
    for it in items:
        kind, rel, name = item_ref(it)
        base = os.path.basename(rel) if rel else name
        k = (kind, base)
        out.append(f"{kind}|{base}|{seen.get(k, 0)}")
        seen[k] = seen.get(k, 0) + 1
    return out


def local_rel(root, base, fallback, default_dir=None):
    """Where this computer keeps a presentation: where it already is, else the folder the rest of the playlist lives in."""
    hits = sorted(glob.glob(os.path.join(glob.escape(root), "Libraries", "*", glob.escape(base))))
    if hits:
        return os.path.relpath(hits[0], root)
    return f"{default_dir}/{base}" if default_dir else fallback


def retarget(item, old, new):
    """Point an item at another library folder."""
    if old == new:
        return item
    qo, qn = urllib.parse.quote(old), urllib.parse.quote(new)
    return I.replace_strings(item, lambda t: new if t == old else (t[:-len(qo)] + qn if t.startswith("file://") and t.endswith(qo) else t))


class State:
    """What the last sync saw: the creator's playlist and presentations."""

    def __init__(self, folder, uid):
        self.dir = os.path.join(folder, uid)

    def load(self):
        try:
            idx = json.load(open(os.path.join(self.dir, "index.json")))
            pl = open(os.path.join(self.dir, "playlist.bin"), "rb").read()
        except Exception:
            return None, {}
        return pl, idx

    def local_uuid(self):
        try:
            return json.load(open(os.path.join(self.dir, "local.json"))).get("uuid")
        except Exception:
            return None

    def pro(self, idx, rel):
        f = idx.get(rel)
        p = os.path.join(self.dir, "pro", f) if f else None
        return open(p, "rb").read() if p and os.path.exists(p) else None

    def save(self, playlist_bytes, pros, local_uuid=None):
        os.makedirs(os.path.join(self.dir, "pro"), exist_ok=True)
        idx = {}
        for rel, data in pros.items():
            fn = hashlib.sha1(rel.encode()).hexdigest() + ".pro"
            open(os.path.join(self.dir, "pro", fn), "wb").write(data)
            idx[rel] = fn
        open(os.path.join(self.dir, "playlist.bin"), "wb").write(playlist_bytes)
        json.dump(idx, open(os.path.join(self.dir, "index.json"), "w"))
        json.dump(dict(uuid=local_uuid), open(os.path.join(self.dir, "local.json"), "w"))


def propresenter_running():
    try:
        return bool(subprocess.run(["pgrep", "-x", "ProPresenter"], capture_output=True, text=True).stdout.strip())
    except Exception:
        return False


def run(remote_path, root, state_dir, apply=False, prefer="operator", playlist=None, allow_running=False, local_uuid=None):
    """Compare (and with apply=True, update) the ProPresenter workspace `root` from the creator's .proplaylist."""
    root = os.path.abspath(os.path.expanduser(root))
    bundle = P.Bundle(remote_path)
    rdoc = P.parse(bundle.read("data"))
    rpls = find_playlists(P.msg(P.first(P.get(rdoc, 3), 12)))
    rpls = [p for p in rpls if not playlist or (P.sval(P.first(p, 2)) or "") == playlist] or rpls
    if not rpls:
        sys.exit("No playlist found in that file.")
    rpl = max(rpls, key=lambda p: len(playlist_items(p)))
    uid, name = P.uuid_of(rpl), P.sval(P.first(rpl, 2)) or ""
    lfile = os.path.join(root, "Playlists", "Library")
    ldoc = P.parse(open(lfile, "rb").read()) if os.path.exists(lfile) else None
    lpls = find_playlists(P.msg(P.first(P.get(ldoc, 3), 12))) if ldoc else []
    st = State(state_dir, uid)
    bpl_bytes, bidx = st.load()
    bpl = P.parse(bpl_bytes) if bpl_bytes else None
    first_sync = bpl is None

    # --- which playlist at church is this one? (ProPresenter gives imported playlists new ids, so the name is used too)
    if local_uuid:
        cands = [p for p in lpls if P.uuid_of(p) == local_uuid]
    else:
        cands = [p for p in lpls if P.uuid_of(p) == uid] or [p for p in lpls if st.local_uuid() and P.uuid_of(p) == st.local_uuid()] \
            or [p for p in lpls if (P.sval(P.first(p, 2)) or "") == name]
    if len(cands) > 1:
        return dict(playlist=name, uuid=uid, applied=False, error=f"{len(cands)} playlists at church are called '{name}'. Say which one to update.",
                    candidates=[dict(uuid=P.uuid_of(p), items=len(playlist_items(p))) for p in cands])
    lpl = cands[0] if cands else None
    local_id = P.uuid_of(lpl) if lpl is not None else uid

    # --- the playlist's items: each one is the creator's, the operator's, or both merged
    def keyed(nodes):
        ks = item_keys(nodes)
        return dict(zip(ks, nodes)), ks
    rd, rk = keyed([localize([(1, 2, M(it))], root)[0][2][1] for k, it in playlist_items(rpl)])
    ld, lk = keyed([it for k, it in playlist_items(lpl)]) if lpl is not None else ({}, [])
    bd, bk = keyed([it for k, it in playlist_items(bpl)]) if bpl else ({}, [])
    fpi = lambda d, k: norm_item(d[k]) if k in d else None
    dirs = [os.path.dirname(r) for r in (item_ref(it)[1] for it in ld.values()) if r]
    default_dir = max(set(dirs), key=dirs.count) if dirs else None
    merged, item_report = {}, []
    for k in list(dict.fromkeys(lk + rk + bk)):
        if lpl is None:
            ch, bad = "r", False
        elif first_sync:
            ch, bad = ("l", False) if k in ld else ("r", False)
        else:
            ch, bad = three(fpi(bd, k), fpi(ld, k), fpi(rd, k), prefer)
        node = (ld if ch == "l" else rd).get(k)
        src = node if node is not None else (ld.get(k) or rd.get(k) or bd.get(k))
        kind, rel, iname = item_ref(src)
        if node is not None and ch == "r" and rel:             # the creator's item points at this computer's library folder
            node = retarget(node, rel, local_rel(root, os.path.basename(rel), rel, default_dir))
        if node is not None and ch == "r" and k not in ld:
            node = I.remap_ids(node)
        if node is not None:
            merged[k] = node
        if bad:
            status = "both changed: " + ("creator's kept" if ch == "r" else "operator's kept")
        elif k in ld and k in rd:
            status = "same" if fpi(ld, k) == fpi(rd, k) else ("kept (operator)" if ch == "l" else "updated by creator")
        elif k in rd and k not in ld:
            status = "removed at church (kept removed)" if (k in bd and ch == "l") else "new from creator"
        else:
            status = "added at church" if k not in bd else "removed by creator"
        item_report.append(dict(item=iname, kind=kind, status=status, conflict=bad))
    if lpl is None:
        order = list(rk)
    elif first_sync:
        order = lk + [k for k in rk if k not in ld]
    else:
        order = merge_order(bk, lk, rk)
    order = [k for k in order if k in merged] + [k for k in merged if k not in order]
    name_node = S(name)                                     # the operator's name for it stays; the creator's new name is taken only if the operator did not rename it
    if lpl is not None and (bpl is None or P.sval(P.first(lpl, 2)) != P.sval(P.first(bpl, 2))):
        name_node = P.first(lpl, 2)
    new_pl = [(1, 2, M([(1, 2, S(local_id))])), (2, 2, name_node), (13, 2, M([(1, 2, M(merged[k])) for k in order]))]

    # --- the presentations the merged playlist uses
    results, seen = [], set()
    for k in order:
        kind, rel, iname = item_ref(merged[k])
        if not rel or rel in seen or kind == "header":
            continue
        seen.add(rel)
        base_name = os.path.basename(rel)
        hits = bundle.by_base.get(base_name.lower(), [])
        remote = bundle.read(hits[0]) if hits else None
        lpath = os.path.join(root, rel)
        local = open(lpath, "rb").read() if os.path.exists(lpath) else None
        res = sync_pres(rel, None if first_sync else st.pro(bidx, base_name), local, remote, root, prefer)
        res["item"] = iname
        results.append(res)

    # --- the media the written presentations need and ProPresenter does not have yet
    media_copy, media_missing = [], []
    for res in results:
        if res["data"] is None:
            continue
        for rel_m in set(re.findall(r"(Media/[^\x00-\x1f\"]+?\.\w{2,5})", res["data"].decode("latin-1"))):
            if os.path.exists(os.path.join(root, rel_m)):
                continue
            src = next((n for n in bundle.entries if n.endswith("/" + rel_m) or n == rel_m), None)
            (media_copy if src else media_missing).append((rel_m, src))

    n_conf = sum(len(r["conflicts"]) for r in results) + sum(1 for r in item_report if r["conflict"])
    out = dict(playlist=name, uuid=uid, first_sync=first_sync, applied=False,
               summary=dict(creator=sum(1 for r in results if r["status"] in ("creator changed", "new")),
                            operator=sum(1 for r in results if r["status"] == "operator changed"),
                            merged=sum(1 for r in results if r["status"].startswith("merged")),
                            review=sum(1 for r in results if r.get("review")), conflicts=n_conf,
                            same=sum(1 for r in results if r["status"] == "same")),
               items=item_report, presentations=[dict(item=r["item"], status=r["status"], conflicts=r["conflicts"]) for r in results],
               media_to_copy=len(media_copy), media_missing=sorted(set(m for m, _ in media_missing)))
    if not apply:
        return out
    if propresenter_running() and not allow_running:
        out["error"] = "ProPresenter is open. Quit it first: it would overwrite the update when it closes."
        return out
    if ldoc is None:
        out["error"] = "ProPresenter's playlist file was not found in that folder."
        return out
    bdir = os.path.join(state_dir, "backups", time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(bdir, exist_ok=True)
    shutil.copy2(lfile, os.path.join(bdir, "Playlists-Library"))
    for res in results:
        lp = os.path.join(root, res["rel"])
        if res["data"] is not None and os.path.exists(lp):
            os.makedirs(os.path.dirname(os.path.join(bdir, res["rel"])), exist_ok=True)
            shutil.copy2(lp, os.path.join(bdir, res["rel"]))
    for rel_m, src in media_copy:
        dest = os.path.join(root, rel_m)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(bundle.extract(src), dest)
    for res in results:
        if res["data"] is not None:
            lp = os.path.join(root, res["rel"])
            os.makedirs(os.path.dirname(lp), exist_ok=True)
            open(lp, "wb").write(res["data"])
    root_nodes = P.get(ldoc, 3)
    pls = P.msg(P.first(root_nodes, 12))
    pls2 = replace_playlist(pls, local_id, new_pl)[0] if lpl is not None else pls + [(1, 2, M(new_pl))]
    doc2 = [(f, w, M([(ff, ww, M(pls2) if ff == 12 else vv) for ff, ww, vv in root_nodes]) if f == 3 else v) for f, w, v in ldoc]
    open(lfile, "wb").write(I.enc(doc2))
    pros = {}
    for res in results:                                     # the next sync compares against what the creator's file says now
        hits = bundle.by_base.get(os.path.basename(res["rel"]).lower(), [])
        if hits:
            pros[os.path.basename(res["rel"])] = bundle.read(hits[0])
    st.save(I.enc(rpl), pros, local_id)
    out.update(applied=True, backup=bdir)
    return out
