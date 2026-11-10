// Replays input sequences through the JavaScript ActivityPolicy and prints the
// presentation after every step — the JS half of
// tests/presentation/test_activity_policy_js_parity.py. Reads JSON from stdin:
//   [{"settings": {...overrides...}, "steps": [{"at": ms, "report": "<text>"} |
//                                              {"at": ms, "activity": true} |
//                                              {"at": ms, "tick": true}]}, ...]
// and writes [[[shown, intensity], ...], ...]. No file or network access.
import { ActivityPolicy, DEFAULT_SETTINGS, parseSettings } from "../activity-policy.js";

function merge(base, extra) {
  const out = structuredClone(base);
  for (const [k, v] of Object.entries(extra)) {
    out[k] = v && typeof v === "object" && !Array.isArray(v) && out[k] && typeof out[k] === "object"
      ? merge(out[k], v) : v;
  }
  return out;
}

let input = "";
process.stdin.setEncoding("utf8");
for await (const chunk of process.stdin) input += chunk;
const runs = JSON.parse(input).map((run) => {
  const policy = new ActivityPolicy(parseSettings(merge(structuredClone(DEFAULT_SETTINGS), run.settings || {})), 0);
  return run.steps.map((step) => {
    if ("report" in step) policy.onReport(step.report, step.at);
    else if (step.activity) policy.onLocalActivity(step.at);
    else policy.tick(step.at);
    return [policy.shown, policy.intensity];
  });
});
process.stdout.write(JSON.stringify(runs));
