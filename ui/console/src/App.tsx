import { Routes, Route, Navigate } from "react-router-dom";
import { AppShell } from "@/shell/AppShell";
import { Dashboard } from "@/pages/Dashboard";
import { RunsList } from "@/pages/RunsList";
import { RunDetail } from "@/pages/RunDetail";
import { Hitl } from "@/pages/Hitl";
import { Timeline } from "@/pages/Timeline";
import { Stub } from "@/pages/Stub";

export default function App() {
  return (
    <AppShell>
      <Routes>
        <Route path="/" element={<Navigate to="/dashboard" replace />} />
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/runs" element={<RunsList />} />
        <Route path="/runs/:runId" element={<RunDetail />} />
        <Route path="/hitl" element={<Hitl />} />
        <Route path="/timeline" element={<Timeline />} />
        <Route path="/campaigns" element={<Stub routeId="campaigns" />} />
        <Route path="/logs" element={<Stub routeId="logs" />} />
        <Route path="/memory" element={<Stub routeId="memory" />} />
        <Route path="/config" element={<Stub routeId="config" />} />
      </Routes>
    </AppShell>
  );
}
