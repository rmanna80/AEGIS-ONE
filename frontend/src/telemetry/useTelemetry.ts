import { useEffect } from "react";
import { useStore } from "../state/store";
import type { Snapshot } from "../types";

const RECONNECT_MS = 1500;

/** Keeps one WebSocket to the backend open and feeds snapshots into the store. */
export function useTelemetry(): void {
  useEffect(() => {
    let socket: WebSocket | null = null;
    let timer: number | undefined;
    let closed = false;
    const { setLink, ingest } = useStore.getState();

    const connect = () => {
      setLink("connecting");
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(`${scheme}://${location.host}/ws/telemetry`);
      socket.onopen = () => setLink("open");
      socket.onmessage = (event) => {
        try {
          const message = JSON.parse(event.data as string) as Snapshot;
          if (message.type === "snapshot") ingest(message);
        } catch (error) {
          console.error("Discarded malformed telemetry message", error);
        }
      };
      socket.onclose = () => {
        setLink("closed");
        if (!closed) timer = window.setTimeout(connect, RECONNECT_MS);
      };
      socket.onerror = () => socket?.close();
    };

    connect();
    return () => {
      closed = true;
      window.clearTimeout(timer);
      socket?.close();
    };
  }, []);
}
