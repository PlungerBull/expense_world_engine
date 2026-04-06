# Expense Tracker — Design Philosophy

> Extracted from the Warm Productivity Vision & Philosophy document.
> Multi-app ecosystem sections removed. Expense Tracker focus only.

---

## Core Philosophy

### Simple by Default, Powerful When You Need It

Every feature starts simple. The surface is clean and approachable — anyone can pick it up and start using it without a tutorial. But beneath that simplicity, power-user features exist for those who want them. Complexity is never forced; it's revealed when you ask for it.

### Wholesome Minimalism

This isn't cold, sterile minimalism. It's warm. A UI that feels like it was designed by someone who cares about your day, not just your data. Every interaction should reduce stress, never add it. If a feature creates anxiety or pressure, it doesn't belong here.

### Zen UX — Things 3 Level of Restraint

Very little on screen at once. Every pixel earns its place. Animations are subtle and purposeful, never decorative. The interface breathes — generous whitespace, clear hierarchy, no visual noise. Inspired by Things 3's philosophy: powerful software that feels effortless.

### Your Data, Your Control

You own everything. Your data is yours. Sharing is always a deliberate choice, never a default. When collaboration features exist, you share specific items with specific people — not workspaces.

---

## The Expense Tracker

Track what you spend, who owes whom, and where your money goes.

### Core Tracking

- **Inbox/ledger structure:** Log any expense fast — incomplete entries go to the inbox. When all mandatory fields are present and the date is today or past, the item shows a "ready" indicator and a Promote button. The user taps Promote to move it to the ledger. This lets users add optional fields (hashtags, description, receipt photo) before confirming. The ledger enforces completeness — nothing lives there without all required fields. Items already in the ledger can still be edited.
- **Flat categories** — no hierarchy. Every category is directly assignable. Two system categories exist: `@Debt` (for person accounts) and `@Other` (for inter-account transfers).
- **Hashtags** — multiple per expense, freeform, available on both inbox and ledger items.
- **Description** — optional free text directly on any transaction (inbox or ledger).
- **Multiple bank accounts** — each account has a single currency. A real-world multi-currency card is modeled as separate accounts, one per currency.
- **Multi-currency** — USD + PEN only (additional currencies deferred). Exchange rates stored in a single-base reference table (all rates relative to USD). Auto-filled on transaction creation, always user-overridable. Original transaction amounts in the account's currency are never mutated.
- **Receipt photo attachment**
- **CSV import and export**

### Dashboard & Analysis

- Simple table showing expenses by category per month, 3-month rolling view
- Category breakdown views with hashtag combination grouping (rows sum cleanly to category total)
- Filtering by individual hashtag across categories
- Search across expenses
- Budget tracking: monthly per-category budgets (income and expense). All-or-nothing — when activated, every category must have a budget. Static monthly template, no rollover. Set in `main_currency`, compared against actual spend across all accounts.

### Financial Integrity

- **Reconciliation:** Match expenses against bank statements via reconciliation batches. On completion, four fields lock: original amount, bank account, title, date. All other fields remain editable. Batches can be un-reconciled (reverted to draft), which unlocks all fields.

### People & Transfers (unified `/` syntax)

People are bank accounts with `is_person = true`. The `/` syntax creates a paired transaction on any account — unifying debt tracking and inter-account transfers into one mechanism.

- **Shared expense:** `-60 Lunch @Food $Chase /Eliana +30` → -60 on Chase (@Food), +30 on Eliana (@Debt). Eliana owes you 30.
- **Settlement:** `+30 Settlement $Chase /Eliana -30` → Eliana's balance returns to 0.
- **Someone else pays:** `-30 Lunch @Food $Eliana` → single transaction on Eliana's account. You owe her 30.
- **Inter-account transfer:** `-60 Exchange $Chase /Chase_Credit +60` → same mechanism, category @Other for both.

Cross-user sharing: link a person account to a real user. Shared expenses are visible to both parties sign-flipped. One transaction record, two readers, no duplication. Both confirm before lockable fields are sealed.

### Expense Planning

- View all upcoming planned expenses (recurring and one-off) sorted by due date
- Create planned expenses directly from the Expense Tracker
- Future-date routing: adding an expense with a future date creates a planned expense instead of a real one
- Recurring expense templates with flexible patterns (daily, weekly, specific days, monthly, yearly)

---

## Design System Direction

**Color philosophy:** Warm, not clinical. The palette should feel like good light on a clean surface — not a bank app, not a spreadsheet. Positive/income amounts in a muted green. Negative/expense amounts in standard text color (not red — the minus sign already communicates direction; red is reserved for errors and destructive states).

**Typography:** SF Pro. Clear hierarchy between title, body, and caption. No decorative fonts.

**Spacing:** Generous. The interface breathes. Consistent 4pt grid.

**Animations:** Subtle and purposeful. Never decorative. Transitions that feel instant but smooth — not showy.

**Iconography:** SF Symbols. Consistent weight and size. No custom icon sets unless absolutely necessary.

**Amount display format:** Always ISO currency code + space + number. `USD 30.50`, `-PEN 1,500.00`. Never locale symbols (`$`, `S/`) — they're ambiguous across currencies.
