# Arbiter

A Home Assistant integration that decides what a switch should be doing when several
automations disagree.

## The problem

Home Assistant has no arbitration layer. Every automation calls `switch.turn_on` directly
against a shared state machine, so when two of them disagree the outcome is decided by
firing order rather than by intent.

The case this was built for:

> A schedule turns the lights on in the morning. Later that day an event ends, its
> "turn everything off" automation fires, and the lights go off — mid-day, while the
> morning schedule is supposedly still in force.

**That is not a priority problem.** The morning automation fired once and then stopped
having an opinion. Its command is a past event, not a standing state, so anything that
runs later wins by default however you rank it. Ranking "daily" above "event" would be
wrong too — at 3am the event's cleanup *should* win.

What is missing is a record of **why** the lights are on and **who still needs them on**.

## Reasons

A **reason** is a named, ranked statement that some switches should be on (or off), alive
for a bounded period. The highest-priority live reason decides; if none are live, the
switch falls back to a configured state.

Two things make it work.

### Reasons have lifetimes

- A **window** opens and closes on a schedule, and its start and end are specified
  *independently*. A morning on-time and an evening off-time become one standing state
  instead of two edge triggers. Each edge can be a clock time, a sun event plus an
  offset, or an entity plus an offset — so an `input_datetime` or a timestamp `sensor`
  works, which matters when the time moves every day.
- A **latch** is opened by one automation and closed by another, like an `input_boolean`
  — but with a mandatory `max_hold`, so a close that never runs cannot pin the switches
  indefinitely.

### "Off" has two meanings

Most `turn_off` calls in a real config mean *"my reason has ended"*, not *"the lights must
now be off"*. Each automation is mapped to either **open** or **close** its reason:

| When this runs | It | The reason |
|---|---|---|
| `automation.party_lights_on` | opens | `party` |
| `automation.party_lights_off` | **closes** | `party` |

Closing at 11am leaves the daily reason live, so the lights stay on. The identical close
at 3am finds nothing live and the lights go off through the fallback — with no time
condition needed.

### Why not input_booleans

The reasons-and-booleans pattern is the right instinct, and for "on if any reason is on"
it is less machinery than this. It runs out at four points:

1. **No priority.** An OR cannot express a night lockout that forces *off*, or a person
   outranking everything for an hour.
2. **Latched booleans get stuck.** A boolean set by one automation and cleared by another
   never clears when the clearing automation's conditions do not match. `max_hold` bounds
   it.
3. **The wiring is large.** Five switches × seven reasons is ~35 helpers, two automations
   each, plus a template applier per switch.
4. **No attribution.** A boolean cannot tell you which automation set it, or that two
   automations fought two seconds apart.

Reasons are still exposed as `binary_sensor.reason_<name>`, so dashboards look the same as
if you had wired the helpers by hand.

## Three modes, per switch

| Mode | What it does |
|---|---|
| `advisory` | Never writes. Tracks reasons, attributes every change, reports conflicts. **Start here.** |
| `guard` | Existing automations keep calling `switch.turn_on`; Arbiter maps each command to a reason and reverts whatever loses. **No automation edits.** |
| `claims` | Automations call `arbiter.open` / `arbiter.close` directly. Arbiter is the only writer, so nothing flickers. |

## Requirements

Home Assistant **2026.2.0** or newer. The GUI is built on config subentries, and target
expansion uses `homeassistant.helpers.target.TargetSelection`, which landed in 2026.1.

## Install

### HACS

1. HACS → ⋮ (top right) → **Custom repositories**
2. URL `https://github.com/zalmyr/ha-arbiter`, type **Integration**, **ADD**
3. Find **Arbiter**, download it, restart Home Assistant
4. **Settings → Devices & Services → Add Integration → Arbiter**

### Manually

Copy `custom_components/arbiter/` from this repository into your Home Assistant config so
that `<config>/custom_components/arbiter/manifest.json` exists, restart, then add the
integration as above.

### It does not appear under Add Integration

Check **Settings → System → Logs** for this line, which Home Assistant emits at startup
for every custom integration it loads:

```
We found a custom integration arbiter which has not been tested by Home Assistant
```

- **Present** → it loaded. Look for a later `arbiter` traceback in the same log, and try a
  hard browser refresh, since the frontend caches the integrations list.
- **Absent** → Home Assistant never saw the folder, which is nearly always the path. The
  only layout that works is `<config>/custom_components/arbiter/manifest.json`. Unzipping
  a repository download and copying the whole folder gives you
  `<config>/custom_components/ha-arbiter-main/custom_components/arbiter/`, which loads
  nothing.

Everything is configured in the UI. Reasons, managed switches and automation mappings are
config *subentries*, so each gets its own add / edit / delete row on the integration page.

### Or bootstrap from YAML

[`arbiter.example.yaml`](arbiter.example.yaml) is a worked example covering both lifetimes,
all three boundary kinds, a weekday filter and a condition. Paste it into
`configuration.yaml` and restart once.

Import is a **one-time bootstrap, not a live sync**. After it lands the GUI is the source
of truth and the YAML block is ignored. Use `arbiter.export_config` to get the current
configuration back as YAML.

## What it gives you

Per managed switch:

- **`binary_sensor.<switch>_should_be_on`** — the resolved answer, independent of what the
  switch is actually doing. In advisory mode this is how you see what Arbiter *would* do
  before letting it act.
- `sensor.<switch>_winning_reason` — which reason is deciding, with the full live stack and
  expiry times in its attributes.
- `binary_sensor.<switch>_conflict` — on while two live reasons want opposite things.
- `switch.<switch>_arbitration` — per-switch escape hatch. Turn it off and Arbiter keeps
  tracking but stops writing to that switch.

Per reason: `binary_sensor.reason_<name>`, with the opening source and expiry.

And one overview of the whole picture:

- **`sensor.arbiter_reasons`** — how many reasons are in force, with the full set in its
  attributes: `live` (name, priority, what it wants, which switches, who opened it, when
  it expires), `idle` (configured but not currently holding anything), `unconfigured`
  (live reasons with no definition behind them — manual overrides, and the weak
  placeholders unmapped automations get), and `automations`, which lists what opens and
  closes each reason.

  The per-reason binary sensors only exist for reasons you configured, so this is the only
  place a manual override or an unmapped automation's reason shows up as something you can
  look at on a dashboard.

### Services

| Service | Purpose |
|---|---|
| `arbiter.open` / `close` / `close_all` | Drive a reason by hand. All three take an optional `targets`, so a reason covering several switches can be opened or released on only some of them |
| `arbiter.override` / `clear_overrides` | Hold a switch the way a person set it |
| `arbiter.set_mode` | Move a switch between advisory, guard and claims |
| `arbiter.explain` | What won, what lost, and why |
| `arbiter.find_conflicts` | Every automation that can command each switch, which are switched off, which have no mapping, and what has actually fought |
| `arbiter.export_config` | The live configuration as YAML |

#### Releasing part of a reason

`close` without `targets` drops the reason everywhere. With them it releases only those
switches and keeps holding the rest:

```yaml
action: arbiter.close
data:
  reason: party
  targets: switch.porch      # the hall is still held by 'party'
```

Releasing the last switch a reason covers closes it outright. The release lasts until
something re-opens the reason — a guard-mode source resolves its switches afresh from the
reason's definition every time it fires, so the full set comes back.

`close_all` takes the same field, which is the one to reach for when a single switch is
stuck: it frees that switch from everything holding it and leaves the others alone.

`arbiter.explain` is the one to reach for first:

```yaml
action: arbiter.explain
data:
  entity_id: switch.porch
```

```yaml
switches:
  switch.porch:
    should_be: "on"
    actual: "on"
    winning_reason: daily
    because: daily (priority 20) wants on
    live_reasons:
      - name: daily
        priority: 20
        wants: "on"
        opened_by: automation.daily_lights_on
        expires: "2026-09-06T22:00:00-04:00"
    overruled: []
```

## Migrating, safely

1. **Advisory, for a week.** Nothing is written. Watch
   `binary_sensor.<switch>_should_be_on` against the real switch state and call
   `arbiter.explain` whenever they diverge. Run `arbiter.find_conflicts` to list any
   automation that can reach a managed switch but has no mapping yet — switched-off
   automations are listed separately, since they need no mapping until you re-enable them.
2. **Guard, one switch at a time.** Arbiter now reverts whatever loses. Leave the
   automations exactly as they are.
3. **Claims, only where you want zero flicker.** Rewrite that automation's action from
   `switch.turn_on` to `arbiter.open`.

A global bypass entity stands Arbiter down completely, and
`switch.<switch>_arbitration` stands it down for one switch.

## How it knows who did what

Home Assistant reuses the caller's `Context` verbatim for the state write a service call
produces, so every change carries the id of whoever asked for it. Arbiter uses that twice:
to recognise its own writes (and not mistake them for someone overriding it), and to
attribute somebody else's — matching the context against the `automation_triggered` event
to name the automation behind it.

Commands are watched on the **service bus**, not only through state changes. An automation
turning on a switch that is already on produces no state change at all, so a state-only
watcher would silently miss its reason. State changes are still watched, for what the
service bus cannot see: a physical wall switch or a Z-Wave association.

A change with no automation behind it, or one carrying a `user_id`, is treated as a person
and held at the override priority for the override timeout. Scene-button automations
should be declared as override sources, because holding a wall switch is a person acting
even though an automation carries it out.

## Known limitations

- **Guard mode is reactive.** The switch visibly flicks for a moment before the revert.
  That is inherent to not rewriting the automations; `claims` mode is flicker-free.
- **`opens` / `closes` is a declaration, not a detection.** Arbiter cannot infer from a
  `switch.turn_off` call whether the author meant "close my reason" or "force off". A
  wrong mapping produces a wrong outcome.
- **Automation entity ids come from aliases.** Renaming an automation changes its entity id
  and its mapping stops matching. `arbiter.find_conflicts` lists unmapped automations,
  which is how you would notice.
- **Static schedule analysis is best-effort.** Automation trigger config is not a public
  API, so `find_conflicts` uses the public `automations_with_*` helpers plus the observed
  runtime conflict log rather than parsing trigger windows.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
pytest
```

Needs Python 3.13; older interpreters resolve to a Home Assistant too old for config
subentries.

## License

MIT
