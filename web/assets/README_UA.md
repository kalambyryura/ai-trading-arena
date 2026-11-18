# Assets колізею

2026-10-01 · D052/E047. Усі зображення створені native ImageGen за обраним макетом (дизайн-макет, не входить у публічний репозиторій). Оригінали скопійовано без зміни пікселів; розміри, байти, назви оригіналів і SHA256 у `manifest.json`. Растрові арена, персонажі та портрети не замінені CSS/SVG малюнками.

| Файл | Використання | Prompt |
|---|---|---|
| `coliseum-floor.png` | Спільне овальне поле і фон мінікарти | `coliseum-prompts.json` |
| `human-poses.png`, `robot-poses.png` | Прозорі atlas 3×3, 1254×1254 | `coliseum-prompts.json` |
| `cash.png` | Нерухомий Cash | `cash-prompt.txt` |
| `human-portrait.png`, `robot-portrait.png` | Великий портрет деталей | `portrait-prompts.json` |

Atlas читається зліва направо, зверху вниз: idle, перевірки з планшетом, обмірковування, крок, guard, punch, block/hit, відпочинок, KO. Дев’ять клітинок по 418×418; CSS `background-position` обирає позу. Код пересуває готовий raster і підписи, не малює персонажа. Портрети деталей — окремі native PNG без перемикання поз.

Базові образи людини й робота повторно використовуються для фактичного складу. Авторство береться з метаданих, а сталі ID/колір підпису відрізняють стратегії. Індивідуальні портрети кожного майбутнього автора ще не створені. Жодних справжніх авторських фотографій або рахункових даних у PNG немає.

## Локальні шрифти й іконки

- Phosphor Icons **2.1.2 regular**: `Phosphor.woff2`, `icons.css` із 27 потрібними офіційними glyph definitions. Джерело — [офіційний npm пакет](https://www.npmjs.com/package/@phosphor-icons/web) через jsDelivr; [репозиторій](https://github.com/phosphor-icons/web). MIT: `PHOSPHOR_LICENSE.txt`.
- **Cormorant Garamond**: локальний variable TTF з [Google Fonts](https://github.com/google/fonts/tree/main/ofl/cormorantgaramond); `cormorantgaramond_OFL.txt`.
- **Manrope**: локальний variable TTF з [Google Fonts](https://github.com/google/fonts/tree/main/ofl/manrope); `manrope_OFL.txt`.

Браузер отримує тільки явно дозволені PNG/CSS/TTF/WOFF2 із `/assets/`; runtime не завантажує шрифти з CDN. Prompts, manifest і ліцензійні тексти збережені в Git для походження, але static route не віддає їх. `font-src 'self'` додає підтримку локальних шрифтів без дозволу зовнішніх origins.
