<p align="center"><img src="app/renderer/logo.png" width="128" alt="SlideBridge logo"></p>

# SlideBridge: ProPresenter converter to MP4 and PDF

Convert a ProPresenter playlist item (a song, the announcements, a liturgy slide, a whole playlist) into an
**MP4 video** or a **PDF**, without opening ProPresenter. Pick the playlist, tick the items, choose the format and
the folder, and click **Make**. It also reads a `.proplaylist` file directly, without importing it.

## Download

Get the latest `.dmg` from the [Releases page](../../releases/latest):

| Your Mac | File |
|---|---|
| Apple Silicon (M1 and newer) | `SlideBridge-<version>-arm64.dmg` |
| Intel | `SlideBridge-<version>-x64.dmg` |

The app is **not notarized by Apple**. First time: drag it to Applications, right-click it, choose **Open**, then **Open**.
If macOS says it is damaged, run once in Terminal: `xattr -dr com.apple.quarantine /Applications/SlideBridge.app`

## What it does

- Reads ProPresenter 7 playlists, including folders, headers, media items and **placeholders** (and shows which
  playlist each placeholder belongs to, and which are still empty).
- Shows a preview of every slide the way ProPresenter lists them, with the group colours.
- Makes **MP4** (1920x1080, 30 fps, with video and sound), **PDF** (one page per slide), or both.
- You choose the output folder and how long each picture stays on screen; after a video it can go straight to
  the next slide.
- Keeps the **text you typed on a video or picture** in ProPresenter (for example a thank-you slide) and centres it
  the way ProPresenter does.
- In the preview, set for each slide **how long it is shown** and, for videos, **which moment becomes the PDF picture**.
- **Three sources, one "Convert to" row.** Pick the source at the top left: **ProPresenter** (your library or a
  `.proplaylist` file) converts to a **PDF** or an **MP4**; **PDF** (a worship script) and **PowerPoint** (the sermon
  deck and the master deck from Church Center) convert to a **ProPresenter playlist**.
- **Playlist from files:** builds a new playlist (`.proplaylist`) from the worship script, the preacher's PowerPoint and the
  master PowerPoint. Any one is enough. Songs get their full lyrics (2 lines per slide by default), the Nats Panggilan and
  Bible reading slides are rebuilt, the sermon deck becomes a presentation, and the look (fonts, colours, groups, actions)
  is copied from a playlist you already finished. A song's motion is picked at random from that playlist's motions that exist
  on your computer; if none does, the song's background picture is taken from the master deck.
- Opens a `.proplaylist` file directly, or reads ProPresenter's own library.

ProPresenter keeps themes outside the presentation files, so text-only slides are drawn on a dark stand-in
background.

## Safety and privacy

SlideBridge only **reads** ProPresenter's files and never changes your ProPresenter library. It does not use the
network and sends nothing anywhere. Media from a `.proplaylist` is unpacked into `~/Library/Caches/pp2mp4`, which you can delete.

## Build it yourself

Needs macOS, Python 3, Node.js and the Xcode command line tools.

```bash
bash scripts/build-macos.sh        # builds for this Mac's CPU -> dist/SlideBridge-<version>-<arch>.dmg
python3 tests/engine_test.py python3 engine/pp2mp4.py   # engine self-test, no ProPresenter needed
```

The engine can also be used on its own: `python3 engine/pp2mp4.py --help`.

A release is made by pushing a tag such as `v0.1.0` (it must match `version` in `app/package.json`); GitHub
Actions then builds the Apple Silicon and Intel `.dmg` files and attaches them to the release.

## License

MIT, see [LICENSE](LICENSE).

SlideBridge is an independent tool. It is not affiliated with or endorsed by Renewed Vision; ProPresenter is a trademark of Renewed Vision.
