import { fixed } from "../lib/format";
import { compassPoint, headingDeg } from "../lib/frames";
import { useStore } from "../state/store";

// Short labels for PX4 flight modes that would not fit a tile.
const MODE_LABEL: Record<string, string> = {
  RETURN_TO_LAUNCH: "RETURN",
  FOLLOW_ME: "FOLLOW",
  STABILIZED: "STAB",
};

export function Overview() {
  const state = useStore((s) => s.snapshot?.state);
  const mission = useStore((s) => s.snapshot?.mission);
  const p = state?.position_enu_m;
  const heading = state?.attitude ? headingDeg(state.attitude.yaw_deg) : null;
  const battery = state?.battery?.remaining_fraction;
  const simulated = state?.provenance !== "physical";

  return (
    <section className="border-b border-line p-3">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="panel-title">Vehicle · {state?.vehicle_id ?? "--"}</h2>
        <span className="num text-[11px] text-dim">
          {state?.armed ? "ARMED" : "DISARMED"} · {state?.landed_state ?? "--"}
        </span>
      </div>
      <div className="grid grid-cols-3 gap-px overflow-hidden rounded-sm border border-line bg-line">
        <Tile label="Flight mode" value={MODE_LABEL[state?.flight_mode ?? ""] ?? state?.flight_mode ?? "--"} />
        <Tile label="Mission" value={(mission?.phase ?? "idle").toUpperCase()} />
        <Tile
          label="Altitude (rel)"
          value={fixed(state?.relative_altitude_m)}
          unit="m"
          note="above home"
        />
        <Tile label="Ground speed" value={fixed(state?.ground_speed_m_s)} unit="m/s" note="est." />
        <Tile
          label="Vertical speed"
          value={fixed(state?.vertical_speed_up_m_s)}
          unit="m/s"
          note="up +, est."
        />
        <Tile
          label="Heading"
          value={heading == null ? "--" : heading.toFixed(0).padStart(3, "0")}
          unit={heading == null ? "" : `° ${compassPoint(heading)}`}
        />
        <Tile label="East" value={fixed(p?.x)} unit="m" note="ENU, est." />
        <Tile label="North" value={fixed(p?.y)} unit="m" note="ENU, est." />
        <Tile
          label="Battery"
          value={battery == null ? "--" : (battery * 100).toFixed(0)}
          unit="%"
          note={`${fixed(state?.battery?.voltage_v, 1)} V${simulated ? " · sim model" : ""}`}
          tone={battery != null && battery <= 0.15 ? "crit" : battery != null && battery <= 0.25 ? "warn" : undefined}
        />
        <Tile
          label="GNSS"
          value={state?.gps ? String(state.gps.num_satellites) : "--"}
          unit="sats"
          note={state?.gps?.fix_type ?? ""}
        />
        <Tile
          label="Latitude"
          value={fixed(state?.geodetic?.latitude_deg, 6)}
          unit="°"
          small
        />
        <Tile
          label="Longitude"
          value={fixed(state?.geodetic?.longitude_deg, 6)}
          unit="°"
          small
        />
      </div>
      <p className="mt-2 text-[10px] leading-snug text-dim">
        "est." marks values AEGIS derives from the autopilot's estimates. Attitude: roll{" "}
        <span className="num">{fixed(state?.attitude?.roll_deg)}°</span> pitch{" "}
        <span className="num">{fixed(state?.attitude?.pitch_deg)}°</span>.
      </p>
    </section>
  );
}

function Tile(props: {
  label: string;
  value: string;
  unit?: string;
  note?: string;
  small?: boolean;
  tone?: "warn" | "crit";
}) {
  const tone = props.tone === "crit" ? "text-crit" : props.tone === "warn" ? "text-warn" : "";
  return (
    <div className="bg-ink-900 px-2.5 py-2">
      <div className="panel-title">{props.label}</div>
      <div className={`num ${props.small ? "text-[13px]" : "text-[18px]"} leading-tight ${tone}`}>
        {props.value}
        {props.unit && <span className="ml-1 text-[11px] text-dim">{props.unit}</span>}
      </div>
      {props.note !== undefined && (
        <div className="num h-3 text-[9px] text-dim">{props.note}</div>
      )}
    </div>
  );
}
