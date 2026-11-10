import { useEffect } from "react";
import { api } from "./api";
import { AlertsPanel } from "./components/AlertsPanel";
import { Charts } from "./components/Charts";
import { Header } from "./components/Header";
import { MissionPanel } from "./components/MissionPanel";
import { Overview } from "./components/Overview";
import { useStore } from "./state/store";
import { useTelemetry } from "./telemetry/useTelemetry";
import { Scene } from "./visualization/Scene";

export default function App() {
  useTelemetry();
  const setSystem = useStore((s) => s.setSystem);
  const setScenario = useStore((s) => s.setScenario);

  useEffect(() => {
    let cancelled = false;
    const loadScenario = () =>
      api
        .scenario()
        .then((scenario) => !cancelled && setScenario(scenario))
        .catch(() => !cancelled && window.setTimeout(loadScenario, 2000));
    // System info includes the simulator attestation, which can change after start-up.
    const loadSystem = () =>
      api
        .system()
        .then((system) => !cancelled && setSystem(system))
        .catch(() => undefined);
    loadScenario();
    loadSystem();
    const timer = window.setInterval(loadSystem, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [setScenario, setSystem]);

  return (
    <div className="flex h-full min-w-[1100px] flex-col">
      <Header />
      <div className="flex min-h-0 flex-1">
        <main className="flex min-w-0 flex-1 flex-col">
          <div className="min-h-0 flex-1">
            <Scene />
          </div>
          <div className="h-44 shrink-0 border-t border-line bg-ink-900">
            <Charts />
          </div>
        </main>
        <aside className="flex w-[420px] shrink-0 flex-col overflow-y-auto border-l border-line bg-ink-950">
          <Overview />
          <MissionPanel />
          <AlertsPanel />
        </aside>
      </div>
    </div>
  );
}
