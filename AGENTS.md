# AGENTS.md — operating Note Cut as an agent

This file is for an AI agent (or a script) that edits video and receives review comments through Note Cut.
It is deliberately procedural. Follow it in order. Every rule here exists because skipping it once cost a real edit.

If you are a human, read [README.md](README.md) instead; this file assumes you can run shell commands.

---

## 0. The contract in five lines

1. **Note Cut is the source of truth for the state of an edit.** Not your context window, not the chat, not your notes.
2. **`saved` comments are private drafts. Never act on them.** Only `sent` comments are work. `done` is what you produce.
3. **Nothing is done until it is on the server.** A render nobody can open at `/v/<id>` and a comment without a `complete` record did not happen.
4. **Write state back before you stop.** If your session can end (compaction, timeout, crash), the next agent must be able to resume from `notecut handoff <id>` alone.
5. **Never modify the source media, and never edit `data/` by hand.** Renders are derived from an EDL; the ledger is append-only and only the server writes it.

---

## 1. Setup (once per environment)

```bash
pip install git+https://github.com/Cookiebubba/notecut   # or: pip install /path/to/notecut  (stdlib only)
export NOTECUT_URL=http://<host>:8808   # the running server; default http://127.0.0.1:8808
export NOTECUT_BY=<your-agent-name>     # stamped on every complete / state write / log line
notecut videos                          # must list at least one id; exit 2 = cannot reach the server
```

`notecut` is a thin HTTP client; everything it does is also plain `GET`/`POST` JSON (see the API table in the README) if you prefer `curl` or `requests`.

Environment check before any work:

| Check | Command | Expect |
|---|---|---|
| Server reachable | `curl -s $NOTECUT_URL/api/health` | `{"ok": true, "videos": N, …}` |
| Video exists | `notecut videos` | your `<id>` in the list |
| You can write | `notecut log <id> "agent <name> online"` | `ok log (…)` |

---

## 2. Resume protocol (start of EVERY session)

Run these, in this order, before reasoning about the edit. Do not rely on memory of a previous session.

```bash
notecut handoff <id>                    # 1. the brief: summary, open items, rules, comment table, state sections
notecut comments <id> --open --json     # 2. machine-readable rows with status == "sent"
notecut state get <id>                  # 3. the full state document (renders, timeline, tools, log …)
```

Read the handoff top to bottom. The sections mean:

| Section | Written by | What to do with it |
|---|---|---|
| **Summary / How to resume / Rules that bind this edit** | a previous agent (state keys `summary`, `resume`, `rules`) | Obey. If a rule here conflicts with your defaults, the rule wins. |
| **Open items (owner / agent)** | previous agent (state key `open_items`) | Cross-check against the comment table — the table is authoritative, `open_items` is a note. |
| **Comments table** | the server, from the ledger | `sent` rows are your queue. `saved` rows are the owner's drafts — surface them in your report ("1 saved draft not acted on"), never act. `done` rows are finished; do not redo. |
| **Source / Transcript / EDL / Renders / Tools / Log** | previous agents (state keys of the same names) | Paths and versions you build on. `renders.current.file` is what the reviewer is looking at. |

If `handoff` says `state updated never`, this is a fresh video: create the state (§5) before anything else so the next agent is not in your position.

---

## 3. Reading a comment

Each folded row has `id`, `kind`, `status`, `body`, `at`, `cur_t`, and kind-specific fields:

| `kind` | Fields | Meaning |
|---|---|---|
| `point` | `clip_t`, `cur_t` | A note at a moment. `clip_t` is on the render it was made on; **`cur_t`** is the same moment on the *current* render (remapped through `prior_edl` → `edl`). Use `cur_t` to locate it, then map to source time through the EDL. |
| `transcript` | `src_start`, `src_end`, `sel`, `i0`, `i1`, (`edge`, `old_i`, `new_i` when the reviewer nudged a cut point) | A drag across the transcript. **`src_*` are SOURCE timestamps** and never move between renders. `sel` is the selected text. The body says what to do with that span (extend, trim, cut, hold). |
| `pin` | `clip_t`, `cur_t`, `x`, `y` (0–1 fractions of the frame), `shot` | A spot on a frame. Fetch the frame grab at `$NOTECUT_URL/pins/<id>/<cNNNN>.jpg` to see exactly what the reviewer saw. |

Rules of interpretation:

- The **words decide WHAT is kept; the audio decides WHERE the cut lands.** A transcript selection is an intent on a span of words; snap the actual cut to a word boundary or a silence, never mid-word.
- A `transcript` comment whose body says "extend" refers to the selected span's neighbours: extend the kept region *to include* the selection.
- When a comment is ambiguous, do the conservative reading, and say what you chose in the `complete` note. Do not ask in the comment stream; Note Cut has no reply threads — ask through whatever channel the owner uses, and leave the comment `sent`.
- `resolved: true` on a row means the reviewer struck it through themselves. Treat as withdrawn.

---

## 4. Doing the work

1. **Never touch the source.** Work from `state.source.file` (or `notecut.json` → `source`) read-only. All renders are derived.
2. **Edit the EDL, not the video.** `assets/<id>/edl.json` is `{"keep":[{"start":s,"end":e}, …]}` in source seconds. Change it, render from it, keep the previous EDL as `prior_edl.json` so the server can remap older comments onto the new render.
3. **Render, then register.** Produce the new render (any tool), then either:
   - replace the file at the path in `notecut.json` → `media` (fast; keep the same id so comments follow), or
   - `notecut add <id>-v3 --media new.mp4 --edl edl.json --prior-edl prior_edl.json --transcript words.json` for a new id (use when the owner wants both versions side by side).
   Run `notecut prep <id>` if you replaced the media in place, so the proxy, sprite and peaks match it.
4. **Verify the render before reporting it.** Open `$NOTECUT_URL/api/<id>` and check `clip_dur` matches what you rendered. Play the moment of each comment you addressed (`cur_t`) at least mentally against the EDL. A file that exists is not a file that plays; a duration that matches is the minimum evidence.
5. **Do not batch silently.** If you are working through several comments, `complete` each one as it lands, not all at the end — a crash mid-batch must leave an accurate ledger.

---

## 5. Writing back (the part that makes the next session cheap)

### 5.1 Complete a comment

```bash
notecut complete <id> c0007 "extended to src 0:23.52; cut now lands on the pause after 'here'; rendered v3"
```

The note is what the reviewer sees under the green row. Make it specific: what changed, in which render, and any deviation from what was asked.

### 5.2 Update the state

`state` is free-form JSON. Keep it to what a stranger needs to continue. Use `merge` for partial updates (deep-merges dicts; **replaces lists**), `set` only to rewrite the whole document.

```bash
notecut state merge <id> '{
  "summary": "Episode 12 first cut. v3 current: c0007 + c0005 applied.",
  "resume":  ["Open comments: notecut comments <id> --open",
              "EDL: assets/<id>/edl.json (source seconds). Render: tools/render.sh",
              "Music duck is done in tools/audio_post.py, not the EDL"],
  "rules":   ["Never re-cut inside the sponsor read (src 4:10-5:02)"],
  "renders": {"current": {"file": "assets/<id>/preview.mp4", "version": "v3", "duration_s": 611.8},
              "lineage": ["v1 first cut", "v2 c0001", "v3 c0007 c0005"]},
  "timeline": {"edl": "assets/<id>/edl.json", "prior_edl": "assets/<id>/prior_edl.json", "keeps": 41},
  "tools":   {"render": "tools/render.sh <id>", "cutter": "build_edl.py --words words.snapped.json"},
  "open_items": ["OWNER: c0009 saved draft about the intro - not acted on"]
}' --note "v3 after c0007 c0005"
```

Keys the handoff renders as sections, in this order: `summary`, `resume`, `open_items`, `rules`, then `source`, `transcript`, `timeline`, `inserts`, `effects`, `audio`, `renders`, `preview`, `assets`, `tools`, `log`. Anything else is kept but not rendered. The home page shows `renders.current.version`.

### 5.3 Append to the log

```bash
notecut log <id> "v3 rendered; c0007 c0005 done; c0002 blocked: music stem missing"
```

One line per session minimum. The log is the only place a future agent sees *why* something was done.

### 5.4 Before you stop — checklist

- [ ] Every comment you touched has a `complete` record with a real note.
- [ ] `renders.current` points at the file that is actually being served, with the right `version`.
- [ ] `resume` says how to rebuild what you built (commands, not prose).
- [ ] Anything you could not finish is in `open_items` with the reason.
- [ ] `notecut handoff <id>` reads correctly from a cold start — run it and read it as a stranger.
- [ ] `saved` drafts were surfaced to the owner, not acted on.

If you are interrupted before this checklist, the next line of the log should be the first thing you write when you come back.

---

## 6. Watching for new work

```bash
notecut watch                     # polls /api/feed every 2 s, prints new sent comments as they arrive
notecut watch --since 42 --once   # one poll from feed line 42 (persist the line count between runs)
```

`/api/feed?since=N` returns `{"total": T, "since": N, "items": [...]}`; store `T` and pass it back next time. Items are `{at, id, video, summary, rec}` — `summary` is a one-line human description with the review-page link at that moment, `rec` is the raw comment record (`kind`, `body`, `clip_t` / `src_start`,`src_end` / `x`,`y`,`shot`).

A comment appears in the feed only when it is **sent** (Send to agent / Send all). Saved comments never appear there — that is the mechanism behind rule 0.2.

---

## 7. Things that go wrong, and the rule that prevents each

| Failure | Rule |
|---|---|
| Acted on a `saved` comment; owner was still thinking. | Filter on `status == "sent"`. Report saved rows, never act. |
| Re-did a `done` comment after a session restart. | Resume from `handoff`; `done` rows are finished. |
| Comment pointed at the wrong moment after a re-render. | Use `cur_t` (server-remapped) or `src_*`, never a stale `clip_t`. Always register the previous EDL as `prior_edl`. |
| Cut landed mid-word. | Words choose what is kept, audio chooses where the cut lands; snap to a boundary. |
| "Rendered v3" but the page still plays v2. | Check `/api/<id>` → `clip_dur` and `renders.current.file` after `prep`; the file the server serves is the truth. |
| Next agent had to re-read the whole chat. | State not written to Note Cut does not exist. §5.4 before stopping. |
| Wrote `merge` with a shorter `log` list and lost history. | `merge` replaces lists; use `notecut log` for the log. |
| Edited `comments.jsonl` by hand to "fix" a status. | Never. Post `complete`/`resolve`/`delete` through the API; the ledger is append-only. |
| Posted a link the owner could not open on a phone. | Give the `/v/<id>` URL as a Markdown link, and if the owner reviews on a phone, confirm the server is reachable from it (tailnet/tunnel). |

---

## 8. Minimal end-to-end example (copy and adapt)

```bash
export NOTECUT_URL=http://127.0.0.1:8808 NOTECUT_BY=editor-agent
ID=first-cut

notecut handoff $ID
notecut comments $ID --open --json > /tmp/open.json         # [] means nothing to do; stop.

# for each row in /tmp/open.json:
#   locate: transcript -> src_start/src_end ; point/pin -> cur_t -> source time via edl.json
#   edit assets/$ID/edl.json (copy the old one to prior_edl.json first)
#   render -> assets/$ID/preview.mp4 ; notecut prep $ID
#   notecut complete $ID <cNNNN> "<what changed, which render>"

notecut state merge $ID '{"renders":{"current":{"file":"assets/first-cut/preview.mp4","version":"v3"}}}' --note "v3"
notecut log $ID "v3: c0007 c0005 applied; c0002 open (needs music stem)"
notecut handoff $ID                                           # read it back as a stranger
```
