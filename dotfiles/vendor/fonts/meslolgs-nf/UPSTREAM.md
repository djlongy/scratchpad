# Vendored: MesloLGS Nerd Font

Third-party font files, checked in verbatim so the powerline prompt and the tmux status
line render on a client that has no internet access.

| | |
|---|---|
| Upstream | https://github.com/ryanoasis/nerd-fonts |
| Release | `v3.5.0` |
| Artifact | `Meslo.zip` (release asset — **not** vendored; only 4 files are taken out of it) |
| Artifact URL | https://github.com/ryanoasis/nerd-fonts/releases/download/v3.5.0/Meslo.zip |
| Artifact sha256 | `6ef538a04f30af9cbe4d95fbd1ae31205a04c48a2c09714f6145ac9cbb6d1b64` |
| Vendored on | 2026-08-17 |
| Licence | Apache-2.0 — see `LICENSE.txt` (kept as upstream ships it, beside the fonts) |

Apache-2.0 permits redistribution of the font files, which is what this directory does.
The copyright holder of the underlying Meslo LG typeface is André Berg; the Nerd Font
glyph patching is by the nerd-fonts project.

## What is here, and why only these four

`Meslo.zip` is ~109 MiB and carries six line-gap/zero variants × three width families
(default, `Mono`, `Propo`) × four styles. Only one family is needed:

| File | Style | sha256 |
|---|---|---|
| `MesloLGSNerdFont-Regular.ttf` | Regular | `a29e664b2129fa29bdc6923b093d7727129fe88873a586195112fef5cbe307c6` |
| `MesloLGSNerdFont-Bold.ttf` | Bold | `0df17d28c4ef94e709a6db6e2f406826523017700649c8cc6902999af9330214` |
| `MesloLGSNerdFont-Italic.ttf` | Italic | `8136d614ae94662d41a49b0244460d84dcb01f07976cb28fb40454d46449c5bc` |
| `MesloLGSNerdFont-BoldItalic.ttf` | Bold Italic | `f32b285bad75f23bfdadd2cd7e17d0c5f2ee0845e94c5526402c8db86831b624` |

`LGS` is the **small line gap** cut, the one recommended for terminals — the larger gaps
push the powerline separators apart and leave gaps between the background blocks.

### Zero style: this family has the SLASHED zero

The vendored family is `MesloLGS Nerd Font` — small line gap, **slashed** zero (`0`
struck through with a diagonal). The `DZ` families (`MesloLGSDZ…`) are the **dotted**
zero alternative, so `DZ` reads as "dotted zero", not "different zero as in slashed".
Vendoring a `DZ` file would give the dotted form, which is not what is wanted here.

Nothing in the release states this: the bundled `README.md` documents only the width
variants (`Nerd Font` / `Nerd Font Mono` / `Nerd Font Propo`) and never mentions `DZ` or
the zero. So the glyph itself was made the authority — `MesloLGSNerdFont-Regular.ttf` and
`MesloLGSDZNerdFont-Regular.ttf` were rendered through FreeType and compared directly:
the plain family drew a diagonal slash through the zero, the `DZ` family drew a centred
dot. Re-check the same way after any version bump rather than trusting the suffix:

```python
from PIL import Image, ImageDraw, ImageFont
img = Image.new("RGB", (400, 200), "white")
ImageDraw.Draw(img).text((20, 20), "0O", fill="black",
                         font=ImageFont.truetype("MesloLGSNerdFont-Regular.ttf", 120))
img.save("zero.png")   # then look at it
```

The plain (non-`Mono`, non-`Propo`) width is deliberate: it is the double-width-icon cut
whose family name is exactly `MesloLGS Nerd Font`, which is the name every consumer here
configures. `Mono` and `Propo` register under different family names and would not match.

All four files report family `MesloLGS Nerd Font` with styles Regular / Bold / Italic /
Bold Italic, so a terminal that asks for one family gets real bold and italic faces
instead of synthesised ones.

Total vendored: ~11 MiB.

## Consumers

| Consumer | What it does |
|---|---|
| `dotfiles/windows/install-nerd-font.ps1` | per-user Windows install, offline, no admin |
| `dotfiles/install.d/30-fonts.sh` | Linux/macOS install when fontconfig is present |
| `bash/ohmybash/install-nerd-font.sh` | installs from here first, downloads only as a fallback |

## Refreshing this vendor tree

Run on a networked machine, from a temporary directory outside this repository:

```bash
curl -fLO https://github.com/ryanoasis/nerd-fonts/releases/download/<tag>/Meslo.zip
sha256sum Meslo.zip                     # record it in the table above
unzip -j Meslo.zip \
  'MesloLGSNerdFont-Regular.ttf' 'MesloLGSNerdFont-Bold.ttf' \
  'MesloLGSNerdFont-Italic.ttf'  'MesloLGSNerdFont-BoldItalic.ttf' \
  'LICENSE.txt' -d dotfiles/vendor/fonts/meslolgs-nf
sha256sum dotfiles/vendor/fonts/meslolgs-nf/*.ttf   # record each in the table above
```

Never check in the zip itself — it is two orders of magnitude larger than the four files
that are actually used, and the repository's `.gitignore` excludes `*.zip` for that reason.

Confirm the family name survived a version bump before trusting the installers, because
they register the font under a literal name:

```bash
fc-scan --format '%{family}|%{style}\n' dotfiles/vendor/fonts/meslolgs-nf/*.ttf
```
