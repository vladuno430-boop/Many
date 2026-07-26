---
name: pic-search
description: "Reverse image search to identify where a picture came from — which anime a screenshot is from (with exact episode and timestamp) via trace.moe, and which artist or post an illustration came from via SauceNAO. Use this whenever someone shares or points at an image and wants to know its source, origin, artist, or what show/episode it is from — including phrasings like 'what anime is this', 'sauce?', 'where is this from', 'who drew this', 'find the original', 'identify this screenshot', or 'reverse image search this'. Also use it when the user gives an image URL or a local file path and asks what it is. Prefer this over guessing from visual appearance: recognising art styles by eye is unreliable, and this returns a verifiable source link."
---

# Reverse image search

Identify the origin of an image by querying reverse-image-search engines, rather
than guessing from what the picture looks like. Visual recall is unreliable and
confidently wrong answers are worse than none — these engines return a
similarity score and a source link you can check.

## Which engine to use

The two engines index completely different things, which is the main thing to
get right:

| Engine | Indexes | Key needed | Good for |
|---|---|---|---|
| **trace.moe** | anime **video frames** | no | screenshots from anime episodes |
| **SauceNAO** | illustrations, manga, doujin | yes, free | Pixiv/Danbooru art, covers, manga pages |

trace.moe only knows frames that appear in anime episodes. A cover image, promo
art, a manga page, or an original illustration will *not* match it — that is not
a failure of the tool, it is out of scope. When the image is artwork rather than
a screenshot, you need SauceNAO.

## Running it

The script uses only the Python standard library, so there is nothing to install.

```bash
# Anime screenshot — the common case, no API key required
python3 .claude/skills/pic-search/scripts/picsearch.py screenshot.png

# Works with a URL just as well as a local path
python3 .claude/skills/pic-search/scripts/picsearch.py "https://example.com/frame.jpg"

# Artwork: needs a free key from https://saucenao.com/user.php
export SAUCENAO_API_KEY=...
python3 .claude/skills/pic-search/scripts/picsearch.py art.jpg --engine all
```

Useful flags: `--engine trace|sauce|all`, `--min 0.87` to hide weak matches,
`--limit N`, `--json` for structured output, `--quota` to check remaining
searches, `--no-cut-borders` if the image genuinely has content in letterbox bars.

## Reading the result — the part that matters

Every result carries a similarity score, and **trace.moe treats roughly 87% as
the line between a real match and noise**. The script labels anything below it
`weak — treat with suspicion` and prints a summary note.

Report this honestly. A 70% match is usually the engine's best guess among
millions of frames, not the answer, and presenting it as "this is from X" is how
people end up misinformed. Say something like "no confident match — the closest
was Eyeshield 21 at 71%, which is below the reliability threshold" and suggest
the alternatives below. A 99% match, by contrast, is safe to state plainly along
with the episode and timestamp.

Two other fields deserve a mention when present: results flagged adult by AniList
(the script prints `Flagged adult by AniList`), and the preview clip URL, which
lets the user confirm the match themselves in a couple of seconds. Offering that
link is usually more useful than adding your own commentary.

## When nothing matches

Work through these before concluding the image is unidentifiable:

- **Is it actually a screenshot?** If it is artwork, trace.moe was never going to
  find it. Switch to `--engine all` with a SauceNAO key.
- **Is the frame cropped or overlaid?** Heavy cropping, subtitles, watermarks and
  meme text all hurt matching. An uncropped version usually works.
- **Is it too new or too obscure?** trace.moe indexes a large but finite library,
  and very recent episodes may not be in it yet.
- **Is it live action, a game, or 3D?** Neither engine covers those; Google Lens
  or Yandex are the right tools, and it is fair to just say so.

## Limits worth knowing before you start

trace.moe allows about **100 searches per day per IP address**, one at a time —
check with `--quota` if you are about to run a batch. A 429 error means you have
hit the ceiling; waiting is the only fix. Uploads are capped at 25MB.

SauceNAO sits behind Cloudflare and applies its own per-key daily limits. On some
networks it answers with an HTML challenge page instead of JSON; the script
detects this and tells you rather than failing obscurely.

Both engines need outbound internet access. If the environment has none, say so
plainly instead of retrying.
