// The Redlines forecasting tools as a pi extension (the pi side-by-side
// trial, 2026-09-02; Nick: "personally I have had good experiences with pi").
//
// Same four tools our own loop serves (redlines/llm.py call_tools +
// code/run_unified.py): web_search and read_page shell out to the SAME Python
// implementations (code/pi_harness/tool_cli.py -> redlines/tools.py), so the
// comparison is of the loop, not of two search stacks; submit_cells and
// submit_forecast keep the pieces and the final answer on disk for the
// driver (code/pi_harness/run_pi.py) to merge exactly as run_one_joint does.
// The research floor is enforced here too: the submission tools answer with
// an error until `min_research` tool calls have returned.
//
// Loaded by the driver with `pi -p --mode json --no-builtin-tools -e <this>`;
// reads $REDLINES_PI_OUT/spec.json (written by the driver) for the cell
// schema and the floor, and writes pieces.jsonl / final.json beside it.
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { execFileSync } from "node:child_process";
import { appendFileSync, readFileSync, writeFileSync } from "node:fs";

const OUT = process.env.REDLINES_PI_OUT;
if (!OUT) throw new Error("REDLINES_PI_OUT is not set (the driver sets it)");
const PY = process.env.REDLINES_PI_PY || "python3";
const spec = JSON.parse(readFileSync(`${OUT}/spec.json`, "utf8"));
const allowed: Record<string, string[]> = spec.allowed;         // question_id -> horizons
const wantCells: number = spec.want_cells;
const minResearch: number = spec.min_research;
let research = 0;
const received = new Map<string, any>();
const cap = (s: string, n = 8000) => (s.length > n ? s.slice(0, n) : s);

function py(tool: string, args: any) {
  const out = execFileSync(PY, [`${spec.repo}/code/pi_harness/tool_cli.py`, tool, JSON.stringify(args)],
    { encoding: "utf8", maxBuffer: 64 * 1024 * 1024, env: process.env });
  return JSON.parse(out);
}

function notYet(name: string) {
  return { content: [{ type: "text" as const, text: JSON.stringify({ error: `${name} is not offered yet: ${minResearch - research} more research call(s) must return first (${research} of ${minResearch} so far)` }) }], details: {}, isError: true };
}

function missing() {
  const out: string[] = [];
  for (const q of Object.keys(allowed)) for (const h of allowed[q]) if (!received.has(`${q}|${h}`)) out.push(`${q} @ ${h}`);
  return out;
}

const cells = Type.Array(Type.Object({
  question_id: Type.String(),
  horizon: Type.String(),
  probabilities: Type.Record(Type.String(), Type.Number({ minimum: 0, maximum: 1 })),
}));

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "web_search", label: "web_search",
    description: spec.tools.web_search.description,
    parameters: Type.Object({
      query: Type.String(),
      max_results: Type.Optional(Type.Integer({ minimum: 1, maximum: 10 })),
      recent_days: Type.Optional(Type.Integer({ minimum: 1 })),
    }),
    async execute(_id, params) {
      const r = py("web_search", params);
      research += 1;
      return { content: [{ type: "text", text: cap(JSON.stringify(r)) }], details: r };
    },
  });
  pi.registerTool({
    name: "read_page", label: "read_page",
    description: spec.tools.read_page.description,
    parameters: Type.Object({ url: Type.String(), offset: Type.Optional(Type.Integer({ minimum: 0 })) }),
    async execute(_id, params) {
      const r = py("read_page", params);
      research += 1;
      return { content: [{ type: "text", text: cap(JSON.stringify(r)) }], details: r };
    },
  });
  pi.registerTool({
    name: "submit_cells", label: "submit_cells",
    description: spec.tools.submit_cells.description,
    parameters: Type.Object({ forecasts: cells }),
    async execute(_id, params: any) {
      if (research < minResearch) return notYet("submit_cells");
      let n = 0;
      for (const f of params.forecasts || []) {
        if (allowed[f.question_id] && allowed[f.question_id].includes(f.horizon) && f.probabilities && typeof f.probabilities === "object") {
          received.set(`${f.question_id}|${f.horizon}`, f);
          n += 1;
        }
      }
      appendFileSync(`${OUT}/pieces.jsonl`, JSON.stringify({ t: Date.now(), accepted: n, forecasts: params.forecasts }) + "\n");
      const miss = missing();
      const r = { accepted: n, received: received.size, of: wantCells, missing: miss.length, missing_first: miss.slice(0, 20) };
      return { content: [{ type: "text", text: JSON.stringify(r) }], details: r };
    },
  });
  const finalProps: Record<string, any> = {
    forecasts: Type.Optional(cells),
    rationale: Type.String(),
    key_sources: Type.Optional(Type.Array(Type.String())),
  };
  for (const e of spec.elicits) finalProps[e.key] = Type.Record(Type.String(), Type.Number());
  pi.registerTool({
    name: "submit_forecast", label: "submit_forecast",
    description: spec.tools.submit_forecast.description,
    parameters: Type.Object(finalProps),
    async execute(_id, params: any) {
      if (research < minResearch) return notYet("submit_forecast");
      writeFileSync(`${OUT}/final.json`, JSON.stringify({ t: Date.now(), research, answer: params }, null, 1));
      const r = { ok: true, received: received.size, of: wantCells, in_final: (params.forecasts || []).length };
      return { content: [{ type: "text", text: JSON.stringify(r) + " Done: stop here." }], details: r };
    },
  });
}
