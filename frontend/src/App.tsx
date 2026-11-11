import { useEffect } from "react";
import { api } from "./api";
import { AlertsPanel } from "./components/AlertsPanel";
import { Charts } from "./components/Charts";
import { Header } from "./components/Header";
import { MissionPanel } from "./components/MissionPanel";
import { Overview } from "./components/Overview";
import { SearchPanel } from "./components/SearchPanel";
import { useStore } from "./state/store";
import { useTelemetry } from "./telemetry/useTelemetry";
import { VideoPanel } from "./components/VideoPanel";
import { Scene } from "./visualization/Scene";

export default function App() {
  useTelemetry();
  const setSystem = useStore((s) => s.setSystem);
  const setScenario = useStore((s) => s.setScenario);
  const setPerception = useStore((s) => s.setPerception);
  const setNavigation = useStore((s) => s.setNavigation);
  const setMap = useStore((s) => s.setMap);
  const setSar = useStore((s) => s.setSar);
  const sarEnabled = useStore((s) => Boolean(s.sar?.enabled));
  const perceptionEnabled = useStore((s) => Boolean(s.perception?.enabled));

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
    const loadPerception = () =>
      api
        .perception()
        .then((status) => !cancelled && setPerception(status))
        .catch(() => undefined);
    loadScenario();
    const loadNavigation = () =>
      api
        .navigation()
        .then((status) => {
          if (cancelled) return;
          setNavigation(status);
          // Fetch the occupancy map only when it has changed since the last copy.
          if (status.enabled && status.map_revision !== useStore.getState().mapRevision) {
            return api.map(useStore.getState().mapRevision).then((map) => {
              if (!cancelled && map.changed) setMap(map.voxels, map.resolution_m, map.revision);
            });
          }
        })
        .catch(() => undefined);
    const loadSar = () =>
      api
        .sar()
        .then((status) => {
          if (cancelled) return;
          if (!status.enabled) return setSar(status, []);
          return api
            .sarCandidates()
            .then((list) => !cancelled && setSar(status, list.candidates));
        })
        .catch(() => undefined);
    loadSystem();
    loadPerception();
    loadNavigation();
    loadSar();
    const timer = window.setInterval(() => {
      loadSystem();
      loadPerception();
      loadNavigation();
      loadSar();
    }, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [setScenario, setSystem, setPerception, setNavigation, setMap, setSar]);

  return (
    <div className="flex h-full min-w-[1100px] flex-col">
      <Header />
      <div className="flex min-h-0 flex-1">
        <main className="flex min-w-0 flex-1 flex-col">
          {/* Split screen: 3D environment on the left, camera + AI output on the right. */}
          <div className="flex min-h-0 flex-1">
            <div className="min-w-0 flex-1">
              <Scene />
            </div>
            {perceptionEnabled && (
              <div className="w-[42%] min-w-[360px] border-l border-line">
                <VideoPanel />
              </div>
            )}
          </div>
          <div className="h-44 shrink-0 border-t border-line bg-ink-900">
            <Charts />
          </div>
        </main>
        <aside className="flex w-[420px] shrink-0 flex-col overflow-y-auto border-l border-line bg-ink-950">
          <Overview />
          <MissionPanel />
          {sarEnabled && <SearchPanel />}
          <AlertsPanel />
        </aside>
      </div>
    </div>
  );
}
