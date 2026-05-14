// Centralized React Query keys + shared options. Importing from here
// keeps invalidation correct across pages.

import { useQuery, type UseQueryOptions } from "@tanstack/react-query";
import * as api from "./api";
import type { Campaign, Health, Metrics, Run } from "./types";

export const qk = {
  health: () => ["health"] as const,
  metrics: () => ["metrics"] as const,
  runs: () => ["runs"] as const,
  run: (id: string) => ["run", id] as const,
  campaigns: () => ["campaigns"] as const,
};

type Opt<T> = Omit<UseQueryOptions<T, Error, T, readonly unknown[]>, "queryKey" | "queryFn">;

export const useHealth = (opt?: Opt<Health>) =>
  useQuery({ queryKey: qk.health(), queryFn: api.getHealth, refetchInterval: 5_000, ...opt });

export const useMetrics = (opt?: Opt<Metrics>) =>
  useQuery({ queryKey: qk.metrics(), queryFn: api.getMetrics, refetchInterval: 5_000, ...opt });

export const useRuns = (opt?: Opt<Run[]>) =>
  useQuery({ queryKey: qk.runs(), queryFn: api.getRuns, refetchInterval: 3_000, ...opt });

export const useRun = (id: string, opt?: Opt<Run | undefined>) =>
  useQuery({
    queryKey: qk.run(id),
    queryFn: () => api.getRun(id),
    refetchInterval: 2_500,
    enabled: !!id,
    ...opt,
  });

export const useCampaigns = (opt?: Opt<Campaign[]>) =>
  useQuery({ queryKey: qk.campaigns(), queryFn: api.getCampaigns, refetchInterval: 5_000, ...opt });
