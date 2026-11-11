"""Checks for the playlist importer that need no ProPresenter files: text parsing, RTF editing, protobuf writing.
Usage:  python3 tests/import_test.py        (run from the repository root)"""
import os, sys, types
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))
import pp2mp4 as P
import ppimport as I

I.init(P)
fails = []


def check(name, ok):
    print(("PASS  " if ok else "FAIL  ") + name)
    if not ok:
        fails.append(name)


# song title lines in the script
check("title line with key and arrangement", I.looks_like_title_line("Jaminan Mulia| V1-C-V2-C-V3-C-E") == ("Jaminan Mulia", "V1-C-V2-C-V3-C-E"))
check("title line with key letters", I.looks_like_title_line("Satukan Kami | F: B-B-R-R")[0] == "Satukan Kami")
check("title line with bpm", I.looks_like_title_line("Bertumbuh Dalam Kasih | 110 bpm G:B1-B2-R-B1")[0] == "Bertumbuh Dalam Kasih")
check("a credits line is not a title", I.looks_like_title_line("Words by Fanny J. Crosby; Music by Phoebe P. Knapp | Arr. by Cliff Duren") is None)
check("a lyric line is not a title", I.looks_like_title_line("Sungguh alangkah baiknya") is None)

# Bible reading split into P / J / P+J
parts = I.reading_parts(["P: Sungguh, alangkah baiknya,", "apabila saudara diam bersama!", "J: Seperti minyak yang baik,", "P+J: Seperti embun."])
check("reading parts", [p[0] for p in parts] == ["P", "J", "P+J"] and parts[0][1] == "Sungguh, alangkah baiknya,\napabila saudara diam bersama!")

# nats verse split
long = "Tetapi kamulah bangsa yang terpilih, imamat yang rajani, bangsa yang kudus, umat kepunyaan Allah sendiri, supaya kamu memberitakan perbuatan-perbuatan yang besar dari Dia"
check("short verse stays on one slide", len(I.split_verse("Pendek saja.")) == 1)
check("long verse is split at commas", 2 <= len(I.split_verse(long)) <= 3 and " ".join(I.split_verse(long)) == long)

# RTF: the last run's text is replaced, formatting kept
rtf = "{\\rtf1\\ansi\n{\\fonttbl\\f0 X;}\n\\pard\\qc\n\n\\f0\\fs196 \\cf2 \\outl0\\strokewidth-20 \\strokec3 Old text\n\\f1 \\\n\n\\f0 second}"
new = I.rtf_set(rtf, "Kar’na Yesus\nbaris dua")
check("rtf keeps the formatting", "\\fs196 \\cf2 \\outl0\\strokewidth-20 \\strokec3 Kar" in new)
check("rtf escapes the apostrophe and breaks the line", "\\'92na Yesus\\\nbaris dua}" in new and "Old text" not in new)
check("rtf of a run with several sizes keeps the first runs", I.rtf_set("{\\rtf1\\f0\\fs200 \\cf2 P\n\\fs140 :  \n\\fs160 old}", "new").startswith("{\\rtf1\\f0\\fs200 \\cf2 P\n\\fs140 :  \n\\fs160 new"))

# song numbers: two songs in one slot are 4 and 4b
songs = [dict(n=3), dict(n=4), dict(n=4), dict(n=5)]
I.assign_labels(songs)
check("song labels 3, 4, 4b, 5", [s["label"] for s in songs] == ["3", "4", "4b", "5"])

# protobuf: what is written reads back the same
nodes = [(1, 0, 7), (2, 2, I.S("hello")), (3, 2, I.M([(1, 2, I.S("A580084E-8A07-4CFE-AD9C-E6E56935FCE7")), (5, 5, 1.5)])), (4, 1, 2.25)]
back = P.parse(I.enc(nodes))
check("protobuf round trip", back is not None and I.enc(back) == I.enc(nodes))
re_ = I.remap_ids(nodes)
check("ids are renewed, everything else kept", P.get(re_, 3, 1) is None and P.sval(P.first(P.get(re_, 3), 1)) != "A580084E-8A07-4CFE-AD9C-E6E56935FCE7" and P.sval(P.first(re_, 2)) == "hello")

print()
if fails:
    print("FAILED:", ", ".join(fails))
    sys.exit(1)
print("All checks passed")
