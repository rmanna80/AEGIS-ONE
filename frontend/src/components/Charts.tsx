import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { type HistoryPoint, useStore } from "../state/store";

interface Series {
  key: keyof HistoryPoint;
  name: string;
  colour: string;
}

function Chart(props: { title: string; unit: string; series: Series[]; domain?: [number, number] }) {
  const history = useStore((s) => s.history);
  return (
    <div className="flex min-w-0 flex-1 flex-col border-r border-line px-3 py-2 last:border-r-0">
      <div className="mb-1 flex items-center justify-between">
        <h3 className="panel-title">
          {props.title} <span className="normal-case tracking-normal">({props.unit})</span>
        </h3>
        <div className="flex gap-3">
          {props.series.map((s) => (
            <span key={s.key} className="flex items-center gap-1 text-[10px] text-dim">
              <span className="inline-block h-0.5 w-3" style={{ background: s.colour }} />
              {s.name}
            </span>
          ))}
        </div>
      </div>
      <div className="min-h-0 flex-1">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={history} margin={{ top: 4, right: 6, bottom: 0, left: -18 }}>
            <CartesianGrid stroke="#1e2833" vertical={false} />
            <XAxis
              dataKey="t"
              type="number"
              domain={["dataMin", "dataMax"]}
              tickFormatter={(t: number) => `${t.toFixed(0)}s`}
              tick={{ fill: "#7c8b9a", fontSize: 10 }}
              stroke="#263341"
              minTickGap={40}
            />
            <YAxis
              domain={props.domain ?? ["auto", "auto"]}
              tick={{ fill: "#7c8b9a", fontSize: 10 }}
              stroke="#263341"
              width={48}
              tickFormatter={(v: number) => v.toFixed(v % 1 === 0 ? 0 : 1)}
            />
            <Tooltip
              contentStyle={{ background: "#0d1218", border: "1px solid #263341", fontSize: 11 }}
              labelFormatter={(t) => `t = ${Number(t).toFixed(1)} s`}
              formatter={(value) => `${Number(value).toFixed(2)} ${props.unit}`}
              isAnimationActive={false}
            />
            {props.series.map((s) => (
              <Line
                key={s.key}
                type="monotone"
                dataKey={s.key}
                name={s.name}
                stroke={s.colour}
                strokeWidth={1.6}
                dot={false}
                isAnimationActive={false}
                connectNulls={false}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

export function Charts() {
  const physical = useStore((s) => s.snapshot?.state?.provenance === "physical");
  return (
    <div className="flex h-full">
      <Chart
        title="Altitude above home"
        unit="m"
        series={[{ key: "altitude", name: "Relative altitude", colour: "#4cc9f0" }]}
      />
      <Chart
        title="Velocity"
        unit="m/s"
        series={[
          { key: "groundSpeed", name: "Ground speed", colour: "#3ecf8e" },
          { key: "verticalSpeed", name: "Vertical (up +)", colour: "#f472b6" },
        ]}
      />
      <Chart
        title={physical ? "Battery" : "Battery (simulated model)"}
        unit="%"
        domain={[0, 100]}
        series={[{ key: "battery", name: "Remaining", colour: "#f5a524" }]}
      />
    </div>
  );
}
