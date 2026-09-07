# Changelog

All notable changes to Arbiter are recorded here. Versions follow
[semantic versioning](https://semver.org/); the version in
`custom_components/arbiter/manifest.json` is what Home Assistant displays, and the tag of
the latest published GitHub release is what HACS reports.

## 0.4.0

### Added

- The `reason` field on `arbiter.open` and `arbiter.close` is a dropdown of the reasons
  that actually exist, rather than free text. `open` lists the ones you have defined;
  `close` also lists whatever is currently live, so a manual override or an unmapped
  automation's placeholder can be closed rather than merely watched.
- The automation-mapping form can define a reason without leaving it. Picking
  **➕ Create a new reason…** collects the essentials, saves a real reason, and finishes
  the mapping that needed it. Previously the form aborted outright when no reasons
  existed, which was a dead end.

### Changed

- **`arbiter.open` and `arbiter.close` now refuse a reason name that is not known**,
  raising with the valid names listed. A mistyped name in `close` used to do nothing and
  report nothing. This is a behaviour change for anyone calling `arbiter.open` from YAML
  with an ad-hoc name — define the reason first, or use `arbiter.override` for a one-off
  hold. Overrides themselves are unaffected.
- The mapping form lists only **latch** reasons. A source pointed at a window was already
  ignored and reported as a config problem, since a window opens and closes on its own
  schedule; leaving them out prevents the mistake instead of explaining it afterwards. An
  existing mapping keeps whatever it already points at when you edit it.

## 0.3.0

### Added

- `sensor.arbiter_reasons` — a single overview of every reason. Its state is the number
  in force; its attributes carry the live set with priorities, switches, opener and
  expiry, the configured-but-idle ones, the live ones with no definition behind them, and
  a map of which automations open and close each reason.

  The per-reason `binary_sensor.reason_*` entities are only created for configured
  reasons, so manual overrides and the placeholders held for unmapped automations
  previously had no entity at all and were visible only through `arbiter.explain`.

## 0.2.0

### Added

- `arbiter.close` and `arbiter.close_all` accept an optional `targets` list, so a reason
  covering several switches can be released on only some of them. One room's event ending
  no longer has to take down a reason the other rooms still rely on, and a single stuck
  switch can be freed from everything holding it.
- `EVENT_CLOSED` (`arbiter_closed`) carries a `released` list naming the entities that were
  freed, so an automation can tell a partial release from a full close. An empty list means
  the whole reason went.

### Changed

- Releasing the last switch a reason covers closes that reason outright rather than
  leaving an empty one behind.
- `arbiter.close_all` is now registered with a schema. It previously had none and silently
  accepted anything passed to it.

Omitting `targets` behaves exactly as before. Note that a release lasts only until
something re-opens the reason: a guard-mode source resolves its switches afresh from the
reason's definition each time it fires, so the full set comes back.

## 0.1.0

Initial release.

- **Reasons** — a named, ranked statement that some switches should be on (or off), alive
  for a bounded period. The highest-priority live reason decides; with none live a switch
  falls back to a configured state.
- **Two lifetimes** — a *window* opens and closes on independently specified schedules,
  each edge being a clock time, a sun event plus an offset, or an entity plus an offset; a
  *latch* is opened by one automation and closed by another, bounded by a mandatory
  `max_hold` so a missed close cannot pin the switches indefinitely.
- **Open versus close** — an automation can be mapped to *close* its reason rather than
  assert off, so an event ending lets whatever else is still live keep the switches on.
- **Three modes per switch** — `advisory` watches and reports without writing, `guard`
  reverts whatever loses without any automation edits, `claims` makes Arbiter the sole
  writer.
- **Attribution** — commands are watched on the service bus as well as through state
  changes, since an automation turning on a switch that is already on produces no state
  change at all. Changes with no automation behind them, or carrying a user id, are treated
  as a person and held at the override priority.
- **GUI configuration** via config subentries, with YAML import as a one-time bootstrap and
  `arbiter.export_config` to get it back out.
- **Entities** — `binary_sensor.<switch>_should_be_on`, `sensor.<switch>_winning_reason`,
  `binary_sensor.<switch>_conflict`, `switch.<switch>_arbitration`, and
  `binary_sensor.reason_<name>` per reason.
- **Services** — `open`, `close`, `close_all`, `override`, `clear_overrides`, `set_mode`,
  `explain`, `find_conflicts`, `export_config`.
