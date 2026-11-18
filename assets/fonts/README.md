# Fonts

Used by `utils/richtext.py` to draw text on images. A command asks for one font
(`impact.ttf` for meme text, `arial.ttf` elsewhere); anything that font lacks is
drawn from the fallbacks, in this order, and emoji come from the emoji font.

| File | Covers | License | Source |
|---|---|---|---|
| `DejaVuSans-Bold.ttf` | Latin, Greek, Cyrillic, Arabic, Hebrew, Georgian, Armenian, symbols | DejaVu Fonts License (free) | dejavu-fonts.github.io |
| `NotoSansKR-Bold.otf` | Korean, Japanese, common Chinese characters | SIL Open Font License 1.1 | github.com/notofonts/noto-cjk |
| `DroidSansFallbackFull.ttf` | The rest of Chinese | Apache License 2.0 | Android Open Source Project |
| `NotoSansThai-Bold.ttf` | Thai | SIL Open Font License 1.1 | github.com/notofonts/notofonts.github.io |
| `NotoSansDevanagari-Bold.ttf` | Hindi and other Devanagari languages | SIL Open Font License 1.1 | github.com/notofonts/notofonts.github.io |
| `NotoColorEmoji.ttf` | Emoji, in color (v2.047) | SIL Open Font License 1.1 | github.com/googlefonts/noto-emoji |

To cover another script, add its font here and to `FALLBACK_FONTS` in `utils/richtext.py`.
