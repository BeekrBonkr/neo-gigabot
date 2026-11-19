# Garden art

Art for the garden game is kept in separate folders by where it came from, so
placeholder art can be found and replaced, and so files we are not allowed to
republish never reach the public repository.

| Folder | What goes in it | In git? |
| --- | --- | --- |
| `generated/` | Placeholder art produced by `scripts/garden_placeholders.py`. Stands in for anything the packs do not cover until proper art exists. | Yes |
| `packs/<pack-name>/` | Art made by other artists and bought or downloaded as a pack. One folder per pack, files unmodified. | No (ignored) |

Rules:

- Never mix the two. A sprite drawn by a person does not go in `generated/`,
  and nothing produced by code goes in `packs/`.
- `sprites.json` names every sprite the game uses and says which folder it
  comes from. Swapping a placeholder for real art is a change to that file.
- `packs/` is ignored by git because the packs' licenses forbid republishing
  the files. Each checkout (beta and live) needs its own copy. The originals
  are in `~/garden_assets/` on the host.
- To change a placeholder, edit the script and run it again; do not edit the
  PNG files by hand.
- Art made for this project that we do own and may publish would get a third
  folder, `original/`, when there is some.

## Packs in use

| Folder | Pack | Artist | Terms as stated on the store page |
| --- | --- | --- | --- |
| `packs/pixelmoth-flowers/` | Pixel Growing Flowers Farm Pack 16x32, 18 plants | PixelMoth (pixelmoth.itch.io) | Commercial use allowed, credit not required, no reselling or redistributing the pack as-is |
| `packs/pixelmoth-crops/` | Pixel Farm Crop Pack 16x16, 32 plants (not used yet) | PixelMoth (pixelmoth.itch.io) | Same as above |
