# Action Items

Mark any item you want done with a ⭐ at the start of its line, then hand this back.
Full details for each item are in `CODE_SWEEP.md` (numbers match).

---

## High-impact bugs

- [x] ⭐**A1. Remove the `raise error` local error handlers** in `fun.py`, `music.py`, `images.py` so the
  
      global handler runs again (restores error messages + command logging for ~73 commands). Move the
      `TransformerError` branch into the global handler in `main.py`.
- [x] ⭐**A2. Fix image command ordering** — defer *first*, then fetch/process, and run PIL work through
  
      `_run_cpu_bound` for the ~23 image commands that currently block the event loop and risk the
      3-second interaction timeout. Also fix the same defer-late pattern in `reactionroles add`/`batch`
      and `/image text quote`.
- [x] ⭐**A3. Kill or wire up the dead "prefix" setting** — dashboard edits a prefix nothing reads.
  
      (Default plan: remove it from the dashboard, schema, and Config.)
- [x] ⭐**A4. Add moderator-vs-target hierarchy checks** to `/kick`, `/ban`, `/mute` (invoker's top role,
  
      not just the bot's). Also block `/warn` on self and bots.

## Bugs / correctness

- [x] ⭐**B5. Fix `_mass_apply` progress spam** when actions fail (track loop index, not success count).
- [x] ⭐**B6. Handle expired stream URLs** for long-queued music tracks (retry once with a refreshed URL
  
      on playback failure).
- [x] ⭐**B7. Guard `/lock`, `/unlock` against non-TextChannels** (threads, forums).
- [x] ⭐**B8. Fix `/meme` & `/cursed` breaking confusingly** when `/reddit` is blocked.
- [x] ⭐**B9. Sanitize welcome/leave messages** with `allowed_mentions` so templates can't ping
  
      @everyone/roles on every join.
- [x] ⭐**B10. Normalize blocked-command names to lowercase** on insert (dashboard toggle can currently
  
      create case-duplicates it can't remove).

## Performance

- [x] ⭐**P11. Cache guild settings in memory** (invalidate on write) and cache the parsed `default.yml`
  
      at startup — currently re-read from disk on every message event bot-wide. Also collapse fun.py's
      triple settings read into one.
- [x] ⭐**P12. Preload reaction-role message IDs into memory** so every reaction bot-wide doesn't hit
  
      SQLite.
- [x] ⭐**P13. Share one `aiohttp.ClientSession`** on the bot instead of one per request.
- [ ] **P14. Stop syncing the command tree on every startup** — sync via an owner command or on tree
  
      change only.

## Code quality

- [x] ⭐**Q15. Delete dead code**: `info.py:_feedback_cooldowns`, `warnings.count_warnings` (or use it in
  
      `/warn`), duplicate `ImageSourceStore.clear`/`forget`.
- [x] ⭐**Q16. Cap the unbounded dicts** (mention cooldowns, heavy-command tracker keys, per-day log
  
      locks, snipe cache).
- [x] ⭐**Q17. Replace `print()` with the logger** in fun.py's Reddit fetch.
- [x] ⭐**Q18. Replace abandoned `apraw`** — either switch to `asyncpraw` or drop credentials entirely and
  
      use Reddit's public JSON endpoints through the existing `_fetch_json`.
- [x] ⭐**Q19. Unify the duplicated command-allowed check** (fun.py vs ImageToolkit) into one shared
  
      helper.
- [x] ⭐**Q20. Add pytest coverage for `utils/`** (settings sync, playlists, durations, link parsing,
  
      migration mapping).
- [ ] **Q21. Write a README** (setup, env vars, ffmpeg, model files, systemd).

## New features

- [x] ⭐**F22. Warning management**: `/warnings remove <id>`, `/clearwarnings`, and optional
  
      "only count warnings from the last N days" for auto-escalation.
- [x] ⭐**F23. `/userinfo <user>`** — join date, account age, roles, warning count for mods.
- [x] ⭐**F24. Music loop mode + `/music move <from> <to>`** queue reordering.
- [x] ⭐**F25. `/tempban <duration>`** with a scheduled-unban background task.
- [x] ⭐**F26. Multi-option polls** (up to ~5 choices) and/or persistent poll views that survive restarts.
- [x] ⭐**F27. Reaction-role self-healing** — purge mappings when their role or message is deleted.
- [x] ⭐**F28. Starboard.**
- [ ] **F29. `/remindme <duration> <text>`.**
- [ ] **F30. `/slowmode <seconds>`.**
- [x] ⭐**F31. Log rotation** for `storage/logs/` (delete command logs older than N days at startup).
