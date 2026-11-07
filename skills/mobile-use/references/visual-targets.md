# Local visual relocation

Use only when get_status reports local_visual_relocation and the current inspected image has a readable horizontal target label plus distinctive nearby context. Prefer element_id/target for accessible controls. Icons, unclear labels, unmatched text and multiple action candidates still need a new decision.

On tap, add visual_target={text,context_text,search_bounds?} alongside x/y and visual_bounds. text and context_text must be exact complete labels visible in the current image; only whitespace is normalized. Context must distinguish the intended item/card, not merely supply a generic label found elsewhere. visual_bounds contains the target label, including a little space around its lettering. search_bounds optionally limits both labels to an inspected original-pixel region and must allow the intended movement; omitted means the display. Do not narrow a region just to hide competing choices.

Example for an inspected document card: x/y points to “Open”, visual_bounds encloses that label, and visual_target={text:"Open",context_text:"Report.pdf"}. Use the actual bounds and labels, never example coordinates. A changed price, recipient, item or dialog requires a new decision; include additional accessible facts through context_element_ids where relevant.

With visual_container_binding, the service binds both labels to the smallest uniquely identifiable structural container enclosing their evidence, falling back to the inspected search region/window when structure has no smaller group. The action label must be unique in that scope; repeated copies of the same context label are supporting evidence, not extra actions. The current container and exact labels must match, but internal direction, spacing and font size may change. This is label/container identity, not product understanding: a generic brand does not distinguish different products. Choose distinctive item context and bind additional important facts when needed. A window-sized scope provides weaker grouping than an exposed card; do not describe it as a verified card. It then checks a second fresh frame at both labels plus current foreground, display, windows and container surfaces. One further read-only attempt handles motion or a transient absent label during recognition; no input is sent until validation passes. No matching target, ambiguity, changed context or interruption returns not_sent, usually with the fresh recovery image. Plain coordinates retain strict local pixel validation.

The bundled CPU OCR runs locally without screenshot upload or model downloads during a tap. Initial engine loading adds one-time cost. Detection covers the selected region; bounded recognition considers compatible text-line shapes. This is OCR evidence, not semantic understanding or a complete hit-test: small/rotated text, wrong recognition, repeated labels, and custom overlays can defeat it. Do not use visual relocation to infer permission for a changed action.

visual_target can combine with an already authorized tap.follow_up. Relocation happens before input; follow-up timing starts at acknowledged input, never when OCR starts. Only the final observation is delivered. Inspect validation.status/bounds, continuation.status and each input_status. Full diagnostics and local read counts remain in the audit. Two fresh validation screenshots normally replace a rejection/model/retry round trip; fewer delivered images does not necessarily mean fewer device captures.

## Cached candidate IDs

After inspecting an image, get_observation(observation_id,visual_query={text,context_text,search_bounds?},image="never",detail="brief") performs OCR on that cached image only. It returns visual_candidates with bounds, confidence, context and eligible IDs. No device reads, new observation or age renewal occur. Select an inspected candidate with tap(observation_id,visual_candidate_id=...). Do not also pass coordinates, visual_target or another selector. The tap still re-resolves against fresh pixels/tree; cached OCR is reused only for its reference binding. IDs expire with their observation. Ambiguous/low-confidence/context-missing candidates have rejection evidence, not actionable IDs. An OCR label is not proof of clickability or task authorization.

### Compare alternatives on the first returned frame

With `cached_visual_alternatives`, use `get_observation(visual_query_any_of=...)` instead of first trying an incomplete navigation list and then querying missing alternatives separately. Declare the complete authorized set in one shared region, with distinct `text` and identifying `context_text` for every candidate. Read `limits.cached_visual_candidates`; it is separate from the navigation limit. Never truncate the set or split it into input attempts.

```json
{
  "observation_id": "CURRENT_ID",
  "visual_query_any_of": {
    "candidates": [
      {"text": "Open", "context_text": "Report A"},
      {"text": "Preview", "context_text": "Report A"}
    ]
  },
  "detail": "brief",
  "image": "never"
}
```

`observation.visual_candidates.status="matched"` returns one candidate ID. `unresolved` returns the reason and relevant matches without IDs: missing context, duplicate targets and competing alternatives remain decision points. A tap using the returned ID confirms **all declared alternatives** on fresh pixels as well as the selected target/container; a newly appearing competitor blocks input. Single-query IDs retain their original single-target contract.

With `shared_ocr_frames`, queries on the same cached image object and exact region share detection and decoded lines; new label requests decode only previously unexamined compatible lines. Different frames/regions are separate, the cache is bounded, and disconnect clears it. This does not extend observation age or authorize reuse of old coordinates. `vision.detections`, `recognitions`, `frame_hits` and `line_hits` report per-call work; recognition counts are text-line inferences, not screenshots. Reusing a screenshot does not imply zero OCR work.

### Query conditions and actions together

With `same_frame_conditions`, use `get_observation.visual_query_batch` when a visual decision needs both page facts and an action. It shares one OCR read over the union of labels in one cached frame/region, instead of separate condition and candidate calls. Use only one of `visual_query`, `visual_query_any_of` and `visual_query_batch` per call.

```json
{
  "observation_id": "CURRENT_ID",
  "visual_query_batch": {
    "conditions": [
      {"name": "ready", "text": "Ready", "context_text": "Report A"},
      {"name": "busy", "text": "Downloading", "context_text": "Report A"}
    ],
    "candidates": [
      {"text": "Open", "context_text": "Report A"},
      {"text": "Preview", "context_text": "Report A"}
    ],
    "require": ["ready"]
  },
  "detail": "brief",
  "image": "never"
}
```

Read `observation.visual_batch.conditions` by `name`. Each returns `matched`, `missing`, `ambiguous`, `low_confidence` or `unresolved`, with bounds/context on a match and a code otherwise. `missing` means the OCR evidence did not match, not proof of physical absence. Names are opaque caller labels: the service does not infer completion, mutually exclusive states, priority, waits or return actions. Interpret conflicts in the task flow.

`visual_batch.target` uses the existing candidate result shape. A unique target gets an ID only if **every** condition named in `require` matches. Otherwise it remains `blocked` without an ID, with unmet names in `visual_batch.unmet`; missing/competing targets stay `unresolved`. Pass a returned ID to the existing `tap(visual_candidate_id=...)` API. The ID retains all alternatives and required conditions. Fresh validation checks their labels, scoped uniqueness, containers, relevant layout and confirmation pixels. A changed premise returns `not_sent` with `visual_condition_changed`; it does not switch branches or replay input. Conditions omitted from `require` are evidence only, not tap guards.

For read-only decisions omit both `candidates` and `require`; conditions themselves never issue action IDs. Read `limits.visual_conditions` (condition count) and `limits.cached_visual_candidates` (action count). A batch requires at least one condition; optional candidates may contain one or more actions, each with distinct text and required context. Condition names must be unique. Condition context is optional for a page-wide indicator, but include item context when it matters. Optional condition `search_bounds` limits matching within the shared batch `search_bounds` without starting another detection; both are original-pixel rectangles and the condition region must be contained in the shared region. Never crop away competing evidence.

This query performs no device reads, new screenshot, age renewal or input. It uses the current cached PNG in target mode and retains the existing OCR work limit. A backend/work-limit error fails the query rather than turning partial OCR into a missing condition. Old single-query IDs and unguarded alternative IDs retain their original contracts.

For already-known future pages, use navigate's visual_target instead; there is no need for a candidate-query round trip or model-provided rectangle. See [known visual actions](navigation.md#known-visual-actions-and-immediate-follow-up). Reference-image binding errors include sampled OCR labels/bounds; with the existing save_screenshots opt-in they also persist the reference image. Visual navigation errors include the sampled label timestamp and, for layout failures, window evidence. These diagnostics do not authorize replay.
