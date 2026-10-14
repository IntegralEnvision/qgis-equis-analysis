# Selection UI guidance for EQuIS Chemical Explorer

Distilled from a domain survey of comparable tools (not published)
and from what the EQuIS REST API actually permits, as recorded in
[data_selection.py](../equis_loader/data_selection.py). Written 2026-09-05.
Guidance, not a plan: nothing here is scheduled.

The one-sentence version: **the API forces a two-stage query, so the UI
should show the seam instead of hiding it — and the Apple rule-editor idiom
happens to be a good way to show it.**

---

## 1. Five facts about the API that decide the shape

These are verified in code comments, not assumptions. Any design that
contradicts one of them is wrong before it starts.

**Facility is not a filter.** It is the pre-filter that decides what there
is to filter: `list_reports(facility_id)` narrows which reports exist, and
every report parameter declares it as its only dependency. Step 2 of the
accordion already treats it this way. Nothing below applies to it; the
highest level of *Where* that is a filter in the API's sense is the
**location group**, then individual locations (`@sys_loc_codes`).

| # | Fact | Where verified | Consequence for the UI |
|---|---|---|---|
| 1 | A filter only takes effect if it is **saved on the report**. Passed as a query parameter it is silently ignored and the report runs unfiltered — on a big facility that is a 230 s timeout, not a wrong answer. | `data_selection.py` module docstring | Fetch-time filters are a *commit*, not a tweak. They are spent when the fetch runs. |
| 2 | **Nothing cascades before a fetch.** Every filter parameter declares `dependencies: ['@facility_id']` and nothing else; a report with analyte and matrix saved returns byte-identical picker lists to an unfiltered one. | `data_selection.py`, FRACTIONS comment, verified 2026-09-02 | Pickers cannot narrow one another. The in-house predecessor's matrix-rooted cascade does not transfer. Counts and "none in this data" annotations are only honest **after** rows are in hand. |
| 3 | Fetch cost is dominated by **chemical count × years**: one analyte over nine years took 29 s; three years of everything took 174 s. | `selection_panel.py` analyte list comment | The chemical picker is the strongest lever and earns the most room. No count is knowable pre-fetch. |
| 4 | Two sources with different semantics: **live** (parameters shape the request) and **agent** (stored output is frozen; the same values filter client-side). | `DataSelection` docstring; `SOURCE_HINTS` | The same row means different things depending on scope. The UI must say which, near the row. |
| 6 | The parameter service is scoped by the report's **saved** `@facility_id`; the `facilityId` query argument is ignored (verified 2026-09-05: one facility's report returned its 9,914 locations for every facility id passed, the unsaved base report returned none for any). | probe against `/api/reports/{id}/parameters/{name}` | Pickers only work through the per-user report the plugin already creates. Same rule as fact 1, one level down. |
| 5 | After a fetch **everything is in memory** (`ReportTable.rows`); chemical changes, aggregation, clicks are instant. Methods, fractions and per-CAS counts are learned only here. | `report_data.py` docstring; `set_methods`/`set_fractions` | Post-fetch filters are free and may show live counts. Pre-fetch filters are expensive and may not. |

The panel already respects all five: `prefetch_section` vs `filter_section`,
fraction/method rows hidden until a fetch, counts appended to chemical names
only afterwards, and `PanelState` refusing to offer a map before `LOADED`.
The guidance below is about making that structure *legible* rather than
changing it.

---

## 2. Eight rules

### R1. Show the seam. Two groups, two headers, one fetch button between them.

Every environmental tool surveyed either owns its database (the in-house predecessor, Hydro
GeoAnalyst) and cascades freely, or is server-backed (EQuIS Enterprise, WQP)
and does not. This plugin is the second kind and the panel's own comment
says why the split matters: *"mixing them in one box is what made 'Fetch'
and 'Add layer' look like two buttons for the same thing."* Give the groups
names that state the contract:

- **Sent to EQuIS** — decides which rows come back. Changing a row here
  invalidates the rows in hand.
- **Applied here** — re-scopes rows already fetched. Instant.

A row never migrates between groups; the field it filters on determines
which group it belongs to (see §3).

### R2. Order is Where → What → When → How, and Where starts at location group.

All seven peers order selection the same way: location → matrix, analyte,
fraction, method → date or most-recent → how results are treated. EQuIS
Analytical Results II opens its parameter tree the same way: *Location →
Individual(s), Group(s)*. Today the plugin has no Where row at all:
`@sys_loc_codes` values are read by the prepare task and `DataSelection`
carries a `locations` list, but no control populates it. The Where row
belongs at the top of step 4, above chemicals, as a location-group picker
with individual locations (typed or picked on the map) beneath it.

Verified 2026-09-05: the ARII base report (508 on this tenant) carries `@location_groups` (MultiSelect,
caption `Location\Group(s):`, depends on `@facility_id`), and groups are
expanded server-side so the plugin need not know their members. But of the
16 facilities on this tenant with an ARII-based report, only **3 define any
groups** (with 2, 6 and 23); 13 have none. So by R3 the
group picker appears only when the parameter service returns groups for the
facility — which, unlike fraction and method, is knowable *before* a fetch.
Individual locations run from 6 to 11,280 per facility, so the map pick and
a search box, not a bare list, are the individual-location control.

### R3. Never offer a filter you cannot honor honestly.

The panel's current discipline — hide fraction and method until a fetch
says what exists; disable a matrix combo with one choice but keep it
visible naming that matrix — is the right rule and should generalize:

- Pre-fetch rows show *what exists*, never *how much* (no counts).
- Post-fetch rows show counts per option and annotate absent options
  (`SW8270  (none in this data)`) rather than removing them, because the
  next request may widen to them.
- A control that would silently return zero rows (dissolved on sediment)
  is not offered before the data can say whether it applies.

### R4. Counts after, nothing before. Never a dead-end.

Faceted-search practice says show counts per option and never strand the
user on an empty result. Here that splits by stage:

- **Applied here** footer: `142 locations · 3,210 results match`, updated
  on every change. This is what the Apple footer shows and it is free.
- **Sent to EQuIS** footer: no count and no cost estimate. The count is
  unknowable and an estimate would be two data points dressed up. Say
  plainly "count known after fetch" and nothing else. (Decided
  2026-09-05.)
- Never display `0 match` before rows are in hand; an empty list is
  indistinguishable from a facility with no chemistry, which the panel
  already guards against with `set_unavailable`.

### R5. Result handling is one strip with three named choices, and nothing the aggregate does not honor.

The in-house predecessor's Result Handling tab (non-detects · duplicates · multiple results ·
exclusions) is the vocabulary users already speak, and EQuIS Analytical
Results II uses the same categories. Here there are **three** real choices,
not four: field duplicates are already the FD sample type under QC, and two
methods on one sample are two analyses, not duplicates — the panel's own
tooltip says so. **Aggregation stays client-side** (decided 2026-09-05):
rows in hand are what make switching most-recent ↔ every-result instant,
and ARII's *Most Recent Sampling* checkbox would give that up for a smaller
fetch. The same reasoning keeps the non-detect convention client-owned
rather than using `@nd_multiplier`.

| Choice | Options that are real today | Server equivalent, deliberately not used |
|---|---|---|
| Non-detects | draw hollow · DL × k · exclude | `@nd_multiplier` |
| QC samples | Normal only (default) · include EB/FD/TB/FB | `@sample_types` *is* used — a fetch filter, so this control lives in **step 4**, not the step-5 strip |
| Multiple results per location | most recent · maximum · every result | *Most Recent Sampling* |

The in-house predecessor's lesson to take literally: its duplicate-handling radios were never
wired to the SQL. A choice that appears in the strip must be visible in the
layer. Do not add a fourth until it is.

### R6. No threshold filter. Exceedance, if ever, is symbology on the full range.

Considered and rejected 2026-09-05. Peers do symbolize against a level
(ESdat, Traffic Light Map, the in-house predecessor's screening-level bins), but a *filter*
`Result > 100 µg/kg` produces layers that omit part of the data, and a
layer like that left in a project is a liability: whoever opens it later
does not see the full range and may not know it is missing. The utility is
limited; the failure mode is silent. If exceedance display is ever wanted
it is a renderer over every row — above/below a level, non-detects still
hollow — never a row filter, and it is not on the list now.

### R7. The query sentence is the layer's provenance, written once, at Add layer.

The in-house predecessor writes its filter summary into the layer's metadata abstract, so a
layer left in a project explains itself months later. Do the same, with one
rule about timing: the sentence is composed **at the moment the layer is
added**, from the fetch that produced the rows, the step-5 filters, the
aggregation and the symbology in force right then — and it is never
rewritten afterward. Later changes in the panel describe the *next* layer,
not this one. Put it in the layer abstract, derive the layer name from it,
and show it in the banner. `DataSelection.describe()` is most of the text.

### R8. Saved selections — not now, and not the cache.

Every peer has named, reusable selections. Whether this plugin needs them is
undecided; the distinction from what exists matters before deciding:

- **The cache** (`cache.py`) stores *rows*: `<report_id>__<param_hash>.csv.gz`
  per facility, so a three-minute report is not re-run. It answers "don't
  make me wait again." It is keyed by the parameters that produced it, but
  it is not browsable as a list of things you asked for.
- **The per-user report** on the server already persists the step-4 half
  (chemicals, matrix, dates, sample types) between sessions — it is a saved
  query in the API's own terms, and Enterprise shows it.
- **A saved selection** would be a small named recipe — the step-5 half plus
  aggregation, non-detect convention and symbology — that can be re-applied
  to a fresh fetch or another facility. It answers "don't make me re-pick
  fourteen chemicals and the same three filters again." the in-house predecessor's `.flt`
  files are exactly this.

Decided 2026-09-05: **not now.** Revisit only if someone asks. If it ever
comes, it is per facility, in the QGIS project (it travels with the map it
describes), and references the report by id rather than copying it.

---

## 3. The rule-editor idiom, inside the six steps that exist

The reference is Apple's search field + scope bar + rule editor (the Finder
Smart Folder / Mail rules "All of the following are true" pattern; HIG:
*Search fields*, *Token fields*). It is worth borrowing for one reason: a
rule row states *field · operator · value* in one line, which is exactly
what a stage-typed filter needs to be legible. It is **not** a reason to add
a surface. The accordion's six boxes already are the scope bar and the two
rule groups:

| Apple idiom | Already here | Nothing to add |
|---|---|---|
| Tokens `Facility:` `Report:` | steps 2 and 3 | a token field would duplicate two combos |
| Scope bar Live / Agent / Cached | source choice in step 3 + offline flag in `PanelState` | — |
| Upper rule group | **4. Pre-fetch Data** = *Sent to EQuIS* | header text and "count known after fetch" |
| Lower rule group | **5. Filter / Aggregate** = *Applied here* | header text and a live count line |
| Footer count | bottom of step 5 | — |
| Save / saved list | not present | small; R8 |

What steps 4 and 5 would read like with rows aligned as field · operator ·
value, at the dock's current width:

```
▾ 4. Pre-fetch Data — sent to EQuIS; decides which rows come back
   Location group   is any of   Transect A, Transect B ▾
   Chemical         is any of   Benzene, Toluene, +2 ▾
   Matrix           is any of   SE ▾
   Sample date      between     2019-01-01 – 2025-12-31
   Sample type      is          Normal · excludes EB, FD, TB, FB
                                count known after fetch
                                             [ Fetch 4 chemicals ]
▾ 5. Filter / Aggregate — applied here; 15,858 rows in hand
   Fraction         is any of   Dissolved (7,912), Total (7,946)  ⚠ mixed
   Method           is any of   SW8260 (12,004) · SW8270 none in this data
   Per location     Most recent ▾    Non-detects  Hollow ▾    QC  Excluded ▾
                                142 locations · 3,210 results
                                             [ Add layer to map ]
```

What keeps this honest rather than decorative:

- **A field belongs to one step.** Location group, chemical, matrix, date,
  sample type are ARII parameters and live in step 4. Fraction, depth,
  detect flag are columns and live in step 5. Method is a parameter whose
  options are only known after a fetch, so it appears in step 5 annotated
  and may be promoted to step 4 to narrow the *next* fetch — the panel's
  `set_methods(..., present=)` already holds both facts.
- **Operators are per field, and few.** Server fields are multi-select
  lists or a date pair, so `is any of` and `between` — never `contains`,
  because no such parameter exists. Depth in step 5 is a range. About
  eight fields, fixed; not a predicate engine.
- **"All of the following" is fixed for step 4** (parameters are ANDed).
  *Any* only in step 5, and only if a use appears; none did in the survey.
- **A change in step 4 after `LOADED` marks step 5 stale.** A message line
  at the top of step 5 — "These rows are from your last fetch. Fetch again
  to apply the changes above." — joins the existing disabled *Map* button.
  Step 5 stays usable on the old rows; nothing is dimmed.
- **Rows are not added or removed.** `−`/`+` in the reference is for open
  predicates; here the rows are the fixed set of fields, hidden when the
  data cannot honor them (R3), as fraction and method already are.

---

## 4. What not to build

- **A seventh box, a token field, or a separate query window.** Steps 2
  and 3 are the tokens; steps 4 and 5 are the rule groups. Visual scope
  stays where it is.
- **Threshold-based row filters** (R6): partial-range layers are a
  liability in a project.
- **A general predicate editor.** The Apple look is a *presentation* of a
  bounded set of typed rows. Arbitrary nesting, OR groups across stages,
  or a free-text expression would promise the server things fact 1 and
  fact 2 forbid.
- **A cascade before fetch.** Cannot be honored (fact 2); would only be
  fakeable by fetching first, which is the expensive thing being avoided.
- **"All" as a list item.** the in-house predecessor needed warning dialogs to police it.
  Empty selection means all, and the row says so (`All matrices`).
- **Result-handling options ahead of the aggregate that honors them**
  (R5).
- **Modal dialogs.** The dock panel with a state machine is the better
  base; every peer that has moved recently (Enterprise, HGA) went to
  dockable panels and widgets.

---

## 5. Order of adoption, smallest first

*Implemented 2026-09-06 as 3.27.0 (uncommitted at time of writing); see
§6 for the two placement changes found during implementation.*

Each step stands alone and is worth having even if the next never happens.
Every one lands inside the existing six boxes. They ship together as
one release (3.27); none goes out as a patch on its own. Each step names
what it reads from, or adds to, `PanelState` — see §7.

1. **Headers and footer** on steps 4 and 5 (R1, R4): name the contract in
   each box title, add the post-fetch count line to step 5 and "count
   known after fetch" to step 4. No new controls.
   *State:* the step-5 count shows only in `LOADED`; step 4's line shows in
   `PREPARED`, is replaced by progress in `FETCHING`. Text follows
   `fsm.state`; no new state.
2. **Provenance sentence** into layer abstract and name at Add layer (R7).
   `describe()` exists.
   *State:* allowed only when `LOADED` and not stale — the same gate as
   *Map* today. Composed from the selection that produced the rows
   (`_fetched_selection`), never from the live controls.
3. **Result-handling strip** — *Per location* and *Non-detects* in step
   5; the QC checkbox and the date range move **up to step 4**, because
   `@sample_types`, `@start_date` and `@end_date` are saved onto the report
   and sit in `_selection_key()`. Both had been in the step-5 form, where a
   change looked free but silently demanded another fetch (R1, R5).
   *State:* aggregation and non-detect convention are local and never
   enter the key; QC and dates do. The move makes the box match the key.
4. **Where row** in step 4 — location group (when the facility has any),
   then locations (R2).
   *State:* group values arrive with the prepare task (`FACILITY →
   PREPARED`), so `@location_groups` joins the `wanted` tuple in
   `tasks.py` and the row is shown or hidden at `PREPARED`. Both
   `@location_groups` and `@sys_loc_codes` are server parameters and
   enter `_selection_key()`; a change after `LOADED` sets stale.
5. **Stale message line** at the top of step 5, beside the existing
   disabled *Map* button.
   *State:* reads `fsm.stale` (§7), the one addition to the machine.
6. **Align rows as field · operator · value** in steps 4 and 5 (§3).
   Cosmetic once 1–5 exist; premature before them.
   *State:* none.

---

## 6. Decisions

Decided 2026-09-05:

- Facility is a pre-filter, not a filter; Where begins at location group.
- Visual scope does not grow: six boxes, no new surface.
- Aggregation and the non-detect convention are client-side.
- Three result-handling choices, not four.
- No threshold filter.
- No pre-fetch cost line.
- Layer provenance is written once, at Add layer, never updated.
- Release: all six adoption steps together in 3.27, not as patches.
- Stale data: add one message line at the top of step 5; keep the disabled
  *Map* button; dim nothing.
- Saved selections: not now (R8).
- Staleness moves onto `PanelState` as a flag beside `offline` and `busy`;
  not a new pipeline state (§7).
- Found in implementation: the **date range** was also in the step-5 form
  while `@start_date`/`@end_date` sit in the selection key. Moved to step 4
  with QC, for the same reason.
- Individual locations: a checkbox "Only locations selected on the map
  (N)" in step 4, reading the Locations layer's QGIS selection, rather
  than a list — facilities run to 11,000 locations.
- Analyte groups (`@analyte_groups`): not now; revisit after the six steps
  land.
- **Where row**: `@location_groups` exists and works through the per-user
  report; shown only for facilities that define groups (3 of 16 here).
  Individual locations via map pick + search. (Verified 2026-09-05; R2.)

Parked, with the reason: ARII carries `@analyte_groups`
(`Result\Analyte\Group(s):`, 26 groups on one facility), a pre-fetch lever on
the strongest cost driver — chemical count — by names users already know.
Worth having; deferred so the panel does not grow two ways at once.

Open: nothing. Every question raised in this document has an answer above.

---

---

## 7. The state machine is the spine

[panel_state.py](../equis_loader/panel_state.py) already answers "what may
the panel do right now" from one place: a pipeline `DISCONNECTED →
CONNECTED → FACILITY → PREPARED → FETCHING → LOADED`, two orthogonal
flags (`offline`, `busy`), and sub-modes of `LOADED` (`IDLE`, `PICKING`,
`ROLLING`). It holds state, refuses illegal moves, never touches a widget.
Three bugs came out of not having it; nothing in this document should
re-create the conditions for them.

**What each box shows, by state.** This is the whole of §3 restated as a
lookup, and it is what step 1 implements:

| State | Step 4 (Sent to EQuIS) | Step 5 (Applied here) | Map |
|---|---|---|---|
| `DISCONNECTED`, `CONNECTED` | disabled, empty | hidden rows | off |
| `FACILITY` | pickers filling ("⏳ …") | hidden rows | off |
| `PREPARED` | pickers live · "count known after fetch" · Where row shown iff groups exist | rows hidden (fraction, method) or disabled | off |
| `FETCHING` | locked · progress | as before | off |
| `LOADED` | live | rows shown with counts · footer `N locations · M results` | on, unless stale |
| `LOADED` + stale | live | rows usable on old rows · message line | off, tooltip names the fetch button |

`offline` changes none of these; it only decides where a fetch reads from.

**The one addition: `stale` as a flag, not a state.** Today staleness is
computed in the dialog (`_fetched_selection != _selection_key()`) and
surfaced only through `_can()["stale"]`. Steps 2, 4 and 5 all need it, and
they must agree. So it moves onto `PanelState` beside `offline` and `busy`:

- `stale` is meaningful only in `LOADED`; entering any other state clears
  it, the way leaving `LOADED` already resets `mode` to `IDLE`.
- The dialog sets it when the selection key moves; the machine does not
  know what a selection key is. It stays dumb.
- `describe()` reports it (`loaded, stale`), and
  [test_state.py](../tools/test_state.py) grows the cases: set in
  `LOADED` sticks; set elsewhere is refused and recorded in
  `violations`; a successful fetch clears it.

Deliberately **not** a seventh pipeline state: a stale panel can still map
nothing, still filter the old rows, still pick and roll on an existing
layer. It differs from `LOADED` only in what *Map* and *Add layer* may do,
which is exactly what a flag is for. Making it a state would double the
`LOADED` transitions and its sub-modes to say one boolean.

**The second addition: `interactive`, also a flag (2026-09-29).** A
toggle at the top of step 5 turns the chemical list single-select. Each pick
draws one scratch memory layer (the *preview*) and re-bins the chart. Every
later change to a step-5 or step-6 control removes the preview and adds a
new one. *Keep layer* turns the preview into an ordinary layer. It is not a
state, and not a mode beside `PICKING`/`ROLLING`, because of what it
implies:

```
interactive (armed in any state; survives a fetch)
    |-- ROLLING: ended on arming, refused while armed
    |     (a roll cycles a multi-selection and owns a renderer
    |      that the preview replaces on every pick)
    |-- PICKING: allowed (clicking the preview plots its series)
    `-- may_preview = LOADED and not busy and not stale
          |-- chemical picked  -> preview removed, new one added
          |-- dropdown changed -> preview removed, new one added
          |-- Keep layer       -> preview becomes an ordinary layer
          `-- may_preview lost -> preview removed, reason on the status line
```

| | Normal | Interactive |
|---|---|---|
| Chemical list | multi-select | single-select |
| Step 5 button | *Map chemical(s)* | *Keep layer* |
| Storage | the combo's choice | always memory until kept |
| Replaces | same view key | only the preview, never a kept layer |
| Unit/fraction refusal | modal | status line, and the preview comes down |
| Time-series tool | armed on first map | left alone (map selection tools stay usable for US-6) |
| Zoom | every build | first pick only |
| `stale` on the own-report path | key equality | the fetched chemicals **cover** the pick; every other parameter must still match |

The last row is the one that changes behaviour. Key equality would make
every pick stale: fetch five chemicals, switch to single-select, and `{A}`
no longer equals `{A…E}`. Rows fetched for five chemicals answer a question
about one of them. Fetching with no chemicals selected covers them all, and
is how interactive mode is meant to be fed on the live path. A parquet
source is never stale, so there it works straight away.

Measured on Facility A parquet (3.45 M rows, WS, fraction T, most recent, 219
locations), Intel Core Ultra 7 265U, QGIS 4.2.1. On 2026-09-29 one pick
took **510–690 ms**. On 2026-09-30, after `row_id` stopped being a window
over the whole scan (see `ReportStore._row_id_expression`), it takes
**55–90 ms**: `aggregate` 26–57 ms, `units_for` 13–23 ms. *Every result*
went from about 1.9 s to 0.88 s. Folding the unit question into the
aggregate would now save about 20 ms per pick, which isn't worth touching
two rules the parity gate guards.
[test_interactive.py](../tools/test_interactive.py) drives all of this
through the real panel.

**What does not change.** The transition table, the `LOADED` sub-modes,
the `FETCHING → back where it came from` rule on cancel or failure, and the
principle that violations are logged and ignored rather than raised. A
change in step 4 after `LOADED` does **not** move the panel to `PREPARED`:
the rows in hand are real data from a real request, and throwing a
three-minute fetch away because a control was brushed is the failure the
flag exists to avoid.

---

## Sources

- This repo: [data_selection.py](../equis_loader/data_selection.py),
  [selection_panel.py](../equis_loader/selection_panel.py),
  [panel_state.py](../equis_loader/panel_state.py),
  [report_data.py](../equis_loader/report_data.py), [README](../README.md).
- [EQuIS Analytical Results II parameters](https://help.earthsoft.com/lib_analytical-results---ii.htm)
- [Traffic Light Map widget](https://help.earthsoft.com/ent-traffic-light-map-widget.htm)
- Apple HIG: [Search fields](https://developer.apple.com/design/human-interface-guidelines/search-fields) · [Token fields](https://developer.apple.com/design/human-interface-guidelines/token-fields) · [Searching](https://developer.apple.com/design/human-interface-guidelines/searching)
- [Empathy: facets and filters](https://docs.empathy.co/onboard-empathy-platform/design-search-and-discovery-ui/facets-and-filters.html) · [UXmatters: faceted search](https://www.uxmatters.com/mt/archives/2009/09/best-practices-for-designing-faceted-search-filters.php)
