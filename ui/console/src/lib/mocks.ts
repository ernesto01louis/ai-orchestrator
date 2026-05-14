// Mock fixtures used when VITE_USE_MOCKS=1 (or when the backend is
// unreachable during development). Shapes match src/lib/types.ts.

import type {
  Campaign,
  Health,
  Metrics,
  Run,
  WsMessage,
} from "./types";

const ago = (s: number): string => new Date(Date.now() - s * 1000).toISOString();

export const MOCK_RUNS: Run[] = [
  {
    id: "run_01HZK6QF8E2XAH9R4M",
    project: "wrroc-fixture-pack",
    campaign_id: "cmp_8ftz",
    phase: "generator",
    score: 0.71,
    model: "qwen2.5:14b-instruct-q4_K_M",
    target: "homelab-a100",
    started_at: ago(312),
    paused: null,
    hitl_mode: "full_auto",
    confidence: 0.88,
  },
  {
    id: "run_01HZK6Q9V2CBRT81N3",
    project: "wrroc-fixture-pack",
    campaign_id: "cmp_8ftz",
    phase: "post_planner",
    score: null,
    model: "qwen2.5:14b-instruct-q4_K_M",
    target: "homelab-a100",
    started_at: ago(174),
    paused: "hitl:checkpoint",
    hitl_mode: "checkpoint",
    confidence: 0.42,
  },
  {
    id: "run_01HZK62RXT7H5WJ0PA",
    project: "citation-grade-eval",
    campaign_id: "cmp_2qab",
    phase: "judge",
    score: 0.83,
    model: "llama3.1:70b-instruct-q4_0",
    target: "homelab-a100",
    started_at: ago(942),
    paused: null,
    hitl_mode: "full_auto",
    confidence: 0.91,
  },
  {
    id: "run_01HZK5T0YE9F2GMD4Q",
    project: "citation-grade-eval",
    campaign_id: "cmp_2qab",
    phase: "optimizer",
    score: 0.79,
    model: "llama3.1:70b-instruct-q4_0",
    target: "homelab-a100",
    started_at: ago(1180),
    paused: "smartpause",
    hitl_mode: "smartpause",
    confidence: 0.31,
  },
  {
    id: "run_01HZK4ABNR6P7VCT2K",
    project: "rag-router-bench",
    campaign_id: "cmp_91xs",
    phase: "complete",
    score: 0.86,
    model: "mistral-nemo:12b-instruct-2407-q5_K_M",
    target: "homelab-3090",
    started_at: ago(3650),
    paused: null,
    hitl_mode: "full_auto",
    confidence: 0.93,
  },
  {
    id: "run_01HZK3W5J1DMFXY8VL",
    project: "rag-router-bench",
    campaign_id: "cmp_91xs",
    phase: "complete",
    score: 0.74,
    model: "mistral-nemo:12b-instruct-2407-q5_K_M",
    target: "homelab-3090",
    started_at: ago(4120),
    paused: null,
    hitl_mode: "full_auto",
    confidence: 0.81,
  },
  {
    id: "run_01HZK2P9QC0RBHF3SN",
    project: "planner-ablation-v3",
    campaign_id: "cmp_lk44",
    phase: "failed",
    score: null,
    model: "qwen2.5:7b-instruct-q5_K_M",
    target: "homelab-3090",
    started_at: ago(5400),
    paused: null,
    hitl_mode: "checkpoint",
    confidence: 0.18,
  },
];

export const MOCK_CAMPAIGNS: Campaign[] = [
  {
    id: "cmp_8ftz",
    name: "wrroc-fixture-pack · grid-04",
    hitl_mode: "checkpoint",
    status: "running",
    children: 2,
    completed: 0,
    failed: 0,
    started_at: ago(420),
    budget: { used: 1.42, total: 6.0, percentage: 23.7, state: "healthy" },
    grid: { temperature: [0.2, 0.7, 1.0], top_p: [0.9, 0.95], seed: [42, 1337] },
  },
  {
    id: "cmp_2qab",
    name: "citation-grade-eval · run-12",
    hitl_mode: "full_auto",
    status: "running",
    children: 6,
    completed: 4,
    failed: 0,
    started_at: ago(1980),
    budget: { used: 4.18, total: 6.0, percentage: 69.7, state: "warn" },
    grid: { temperature: [0.3, 0.6, 0.9], judge_model: ["llama3.1:70b", "qwen2.5:72b"] },
  },
  {
    id: "cmp_91xs",
    name: "rag-router-bench · sweep-a",
    hitl_mode: "full_auto",
    status: "complete",
    children: 8,
    completed: 8,
    failed: 0,
    started_at: ago(6200),
    budget: { used: 2.91, total: 4.0, percentage: 72.8, state: "healthy" },
    grid: { router: ["bm25", "hybrid", "dense"], k: [4, 8] },
  },
  {
    id: "cmp_lk44",
    name: "planner-ablation-v3 · pilot",
    hitl_mode: "checkpoint",
    status: "aborted",
    children: 4,
    completed: 1,
    failed: 2,
    started_at: ago(7800),
    budget: { used: 0.84, total: 2.0, percentage: 42.0, state: "healthy" },
    grid: { planner_temperature: [0.0, 0.4], reflect: [false, true] },
  },
];

export const MOCK_HEALTH: Health = {
  orchestrator: "ok",
  ollama: "ok",
  hindsight: "ok",
  postgres: "ok",
  redis: "ok",
  tempo: "ok",
  prometheus: "ok",
  dvc: "ok",
  uptime_s: 124_563,
  version: "0.14.2-rc.3",
};

function genSpark(seed: number, n = 32, base = 50, vol = 12): number[] {
  let s = seed;
  const arr: number[] = [];
  for (let i = 0; i < n; i++) {
    s = (s * 9301 + 49297) % 233280;
    const r = (s / 233280 - 0.5) * 2;
    arr.push(Math.max(2, base + r * vol + Math.sin(i / 4) * vol * 0.3));
  }
  return arr;
}

export const MOCK_METRICS: Metrics = {
  llm_calls_total: 24_891,
  llm_calls_rate_5m: 12.4,
  llm_tokens_in_total: 18_204_117,
  llm_tokens_out_total: 5_431_902,
  llm_p50_ms: 482,
  llm_p95_ms: 2150,
  llm_p99_ms: 4830,
  campaigns_active: 2,
  runs_active: 4,
  runs_paused: 2,
  budget_total_usd: 18.0,
  budget_used_usd: 6.44,
  ollama_queue_depth: 3,
  ollama_gpu_util: 0.72,
  ollama_vram_used_gb: 38.4,
  ollama_vram_total_gb: 80.0,
  sparks: {
    llm_rate: genSpark(11, 40, 12, 4),
    p95: genSpark(7, 40, 2200, 600),
    queue: genSpark(31, 40, 3, 2),
    gpu: genSpark(99, 40, 70, 10),
  },
};

const SAMPLE_LINES: Array<[string, string]> = [
  ["planner", "decomposing task: 4 sub-goals identified"],
  ["planner", "tool selection: search.web, fs.read, code.run"],
  ["generator", "draft 1/3 token=128 latency=412ms"],
  ["generator", "draft 2/3 token=256 latency=518ms"],
  ["generator", "draft 3/3 token=412 latency=702ms"],
  ["judge", "rubric=citation_grade.v3 score=0.71 pass=true"],
  ["judge", "rubric=factuality.v2 score=0.84 pass=true"],
  ["optimizer", "applying delta: temperature 0.7 → 0.55"],
  ["optimizer", "applying delta: top_p 0.95 → 0.90"],
  ["planner", "memory hit: 3 prior traces, 2 above threshold"],
];

/** Fake /ws emitter — used when VITE_USE_MOCKS=1. */
export function createMockWsBus(): {
  subscribe: (fn: (m: WsMessage) => void) => () => void;
} {
  const subs = new Set<(m: WsMessage) => void>();
  let seq = 0;
  setInterval(() => {
    const r = MOCK_RUNS[seq % MOCK_RUNS.length];
    if (r.phase === "complete" || r.phase === "failed") {
      seq++;
      return;
    }
    const sample = SAMPLE_LINES[seq % SAMPLE_LINES.length];
    const msg: WsMessage = {
      type: "log",
      run_id: r.id,
      phase: sample[0] as WsMessage["phase"],
      line: sample[1],
      ts: new Date().toISOString(),
    };
    subs.forEach((fn) => fn(msg));
    seq++;
  }, 750);
  return {
    subscribe(fn) {
      subs.add(fn);
      return () => subs.delete(fn);
    },
  };
}
