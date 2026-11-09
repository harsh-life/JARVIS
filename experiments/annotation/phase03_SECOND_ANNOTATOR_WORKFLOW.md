# Second annotation of the Phase 0.3 held-out set: coordinator workflow

1. Give the annotator ONLY the folder `phase03_packet/` (INSTRUCTIONS.md,
   requests.yaml, annotation_template.yaml). Do not give them this file, the
   key, `phase03_annotator_A.yaml`, any suite file, code or results.
2. They fill a copy of `annotation_template.yaml` and return it. Save it as
   `experiments/annotation/phase03_annotator_B.yaml`. Never edit
   `phase03_annotator_A.yaml`.
3. `python -m experiments annotation validate --set phase03 --file experiments/annotation/phase03_annotator_B.yaml`
4. `python -m experiments annotation agreement --set phase03` writes
   `phase03_agreement.json` and `phase03_adjudication.yaml` (disagreements only).
5. Two people (not the rules author alone) decide each disagreement in
   `phase03_adjudication.yaml` (`decision: A`, `B`, or a corrected entry, with
   a rationale).
6. `python -m experiments annotation adjudicate --set phase03` writes the gold
   labels. Then re-score the stored model run with
   `python -m experiments phase04-eval --suite <gold> --replay <run>`.
