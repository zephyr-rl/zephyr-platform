# Design — Graphite

Chosen visual direction for the panel (Stage 1). Dark, neutral graphite with a single blue
accent. Serious and calm: color is used for state, never for decoration.

Canvas: "Zephyr Platform — Visual Directions" (https://claude.ai/artifact/3WEgxpEEBm3gzSxo1Lz8Py),
artboards "A · Graphite" and "Graphite · Components".

Source of truth for code: `frontend/src/styles/tokens.css`.

## Color

| Token | Value | Use |
|---|---|---|
| `--bg` | `#111315` | App background |
| `--bg-sidebar` | `#15181B` | Sidebar |
| `--surface` | `#181B1F` | Cards, tables, panels |
| `--surface-raised` | `#1E2226` | Toasts, dialogs, menus |
| `--surface-hover` | `#22262B` | Hover, active nav item |
| `--border` | `#262A30` | Card borders, dividers |
| `--border-strong` | `#2E333A` | Inputs, raised surfaces |
| `--text` | `#E6E8EB` | Primary text |
| `--text-muted` | `#9AA1AB` | Labels, secondary text (≥ 6:1 on surface) |
| `--text-on-accent` | `#0B0D0F` | Text on accent buttons |
| `--accent` | `#4C8DFF` | Primary actions, focus ring, "in progress" |
| `--success` | `#5FB98A` | Delivered, connected, saved |
| `--warning` | `#F0A35E` | Low stock, needs attention |
| `--danger` | `#F07A6A` | Failed, destructive actions |

State is never shown by color alone: every status also has a label and a glyph
(● delivered, ◐ in progress, ▲ attention, ✕ failed).

## Type

Inter (400/500/600), tabular numbers for figures and ids.

| Token | Size / weight | Use |
|---|---|---|
| `--text-xs` | 12 / 400 | Table headers, captions |
| `--text-sm` | 13 / 400 | Tables, secondary UI |
| `--text-md` | 14 / 400 | Body, controls |
| `--text-lg` | 15 / 600 | Section titles |
| `--text-xl` | 22 / 600 | Page titles |
| `--text-2xl` | 28 / 600 | KPI figures |

## Space, radius, elevation

- Spacing scale (px): 4, 8, 12, 16, 20, 24, 32, 48.
- Radius: `--radius-sm` 4 (badges), `--radius-md` 6 (controls), `--radius-lg` 8 (cards, toasts, dialogs).
- Elevation: only raised surfaces (toast, dialog, menu) get `--shadow-raised`.
- Touch targets ≥ 44 px for primary buttons; controls ≥ 40 px.

## Feedback rules (visual confirmations)

| Situation | Pattern |
|---|---|
| Action finished (saved, delivered, imported) | Toast, bottom right, success glyph, 5 s, with a link to the result |
| Action failed | Toast with danger glyph, stays until dismissed, shows the reason |
| Irreversible action (deliver, cancel, void stock, refund) | Confirmation dialog naming the exact object; confirm button repeats the verb |
| Long-running job (import, restock run) | Inline progress in the panel + toast when done |
| Live state (connection, worker) | Status dot + label in the header |
| Form validation | Inline message under the field, field border in danger |

## Layout

- Sidebar 232 px with nav: Overview, Orders, Stock, Listings, Messages, Reports, Automation,
  Notifications, Settings (bottom).
- Header: store selector (ZephyrStore / Kawai / Jewelry / All stores) + connection status.
- Content: max width fluid, 24–32 px padding, cards on a 12–16 px gap grid.
- Under 860 px the sidebar collapses, grids stack, wide tables scroll inside their card.
