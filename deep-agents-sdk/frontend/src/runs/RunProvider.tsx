import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type PropsWithChildren } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { streamChat } from "../api/sse";
import type { ActiveRun, StreamEvent, TaskRun } from "../types";

interface StartRunInput {
  projectId: string;
  sessionId: string;
  message: string;
}

interface RunContextValue {
  runs: Record<string, ActiveRun>;
  getRun: (projectId?: string, sessionId?: string) => ActiveRun | undefined;
  startRun: (input: StartRunInput) => Promise<void>;
  recoverRun: (projectId: string, sessionId: string, runId: string, status: string) => void;
}

const RunContext = createContext<RunContextValue | null>(null);
export const runKey = (projectId: string, sessionId: string) => `${projectId}:${sessionId}`;

export function RunProvider({ children }: PropsWithChildren) {
  const queryClient = useQueryClient();
  const [runs, setRuns] = useState<Record<string, ActiveRun>>({});
  const runsRef = useRef(runs);
  const controllers = useRef(new Map<string, AbortController>());
  const pollTimers = useRef(new Map<string, number>());

  useEffect(() => { runsRef.current = runs; }, [runs]);
  useEffect(() => () => {
    controllers.current.forEach((controller) => controller.abort());
    pollTimers.current.forEach((timer) => window.clearTimeout(timer));
  }, []);

  const refresh = useCallback(async (projectId: string, sessionId: string) => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["sessions", projectId] }),
      queryClient.invalidateQueries({ queryKey: ["session", projectId, sessionId] }),
      queryClient.invalidateQueries({ queryKey: ["runs", projectId, sessionId] }),
    ]);
  }, [queryClient]);

  const remove = useCallback((projectId: string, sessionId: string) => {
    const key = runKey(projectId, sessionId);
    const timer = pollTimers.current.get(key);
    if (timer) window.clearTimeout(timer);
    pollTimers.current.delete(key);
    controllers.current.delete(key);
    setRuns((current) => {
      if (!current[key]) return current;
      const next = { ...current };
      delete next[key];
      return next;
    });
  }, []);

  const patchRun = useCallback((key: string, patch: Partial<ActiveRun>) => {
    setRuns((current) => current[key] ? { ...current, [key]: { ...current[key], ...patch } } : current);
  }, []);

  const schedulePollRef = useRef<(projectId: string, sessionId: string, runId: string, delay?: number) => void>(() => undefined);
  const poll = useCallback(async (projectId: string, sessionId: string, runId: string) => {
    const key = runKey(projectId, sessionId);
    pollTimers.current.delete(key);
    const active = runsRef.current[key];
    if (!active || active.runId !== runId || active.streamConnected) return;
    try {
      const result = await api<{ runs: TaskRun[] }>(`/api/projects/${projectId}/sessions/${sessionId}/runs`);
      const latest = result.runs.find((item) => item.id === runId) || result.runs.find((item) => item.status === "running");
      if (latest?.status === "running") {
        patchRun(key, { runId: latest.id, status: latest.latest_status || active.status });
        schedulePollRef.current(projectId, sessionId, latest.id, 1500);
      } else {
        remove(projectId, sessionId);
        await refresh(projectId, sessionId);
      }
    } catch {
      schedulePollRef.current(projectId, sessionId, runId, 3000);
    }
  }, [patchRun, refresh, remove]);

  const schedulePoll = useCallback((projectId: string, sessionId: string, runId: string, delay = 1500) => {
    const key = runKey(projectId, sessionId);
    if (pollTimers.current.has(key)) return;
    pollTimers.current.set(key, window.setTimeout(() => { void poll(projectId, sessionId, runId); }, delay));
  }, [poll]);
  schedulePollRef.current = schedulePoll;

  const recoverRun = useCallback((projectId: string, sessionId: string, runId: string, status: string) => {
    const key = runKey(projectId, sessionId);
    setRuns((current) => current[key] ? current : {
      ...current,
      [key]: {
        key, projectId, sessionId, runId, status: status || "Working…",
        streamConnected: false, error: null,
      },
    });
    schedulePoll(projectId, sessionId, runId);
  }, [schedulePoll]);

  const startRun = useCallback(async ({ projectId, sessionId, message }: StartRunInput) => {
    const key = runKey(projectId, sessionId);
    if (runsRef.current[key]) return;
    const controller = new AbortController();
    controllers.current.set(key, controller);
    const initial: ActiveRun = {
      key, projectId, sessionId, runId: null, status: "Preparing the agent workspace…",
      streamConnected: false, error: null,
    };
    runsRef.current = { ...runsRef.current, [key]: initial };
    setRuns((current) => ({ ...current, [key]: initial }));
    let receivedFinal = false;
    try {
      await streamChat({ project_id: projectId, session_id: sessionId, message }, async (event: StreamEvent) => {
        const data = event.data as Record<string, unknown>;
        if (event.type === "run") {
          patchRun(key, {
            runId: String(data.run_id || ""), status: String(data.status || "Working…"), streamConnected: true,
          });
        } else if (event.type === "status") {
          patchRun(key, { runId: String(data.run_id || runsRef.current[key]?.runId || ""), status: String(data.message || "Working…") });
        } else if (event.type === "final") {
          receivedFinal = true;
          remove(projectId, sessionId);
          await refresh(projectId, sessionId);
        } else if (event.type === "error") {
          throw new Error(String(data.message || "Streaming request failed"));
        }
      }, controller.signal);
      if (!receivedFinal) throw new Error("Streaming response ended without a final answer");
    } catch (error) {
      if (controller.signal.aborted) return;
      patchRun(key, { error: error instanceof Error ? error.message : "Request failed", status: "Request failed", streamConnected: false });
      const runId = runsRef.current[key]?.runId;
      if (runId) schedulePoll(projectId, sessionId, runId, 1000);
      else window.setTimeout(() => { remove(projectId, sessionId); void refresh(projectId, sessionId); }, 2500);
    }
  }, [patchRun, refresh, remove, schedulePoll]);

  const value = useMemo<RunContextValue>(() => ({
    runs,
    getRun: (projectId, sessionId) => projectId && sessionId ? runs[runKey(projectId, sessionId)] : undefined,
    startRun,
    recoverRun,
  }), [recoverRun, runs, startRun]);

  return <RunContext.Provider value={value}>{children}</RunContext.Provider>;
}

export function useRuns(): RunContextValue {
  const value = useContext(RunContext);
  if (!value) throw new Error("useRuns must be used within RunProvider");
  return value;
}
