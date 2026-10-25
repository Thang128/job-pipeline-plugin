# Tailoring overlay contract

Write YAML under the private workspace `private/jobs/`. Only `headline`, `summary`, `skills` and `experience_bullets` are accepted. Omit unchanged zones. They must also be editable in the approved block mapping.

Each claim has exactly `text` and a nonempty, unique `evidence_ids` list referring to `private/candidate.yml` career evidence. This synthetic structure illustrates the shape; it is not a fact to reuse:

```yaml
headline:
  text: "A title supported by this user's experience"
  evidence_ids: [reviewed-role]
summary:
  text: "A factual summary supported by the cited private evidence"
  evidence_ids: [reviewed-role]
skills:
  - text: "A verified skill"
    evidence_ids: [reviewed-skill]
experience_bullets:
  reviewed-experience-id:
    - text: "An achievement supported by this user's evidence"
      evidence_ids: [reviewed-achievement]
```

Experience IDs must match the private creator's reviewed semantic mapping/template. A headline/summary is one claim; skills are a nonempty list of claims; experience bullets map IDs to nonempty claim lists.

Quantitative/date/version statements require matching context and units in one cited evidence clause. A matching number elsewhere in the CV is insufficient. Unquantified wording is not automatically proven true; visibly review qualitative claims against sources.

Run the builder, inspect every preview, prepare/review the cover letter, then use `operations review-documents` to bind exact file/master/creator hashes. Failed builds never justify bypass flags, generic substitutes, edited protected facts or invented evidence.
